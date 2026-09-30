# SPDX-License-Identifier: Apache-2.0
"""A small Mem2A client for acting agents, plus helpers that read payloads.

    memory = await Mem2AClient.connect('https://memory.example.com', token=token)
    task = await memory.negotiate(intent, push=PushTarget(url, bearer=secret))
    dossier = dossier_of(task)          # or question_of(task), error_of(task)
    ...act, then...
    task = await memory.commit(task, commit)
    receipt = receipt_of(task)

The helpers (`dossier_of`, `update_of`, ...) accept a Task, a StreamResponse
(from `Mem2AClient.subscribe`), a push notification body (a dict), a
Message, an Artifact or a TaskStatus.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
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
from google.protobuf.json_format import MessageToDict, ParseDict

from mem2a import constants as C
from mem2a import models
from mem2a.card import mem2a_params


#: Anything the reading helpers understand.
Readable = Task | StreamResponse | Message | Artifact | TaskStatus | Mapping[str, Any]
M = TypeVar('M', bound=models.Payload)


@dataclass(frozen=True)
class PushTarget:
    """A webhook memory should POST task events to (A2A push notifications)."""

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
    """Negotiate, answer, commit, subscribe and cancel against a Mem2A memory.

    Every request asks to activate the extension (``A2A-Extensions``) and
    every message lists it (spec 6.1, 6.2). Create one with `connect`.
    """

    def __init__(
        self, card: AgentCard, http: httpx.AsyncClient, blocking: Client, streaming: Client
    ) -> None:
        self.card = card
        self._http = http
        self._blocking = blocking  # SendMessage, GetTask, CancelTask, push configs
        self._streaming = streaming  # SubscribeToTask
        self._context = ClientCallContext(
            service_parameters=ServiceParametersFactory.create(
                [with_a2a_extensions([C.EXTENSION_URI])]
            )
        )

    @classmethod
    async def connect(
        cls, url: str, *, token: str | None = None, http: httpx.AsyncClient | None = None
    ) -> Mem2AClient:
        """Fetch the Agent Card at `url` and check that it is a Mem2A memory.

        `token` is sent as ``Authorization: Bearer <token>`` on every request.
        The client takes ownership of `http`: `close` closes it.
        """
        http = http or httpx.AsyncClient(timeout=30)
        if token is not None:
            http.headers['Authorization'] = f'Bearer {token}'
        try:
            card = await A2ACardResolver(http, url).get_agent_card()
            if mem2a_params(card) is None:
                raise ValueError(f'{url} is not a Mem2A memory: its card lacks {C.EXTENSION_URI}')
        except BaseException:
            await http.aclose()
            raise

        # ClientConfig.streaming picks SendMessage vs SendStreamingMessage and
        # also gates subscribe(), so keep one client of each kind.
        def create(streaming: bool) -> Client:
            config = ClientConfig(
                httpx_client=http,
                streaming=streaming,
                supported_protocol_bindings=[TransportProtocol.JSONRPC],
            )
            return ClientFactory(config).create(card)

        return cls(card, http, create(streaming=False), create(streaming=True))

    async def __aenter__(self) -> Mem2AClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client (both SDK clients share it)."""
        await self._http.aclose()

    # ------------------------------------------------------------ protocol
    async def negotiate(
        self,
        intent: models.Intent | Mapping[str, Any],
        *,
        push: PushTarget | None = None,
        context_id: str | None = None,
    ) -> Task:
        """Open a task with an intent (spec 8.1).

        Returns the task in INPUT_REQUIRED (a dossier or a question) or
        REJECTED (an error). With `push`, memory POSTs every event of the
        task, including later updates, to that webhook.
        """
        configuration = (
            SendMessageConfiguration(task_push_notification_config=push.config())
            if push is not None
            else None
        )
        message = _message(C.INTENT, intent, context_id=context_id)
        return await self._send(message, configuration)

    async def answer(self, task: Task, question_id: str, text: str) -> Task:
        """Answer memory's question on the same task (spec 8.2)."""
        answer = models.Answer(question_id=question_id, text=text)
        return await self._send(_message(C.ANSWER, answer, task=task))

    async def commit(self, task: Task, commit: models.Commit | Mapping[str, Any]) -> Task:
        """Report what the agent did (spec 8.4).

        Returns the task COMPLETED with a receipt, or still INPUT_REQUIRED
        with an error (for example ``stale-dossier``).
        """
        return await self._send(_message(C.COMMIT, commit, task=task))

    async def get(self, task_id: str) -> Task:
        """GetTask: the task as memory has it now, with its current dossier."""
        return await self._blocking.get_task(GetTaskRequest(id=task_id), context=self._context)

    async def cancel(self, task_id: str) -> Task:
        """CancelTask: memory stops watching (CANCELED, phase ``canceled``)."""
        return await self._blocking.cancel_task(
            CancelTaskRequest(id=task_id), context=self._context
        )

    async def add_push(self, task_id: str, target: PushTarget) -> TaskPushNotificationConfig:
        """Register a webhook for an existing task."""
        return await self._blocking.create_task_push_notification_config(
            target.config(task_id), context=self._context
        )

    async def subscribe(self, task_id: str) -> AsyncIterator[StreamResponse]:
        """SubscribeToTask: the task first, then its events, until it ends."""
        request = SubscribeToTaskRequest(id=task_id)
        async for event in self._streaming.subscribe(request, context=self._context):
            yield event

    async def _send(
        self, message: Message, configuration: SendMessageConfiguration | None = None
    ) -> Task:
        request = SendMessageRequest(message=message, configuration=configuration)
        async for event in self._blocking.send_message(request, context=self._context):
            if event.HasField('task'):
                return event.task
        raise RuntimeError('Memory answered without a task')


