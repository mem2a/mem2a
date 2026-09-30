# SPDX-License-Identifier: Apache-2.0
"""A small Mem2A client for acting agents, plus helpers to read payloads.

    memory = await Mem2AClient.connect('https://memory.example.com', token=token)
    task = await memory.negotiate(intent, push=PushTarget(url, bearer=secret))
    dossier = dossier_of(task)          # or question_of(task) / error_of(task)
    ...act...
    task = await memory.commit(task, commit)
    receipt = receipt_of(task)

The helpers (`dossier_of`, `update_of`, ...) accept a Task, a StreamResponse
(from `listen`), a push notification body (a dict), a Message or an Artifact.
"""

from __future__ import annotations

import uuid

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, Union

import httpx

from google.protobuf.json_format import MessageToDict, ParseDict

from a2a.client import A2ACardResolver, Client, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import ServiceParametersFactory, with_a2a_extensions
from a2a.helpers import new_data_part
from a2a.types import (
    AgentCard,
    Artifact,
    AuthenticationInfo,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import TransportProtocol
from mem2a import constants as C
from mem2a import models
from mem2a.card import mem2a_params


#: Anything the reading helpers understand.
Readable = Union[Task, StreamResponse, Message, Artifact, TaskStatus, Mapping[str, Any]]
M = TypeVar('M', bound=models.Payload)


@dataclass(frozen=True)
class PushTarget:
    """A webhook memory should POST task updates to (A2A push notifications)."""

    url: str
    #: Sent back as ``X-A2A-Notification-Token``.
    token: str | None = None
    #: Sent back as ``Authorization: Bearer <bearer>``.
    bearer: str | None = None

    def config(self, task_id: str = '') -> TaskPushNotificationConfig:
        return TaskPushNotificationConfig(
            task_id=task_id,
            url=self.url,
            token=self.token or '',
            authentication=(
                AuthenticationInfo(scheme='Bearer', credentials=self.bearer)
                if self.bearer
                else None
            ),
        )


class Mem2AClient:
    """Negotiate, answer, commit, listen and cancel against a Mem2A memory.

    Every request asks to activate the extension (``A2A-Extensions``).
    """

    def __init__(
        self, card: AgentCard, http: httpx.AsyncClient, blocking: Client, streaming: Client
    ) -> None:
        self.card = card
        self._http = http
        self._blocking = blocking  # SendMessage & co.
        self._streaming = streaming  # SubscribeToTask
        self._context = ClientCallContext(
            service_parameters=ServiceParametersFactory.create(
                [with_a2a_extensions([C.EXTENSION_URI])]
            )
        )

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> Mem2AClient:
        """Fetch the Agent Card at `url` and check that it is a Mem2A memory.

        `token` is sent as ``Authorization: Bearer <token>`` on every request.
        The client owns `http` from now on (`close` closes it).
        """
        http = http or httpx.AsyncClient(timeout=30)
        if token is not None:
            http.headers['Authorization'] = f'Bearer {token}'
        card = await A2ACardResolver(http, url).get_agent_card()
        if mem2a_params(card) is None:
            await http.aclose()
            raise ValueError(f'{url} is not a Mem2A memory: no {C.EXTENSION_URI} in its card')

        # a2a-sdk's ClientConfig.streaming picks SendMessage vs
        # SendStreamingMessage *and* gates subscribe(), so keep two clients.
        def create(streaming: bool) -> Client:
            config = ClientConfig(
                httpx_client=http,
                streaming=streaming,
                supported_protocol_bindings=[TransportProtocol.JSONRPC],
            )
            return ClientFactory(config).create(card)

        return cls(card, http, create(False), create(True))

    async def __aenter__(self) -> Mem2AClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()  # shared by both SDK clients

    # ---------------------------------------------------------- protocol
    async def negotiate(
        self,
        intent: models.Intent | Mapping[str, Any],
        *,
        push: PushTarget | None = None,
        context_id: str | None = None,
    ) -> Task:
        """Open a task with an intent. Returns it in INPUT_REQUIRED (a dossier
        or a question) or REJECTED (an error)."""
        config = (
            SendMessageConfiguration(task_push_notification_config=push.config())
            if push is not None
            else None
        )
        message = self._message(C.INTENT, intent, context_id=context_id)
        return await self._send(message, config)

    async def answer(self, task: Task, question_id: str, text: str) -> Task:
        answer = models.Answer(question_id=question_id, text=text)
        return await self._send(self._message(C.ANSWER, answer, task=task))

    async def commit(self, task: Task, commit: models.Commit | Mapping[str, Any]) -> Task:
        """Report what the agent did. COMPLETED with a receipt, or still
        INPUT_REQUIRED with an error (for example ``stale-dossier``)."""
        return await self._send(self._message(C.COMMIT, commit, task=task))

    async def get(self, task_id: str) -> Task:
        return await self._blocking.get_task(GetTaskRequest(id=task_id), context=self._context)

    async def cancel(self, task_id: str) -> Task:
        """Stop the watch (CANCELED, phase ``canceled``)."""
        return await self._blocking.cancel_task(
            CancelTaskRequest(id=task_id), context=self._context
        )

    async def add_push(self, task_id: str, target: PushTarget) -> TaskPushNotificationConfig:
        return await self._blocking.create_task_push_notification_config(
            target.config(task_id), context=self._context
        )

    async def listen(self, task_id: str) -> AsyncIterator[StreamResponse]:
        """SubscribeToTask: the task first, then its updates, until it ends."""
        async for event in self._streaming.subscribe(
            SubscribeToTaskRequest(id=task_id), context=self._context
        ):
            yield event

    # ----------------------------------------------------------- helpers
    def _message(
        self,
        media_type: str,
        payload: models.Payload | Mapping[str, Any],
        *,
        task: Task | None = None,
        context_id: str | None = None,
    ) -> Message:
        data = payload.dump() if isinstance(payload, models.Payload) else dict(payload)
        return Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            task_id=task.id if task is not None else None,
            context_id=task.context_id if task is not None else context_id,
            parts=[new_data_part(data, media_type=media_type)],
            extensions=[C.EXTENSION_URI],
        )

    async def _send(
        self, message: Message, configuration: SendMessageConfiguration | None = None
    ) -> Task:
        request = SendMessageRequest(message=message, configuration=configuration)
        async for event in self._blocking.send_message(request, context=self._context):
            if event.HasField('task'):
                return event.task
        raise RuntimeError('Memory answered without a task')


# ============================================================ reading helpers
def parse_push(body: Mapping[str, Any]) -> StreamResponse:
    """A push notification body (a StreamResponse in JSON) as a proto."""
    return ParseDict(dict(body), StreamResponse(), ignore_unknown_fields=True)


def _focus(obj: Readable) -> tuple[Sequence[Artifact], Message | None, TaskState | None]:
    """(artifacts, status message, state) of anything readable."""
    if isinstance(obj, Mapping):
        obj = parse_push(obj)
    if isinstance(obj, StreamResponse):
        kind = obj.WhichOneof('payload')
        if kind == 'task':
            obj = obj.task
        elif kind == 'artifact_update':
            return [obj.artifact_update.artifact], None, None
        elif kind == 'status_update':
            obj = obj.status_update.status
        elif kind == 'message':
            return [], obj.message, None
        else:
            return [], None, None
    if isinstance(obj, Task):
        return list(obj.artifacts), _status_message(obj.status), obj.status.state
    if isinstance(obj, TaskStatus):
        return [], _status_message(obj), obj.state
    if isinstance(obj, Artifact):
        return [obj], None, None
    if isinstance(obj, Message):
        return [], obj, None
    raise TypeError(f'Cannot read Mem2A payloads from {type(obj).__name__}')


def _status_message(status: TaskStatus) -> Message | None:
    return status.message if status.HasField('message') else None


def _data(parts: Sequence[Part], media_type: str) -> Any:
    for part in parts:
        if part.media_type == media_type and part.HasField('data'):
            return MessageToDict(part.data)
    return None


def _from_artifact(obj: Readable, artifact_id: str, media_type: str, model: type[M]) -> M | None:
    artifacts, _, _ = _focus(obj)
    for artifact in artifacts:
        if artifact.artifact_id == artifact_id:
            data = _data(artifact.parts, media_type)
            return None if data is None else model.model_validate(data)
    return None


def _from_status(obj: Readable, media_type: str, model: type[M]) -> M | None:
    _, message, _ = _focus(obj)
    data = None if message is None else _data(message.parts, media_type)
    return None if data is None else model.model_validate(data)


def dossier_of(obj: Readable) -> models.Dossier | None:
    return _from_artifact(obj, C.DOSSIER_ARTIFACT, C.DOSSIER, models.Dossier)


def receipt_of(obj: Readable) -> models.Receipt | None:
    return _from_artifact(obj, C.RECEIPT_ARTIFACT, C.RECEIPT, models.Receipt)


def question_of(obj: Readable) -> models.Question | None:
    return _from_status(obj, C.QUESTION, models.Question)


def update_of(obj: Readable) -> models.Update | None:
    return _from_status(obj, C.UPDATE, models.Update)


def error_of(obj: Readable) -> models.Error | None:
    return _from_status(obj, C.ERROR, models.Error)


def phase_of(obj: Readable) -> str | None:
    """The Mem2A phase of the current status message, if any."""
    _, message, _ = _focus(obj)
    if message is None:
        return None
    phase = MessageToDict(message.metadata).get(C.PHASE_KEY)
    return phase if isinstance(phase, str) else None


def state_of(obj: Readable) -> str | None:
    """The task state name, e.g. ``TASK_STATE_INPUT_REQUIRED``."""
    _, _, state = _focus(obj)
    return None if state is None else TaskState.Name(state)


def text_of(obj: Readable) -> str:
    """The human-readable text of the current status message."""
    _, message, _ = _focus(obj)
    if message is None:
        return ''
    return ' '.join(part.text for part in message.parts if part.HasField('text'))