def _message(
    media_type: str,
    payload: models.Payload | Mapping[str, Any],
    *,
    task: Task | None = None,
    context_id: str | None = None,
) -> Message:
    """An agent message with exactly one Mem2A payload, listing the extension."""
    data = payload.dump() if isinstance(payload, models.Payload) else dict(payload)
    return Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        task_id=task.id if task is not None else None,
        # Always send the contextId with the taskId (see Mem2ARequestHandler).
        context_id=task.context_id if task is not None else context_id,
        parts=[new_data_part(data, media_type=media_type)],
        extensions=[C.EXTENSION_URI],
    )


# ========================================================== reading helpers
def parse_push(body: Mapping[str, Any]) -> StreamResponse:
    """A push notification body (a StreamResponse as JSON) as a proto."""
    response = StreamResponse()
    ParseDict(dict(body), response, ignore_unknown_fields=True)
    return response


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
    """The dossier in a task, an artifact update or a push body."""
    return _from_artifact(obj, C.DOSSIER_ARTIFACT, C.DOSSIER, models.Dossier)


def receipt_of(obj: Readable) -> models.Receipt | None:
    """The receipt of a committed task."""
    return _from_artifact(obj, C.RECEIPT_ARTIFACT, C.RECEIPT, models.Receipt)


def question_of(obj: Readable) -> models.Question | None:
    """The question in the current status message, if memory asked one."""
    return _from_status(obj, C.QUESTION, models.Question)


def update_of(obj: Readable) -> models.Update | None:
    """The update in the current status message: what changed in the dossier."""
    return _from_status(obj, C.UPDATE, models.Update)


def error_of(obj: Readable) -> models.Error | None:
    """The error in the current status message, if memory refused something."""
    return _from_status(obj, C.ERROR, models.Error)


def phase_of(obj: Readable) -> str | None:
    """The Mem2A phase of the current status message, if any."""
    _, message, _ = _focus(obj)
    if message is None:
        return None
    phase = MessageToDict(message.metadata).get(C.PHASE_KEY)
    return phase if isinstance(phase, str) else None


def state_of(obj: Readable) -> str | None:
    """The task state name, for example ``TASK_STATE_INPUT_REQUIRED``."""
    _, _, state = _focus(obj)
    return None if state is None else TaskState.Name(state)


def text_of(obj: Readable) -> str:
    """The text of the current status message, for people."""
    _, message, _ = _focus(obj)
    if message is None:
        return ''
    return ' '.join(part.text for part in message.parts if part.HasField('text'))
