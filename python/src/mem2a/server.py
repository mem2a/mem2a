# SPDX-License-Identifier: Apache-2.0
"""Serve a `MemoryEngine` as an A2A v1.0 JSON-RPC agent that speaks Mem2A v0.1.

Built on a2a-sdk 1.2.x:

* `Mem2AExecutor` (an ``AgentExecutor``) turns engine `Reply` objects into A2A
  events: the dossier or receipt artifact first, then one status message that
  carries the phase and, in reply to an agent message, ``inReplyTo``. Any
  error in a turn ends the task as FAILED (phase ``failed``, error
  ``internal``); the details go to the server log only.
* `Mem2ARequestHandler` (a ``DefaultRequestHandlerV2``) adds the Mem2A rules:
  it refuses unactivated SendMessage (-32008) before a task exists, runs one
  turn at a time per task, answers a retried message from the task instead of
  processing it twice, filters stored dossiers by current access on every
  read, and screens and caps push notification configs.
* `Deliveries` listens to the engine and runs *internal turns* through the SDK
  (`run_internal_turn`), so updates are stored, pushed and streamed exactly
  like client-driven turns.
* `QueuedPushSender` sends push notifications in the background, in order, with
  retries, so no reply waits for a webhook.
* `AuthMiddleware` authenticates every JSON-RPC request (HTTP 401 otherwise);
  `ExtensionEchoMiddleware` echoes the activated extensions in the
  ``A2A-Extensions`` response header, which the SDK does not do.

`create_app` wires it all into a Starlette app.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import socket
import urllib.parse
import weakref
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterable,
    Iterator,
)
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from a2a.auth.user import User
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import (
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
    PushNotificationConfigStore,
    PushNotificationSender,
    TaskStore,
    TaskUpdater,
)
from a2a.server.tasks.push_notification_sender import PushNotificationEvent
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import (
    A2AError,
    ExtensionSupportRequiredError,
    InternalError,
    InvalidParamsError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.proto_utils import to_stream_response
from a2a.utils.push_url_validator import validate_push_notification_url
from a2a.utils.task import apply_history_length
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as ASGIMessage

from mem2a import constants as C
from mem2a import models
from mem2a.admin import admin_routes
from mem2a.auth import AuthenticationError, Authenticator, Identity
from mem2a.card import build_agent_card
from mem2a.engine import MemoryEngine, Payloads, Reply
from mem2a.validation import ValidationMode, check_outgoing


logger = logging.getLogger(__name__)

RPC_PATH = '/a2a/jsonrpc'

# Keys in the ASGI scope and in ServerCallContext.state. The server builds
# `state` itself, so clients cannot set them.
IDENTITY_KEY = 'mem2a.identity'
TURN_KEY = 'mem2a.internal_turn'
TURN_RAN_KEY = 'mem2a.internal_turn_ran'
ACTIVATED_KEY = 'mem2a.activated_extensions'

#: JSON-RPC code in the body of HTTP 401 responses. A2A defines no code for
#: authentication failures (it uses the binding's own error, HTTP 401), so
#: this is the generic implementation-defined server error.
AUTH_ERROR_CODE = -32000

#: Webhook origins on this machine, for sandboxes and demos (never production).
LOCALHOST_ORIGINS = (
    'http://127.0.0.1:*',
    'http://localhost:*',
    'http://[::1]:*',
    'https://127.0.0.1:*',
    'https://localhost:*',
    'https://[::1]:*',
)

InternalTurn = Literal['refresh', 'expire']
UrlValidator = Callable[[str], Awaitable[bool]]

TASK_STATE: dict[C.Phase, TaskState] = {
    'working': TaskState.TASK_STATE_WORKING,
    'question': TaskState.TASK_STATE_INPUT_REQUIRED,
    'awaiting-commit': TaskState.TASK_STATE_INPUT_REQUIRED,
    'reauth': TaskState.TASK_STATE_AUTH_REQUIRED,
    'committed': TaskState.TASK_STATE_COMPLETED,
    'refused': TaskState.TASK_STATE_REJECTED,
    'expired': TaskState.TASK_STATE_CANCELED,
    'canceled': TaskState.TASK_STATE_CANCELED,
    'failed': TaskState.TASK_STATE_FAILED,
}


class Mem2AUser(User):
    """The SDK scopes tasks and push configs by ``user_name``: a task belongs
    to the agent *and* the principal it acts for."""

    def __init__(self, identity: Identity) -> None:
        self.identity = identity

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return f'{self.identity.agent}|{self.identity.principal}'


def mem2a_parts(message: Message | None) -> Payloads:
    """The Mem2A parts of an incoming message as (mediaType, data) pairs.

    Text and other parts are ignored: memory never bases decisions on them.
    """
    if message is None:
        return []
    return [
        (part.media_type, MessageToDict(part.data) if part.HasField('data') else None)
        for part in message.parts
        if part.media_type.startswith('application/vnd.mem2a.')
    ]


@contextlib.contextmanager
def _no_leaks(operation: str) -> Iterator[None]:
    """Turn unexpected exceptions into a generic A2A internal error.

    The SDK would send the exception's text to the client (-32603); log it
    here instead. A2A errors (not found, invalid params, ...) pass through.
    """
    try:
        yield
    except A2AError:
        raise
    except Exception:
        logger.exception('%s failed', operation)
        raise InternalError(message='Internal error.') from None


# =============================================================== push URLs
_ORIGIN = re.compile(r'^(https?)://(\[[^\]]+\]|[^/:?#@\[\]]+)(?::(\d+|\*))?/?$', re.IGNORECASE)
_DEFAULT_PORT = {'http': '80', 'https': '443'}


def _pattern(origin: str) -> tuple[str, str, str]:
    match = _ORIGIN.match(origin.strip())
    if match is None:
        raise ValueError(f'Not an origin: {origin!r}. Use scheme://host[:port]; the port may be *')
    scheme = match.group(1).lower()
    return scheme, match.group(2).lower(), match.group(3) or _DEFAULT_PORT[scheme]


def _origin_of(url: str) -> tuple[str, str, str] | None:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORT or not parts.hostname:
        return None
    host = f'[{parts.hostname}]' if ':' in parts.hostname else parts.hostname
    return parts.scheme, host.lower(), str(port or _DEFAULT_PORT[parts.scheme])


class PushPolicy:
    """Which webhook URLs memory will call (spec 11.4).

    Args:
        origins: If set, only these origins, for example
            ``https://agent.example.com`` or ``http://127.0.0.1:*`` (any port).
        validator: Otherwise, an async check. The default, the SDK's
            ``validate_push_notification_url``, refuses hosts that resolve to
            private, loopback or link-local addresses. None allows any URL.
    """

    def __init__(
        self,
        origins: Iterable[str] | None = None,
        validator: UrlValidator | None = validate_push_notification_url,
    ) -> None:
        self._origins = None if origins is None else [_pattern(o) for o in origins]
        self._validator = validator

    async def allows(self, url: str) -> bool:
        if self._origins is not None:
            origin = _origin_of(url)
            return origin is not None and any(
                origin[:2] == allowed[:2] and allowed[2] in ('*', origin[2])
                for allowed in self._origins
            )
        return self._validator is None or await self._validator(url)


# ============================================================ push sender
def _push_body(event: PushNotificationEvent) -> bytes:
    body = MessageToDict(to_stream_response(event))
    update = body.get('artifactUpdate')
    if isinstance(update, dict):
        update.setdefault('append', False)  # proto3 drops false; the spec shows it
    return json.dumps(body).encode()


class QueuedPushSender(PushNotificationSender):
    """Sends push notifications in the background, so no reply waits for one.

    Each (task, webhook) pair has its own queue, so events arrive in order. A
    POST that fails with a network error, 408, 429 or 5xx is retried up to
    `retries` times with exponential backoff; redirects are not followed.
    Bodies are StreamResponse JSON with ``Content-Type: application/a2a+json``,
    plus ``Authorization`` and ``X-A2A-Notification-Token`` as configured. The
    `policy` is checked again before every POST.
    """

    def __init__(
        self,
        config_store: PushNotificationConfigStore,
        http: httpx.AsyncClient,
        policy: PushPolicy,
        *,
        retries: int = 3,
        backoff: float = 0.25,
    ) -> None:
        self._store = config_store
        self._http = http
        self._policy = policy
        self._retries = retries
        self._backoff = backoff
        self._queues: dict[tuple[str, str], deque[tuple[TaskPushNotificationConfig, bytes]]] = {}
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def send_notification(self, task_id: str, event: PushNotificationEvent) -> None:
        configs = await self._store.get_info_for_dispatch(task_id)
        if not configs:
            return
        body = _push_body(event)  # serialize now: the SDK may reuse the event
        for config in configs:
            key = (task_id, config.id)
            self._queues.setdefault(key, deque()).append((config, body))
            if key not in self._workers:
                self._workers[key] = asyncio.get_running_loop().create_task(self._drain(key))

    async def wait_idle(self) -> None:
        """Wait until every queued notification was delivered or given up."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)

    async def aclose(self, timeout: float = 2.0) -> None:
        """Give queued notifications `timeout` seconds, then drop the rest."""
        workers = list(self._workers.values())
        if workers:
            _done, pending = await asyncio.wait(workers, timeout=timeout)
            for worker in pending:
                worker.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _drain(self, key: tuple[str, str]) -> None:
        queue = self._queues[key]
        try:
            while queue:
                config, body = queue.popleft()
                try:
                    await self._deliver(key[0], config, body)
                except Exception:  # a custom URL validator, say; keep the queue going
                    logger.exception('Push for task %s to %s failed', key[0], config.url)
        finally:
            del self._workers[key]
            if not queue:
                self._queues.pop(key, None)

    async def _deliver(self, task_id: str, config: TaskPushNotificationConfig, body: bytes) -> None:
        headers = {'Content-Type': C.A2A_JSON}
        if config.token:
            headers['X-A2A-Notification-Token'] = config.token
        auth = config.authentication
        if config.HasField('authentication') and auth.scheme and auth.credentials:
            headers['Authorization'] = f'{auth.scheme} {auth.credentials}'
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            if not await self._policy.allows(config.url):
                logger.warning('Not pushing task %s to %s: URL not allowed', task_id, config.url)
                return
            try:
                response = await self._http.post(
                    config.url, content=body, headers=headers, follow_redirects=False
                )
            except httpx.HTTPError as error:
                logger.info('Push for task %s to %s failed: %r', task_id, config.url, error)
                continue
            if response.is_success:
                return
            status = response.status_code
            if status not in (408, 429) and status < 500:
                logger.warning('Push for task %s to %s got HTTP %d', task_id, config.url, status)
                return
            logger.info('Push for task %s to %s got HTTP %d', task_id, config.url, status)
        logger.warning(
            'Gave up pushing to %s for task %s after %d attempts',
            config.url,
            task_id,
            self._retries + 1,
        )


# ================================================================ executor
class Mem2AExecutor(AgentExecutor):
    """Runs the engine for each turn and publishes what it replies."""

    def __init__(self, engine: MemoryEngine, *, validation: ValidationMode = 'log') -> None:
        self.engine = engine
        self.validation = validation

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id or '', context.context_id or ''
        state = context.call_context.state
        turn: InternalTurn | None = state.get(TURN_KEY)
        message = context.message
        in_reply_to = message.message_id if message is not None and turn is None else None
        if turn is None and context.current_task is None:
            # A new task: publish it first, with the intent in its history.
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[message] if message is not None else [],
                )
            )
        updater = TaskUpdater(event_queue, task_id, context_id)
        try:
            reply = self._reply(context, turn)
            if reply is not None:
                await self.publish(reply, updater, in_reply_to)
        except Exception:
            logger.exception('A %s turn of task %s failed', turn or 'client', task_id)
            await self._fail(task_id, updater, in_reply_to)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """CancelTask: stop watching and end the task (phase ``canceled``)."""
        task_id = context.task_id or ''
        updater = TaskUpdater(event_queue, task_id, context.context_id or '')
        try:
            reply = self.engine.cancel(task_id) or Reply(phase='canceled', text='Canceled.')
            await self.publish(reply, updater)
        except Exception:
            logger.exception('Canceling task %s failed', task_id)
            await self._fail(task_id, updater, None)

    def _reply(self, context: RequestContext, turn: InternalTurn | None) -> Reply | None:
        task_id, context_id = context.task_id or '', context.context_id or ''
        state = context.call_context.state
        identity: Identity = state[IDENTITY_KEY]
        if turn is not None:
            state[TURN_RAN_KEY] = True
            return (
                self.engine.refresh(task_id) if turn == 'refresh' else self.engine.expire(task_id)
            )
        payloads = mem2a_parts(context.message)
        if context.current_task is None:
            return self.engine.negotiate(task_id, context_id, identity, payloads)
        return self.engine.respond(task_id, identity, payloads)

    async def publish(
        self, reply: Reply, updater: TaskUpdater, in_reply_to: str | None = None
    ) -> None:
        """Artifacts first (a blocking SendMessage returns at the status), then
        one status message: text, the Mem2A payloads, the phase and inReplyTo."""
        artifacts: dict[C.PayloadKind, models.Payload | None] = {
            'dossier': reply.dossier,
            'receipt': reply.receipt,
        }
        for kind, payload in artifacts.items():
            if payload is not None:
                await updater.add_artifact(
                    parts=[self._part(kind, payload)],
                    artifact_id=kind,
                    name=kind,
                    extensions=[C.EXTENSION_URI],
                    append=False,  # same artifactId and append false: replace it
                    last_chunk=True,
                )
        payloads: dict[C.PayloadKind, models.Payload | None] = {
            'question': reply.question,
            'update': reply.update,
            'error': reply.error,
        }
        parts = [Part(text=reply.text)]
        parts += [self._part(kind, p) for kind, p in payloads.items() if p is not None]
        metadata: dict[str, str] = {C.PHASE_KEY: reply.phase}
        if in_reply_to:
            metadata[C.IN_REPLY_TO_KEY] = in_reply_to
        message = updater.new_agent_message(parts, metadata=metadata)
        message.extensions.append(C.EXTENSION_URI)
        await updater.update_status(TASK_STATE[reply.phase], message=message)

    async def _fail(self, task_id: str, updater: TaskUpdater, in_reply_to: str | None) -> None:
        """End the task as FAILED without saying why (the log has the details)."""
        try:
            await self.publish(self.engine.fail(task_id), updater, in_reply_to)
        except Exception:
            logger.exception('Could not report the failure of task %s', task_id)
            raise RuntimeError('Internal error.') from None  # the SDK sends this text

    def _part(self, kind: C.PayloadKind, payload: models.Payload) -> Part:
        data = payload.dump()
        check_outgoing(kind, data, self.validation)
        return new_data_part(data, media_type=C.MEDIA_TYPE_OF[kind])


# ================================================================= handler
class Mem2ARequestHandler(DefaultRequestHandlerV2):
    """DefaultRequestHandlerV2 plus the Mem2A rules.

    * **Activation**: SendMessage without Mem2A activation gets -32008 before
      a task exists (raising from the executor would leave a FAILED task).
    * **Binding**: every operation on a task by anyone but the agent and
      principal that opened it gets TaskNotFoundError (the SDK scopes tasks
      by `Mem2AUser.user_name`).
    * **One turn at a time**: a2a-sdk 1.2.x answers a blocking SendMessage
      with the first INPUT_REQUIRED or terminal event on the task's shared
      event stream, even one from an earlier turn still in flight, such as an
      update memory is delivering. Client follow-ups, cancels and memory's
      internal turns therefore take `turn_lock` for the whole turn.
    * **Retries**: a follow-up whose messageId the task already has in its
      history gets the task back without being processed again, even when the
      task has completed (so a retried commit returns its receipt and is never
      recorded twice).
    * **Reads**: GetTask, ListTasks, stream snapshots and replies filter stored
      dossiers by the principal's current access, in any task state.
    * **Push configs**: URLs must pass the `PushPolicy`; each task has at
      most `max_push_configs`; configs get ids (``push-1``, ...); responses
      never echo credentials or tokens.
    * **Errors**: unexpected exceptions become a generic internal error.
    """

    def __init__(
        self,
        engine: MemoryEngine,
        *,
        agent_card: AgentCard,
        task_store: TaskStore,
        push_config_store: PushNotificationConfigStore,
        push_sender: PushNotificationSender,
        push_policy: PushPolicy,
        max_push_configs: int = 5,
        validation: ValidationMode = 'log',
    ) -> None:
        super().__init__(
            agent_executor=Mem2AExecutor(engine, validation=validation),
            task_store=task_store,
            agent_card=agent_card,
            push_config_store=push_config_store,
            push_sender=push_sender,
        )
        self.engine = engine
        self.push_policy = push_policy
        self.max_push_configs = max_push_configs
        self.deliveries = Deliveries(engine, self)
        self._configs = push_config_store
        self._turn_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def turn_lock(self, task_id: str) -> asyncio.Lock:
        """The lock that serializes turns of `task_id`."""
        lock = self._turn_locks.get(task_id)
        if lock is None:
            lock = self._turn_locks[task_id] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------ messages
    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        with _no_leaks('SendMessage'):
            await self._admit(params, context)
            task_id = params.message.task_id
            result: Task | Message
            if not task_id:
                result = await super().on_message_send(params, context)
            else:
                async with self.turn_lock(task_id):
                    answered = await self._answered(params, context)
                    if answered is not None:
                        result = answered
                    else:
                        result = await super().on_message_send(params, context)
            return self._visible(result, context) if isinstance(result, Task) else result

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        with _no_leaks('SendStreamingMessage'):
            await self._admit(params, context)
            task_id = params.message.task_id
            async with self.turn_lock(task_id) if task_id else contextlib.nullcontext():
                answered = await self._answered(params, context) if task_id else None
                if answered is not None:
                    yield self._visible(answered, context)
                    return
                async with aclosing(super().on_message_send_stream(params, context)) as events:
                    async for event in events:
                        yield self._visible(event, context) if isinstance(event, Task) else event

    async def _admit(self, params: SendMessageRequest, context: ServerCallContext) -> None:
        if C.EXTENSION_URI not in context.requested_extensions:
            raise ExtensionSupportRequiredError(
                message=f'This memory requires the Mem2A extension. Send the header '
                f'{HTTP_EXTENSION_HEADER}: {C.EXTENSION_URI}',
                data={'uri': C.EXTENSION_URI},
            )
        if IDENTITY_KEY not in context.state:
            raise RuntimeError('An unauthenticated request reached the handler')
        message = params.message
        if message.task_id:
            task = await self._stored(message.task_id, context)  # only for its owner
            # a2a-sdk 1.2.1 gives a follow-up that names only its taskId a new,
            # random contextId (A2A says to infer the task's); its events then
            # carry the wrong contextId and the next turn FAILS the task.
            if not message.context_id:
                message.context_id = task.context_id
        if params.configuration.HasField('task_push_notification_config'):
            config = params.configuration.task_push_notification_config
            await self._admit_push(config, message.task_id, context)

    async def _answered(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | None:
        """The task, if it already has this message (a retry), else None."""
        message_id = params.message.message_id
        task = await self._stored(params.message.task_id, context)
        if message_id and any(
            m.role == Role.ROLE_USER and m.message_id == message_id for m in task.history
        ):
            return apply_history_length(task, params.configuration)
        return None

    # --------------------------------------------------------------- reads
    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        with _no_leaks('GetTask'):
            await super().on_get_task(params, context)  # only for its owner
            await self.deliveries.settle(params.id)  # so the dossier is current
            task = await super().on_get_task(params, context)
            return None if task is None else self._visible(task, context)

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        with _no_leaks('ListTasks'):
            page: ListTasksResponse = await super().on_list_tasks(params, context)
            for task in page.tasks:
                visible = self._visible(task, context)
                if visible is not task:
                    task.CopyFrom(visible)
            return page

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        with _no_leaks('SubscribeToTask'):
            async with aclosing(super().on_subscribe_to_task(params, context)) as events:
                async for event in events:
                    yield self._visible(event, context) if isinstance(event, Task) else event

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        with _no_leaks('CancelTask'):
            await self._stored(params.id, context)  # only for its owner
            async with self.turn_lock(params.id):  # after any update in flight
                task = await super().on_cancel_task(params, context)
            return None if task is None else self._visible(task, context)

    def _visible(self, task: Task, context: ServerCallContext) -> Task:
        """`task` with its dossier filtered by current access (a copy if changed)."""
        viewer = context.state.get(IDENTITY_KEY)
        visible: Task | None = None
        for a, artifact in enumerate(task.artifacts):
            if artifact.artifact_id != C.DOSSIER_ARTIFACT:
                continue
            for p, part in enumerate(artifact.parts):
                if part.media_type != C.DOSSIER or not part.HasField('data'):
                    continue
                dossier = models.Dossier.model_validate(MessageToDict(part.data))
                shown = self.engine.visible_dossier(task.id, dossier, viewer)
                if shown is not dossier:
                    if visible is None:
                        visible = Task()
                        visible.CopyFrom(task)
                    visible.artifacts[a].parts[p].CopyFrom(new_data_part(shown.dump(), C.DOSSIER))
        return visible or task

    async def _stored(self, task_id: str, context: ServerCallContext) -> Task:
        """The stored task, unfiltered, for its owner only (TaskNotFoundError)."""
        task: Task | None = await super().on_get_task(GetTaskRequest(id=task_id), context)
        if task is None:
            raise TaskNotFoundError
        return task

    # --------------------------------------------------------- push configs
    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        with _no_leaks('CreateTaskPushNotificationConfig'):
            await self._stored(params.task_id, context)  # only for its owner
            await self._admit_push(params, params.task_id, context)
            config = await super().on_create_task_push_notification_config(params, context)
            return _without_secrets(config)

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        with _no_leaks('GetTaskPushNotificationConfig'):
            config = await super().on_get_task_push_notification_config(params, context)
            return _without_secrets(config)

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> ListTaskPushNotificationConfigsResponse:
        with _no_leaks('ListTaskPushNotificationConfigs'):
            page = await super().on_list_task_push_notification_configs(params, context)
            return ListTaskPushNotificationConfigsResponse(
                configs=[_without_secrets(config) for config in page.configs],
                next_page_token=page.next_page_token,
            )

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        with _no_leaks('DeleteTaskPushNotificationConfig'):
            await super().on_delete_task_push_notification_config(params, context)

    async def _admit_push(
        self, config: TaskPushNotificationConfig, task_id: str, context: ServerCallContext
    ) -> None:
        """Screen a push config's URL, give it an id, and enforce the per-task cap."""
        if not await self.push_policy.allows(config.url):
            raise InvalidParamsError(
                message=f'Memory does not send push notifications to {config.url}'
            )
        existing = await self._configs.get_info(task_id, context) if task_id else []
        ids = {c.id for c in existing}
        if not config.id:
            config.id = next(f'push-{n}' for n in itertools.count(1) if f'push-{n}' not in ids)
        if config.id not in ids and len(ids) >= self.max_push_configs:
            raise InvalidParamsError(
                message=f'limit-exceeded: a task can have at most {self.max_push_configs} push '
                'notification configs. Delete one first.',
                data={'code': 'limit-exceeded'},
            )


def _without_secrets(config: TaskPushNotificationConfig) -> TaskPushNotificationConfig:
    """A copy without the token and credentials: the caller already has them."""
    copy = TaskPushNotificationConfig()
    copy.CopyFrom(config)
    copy.ClearField('authentication')
    copy.token = ''
    return copy


async def run_internal_turn(
    handler: DefaultRequestHandlerV2,
    task_id: str,
    context_id: str,
    call_context: ServerCallContext,
) -> None:
    """Run one server-initiated turn of an existing task through the SDK.

    a2a-sdk 1.2.x has no public API for this, so we do what
    ``DefaultRequestHandlerV2.on_message_send`` does internally, minus the
    message: get the task's ActiveTask and enqueue a request on it. The SDK
    then orders it after pending client turns, re-reads the task into
    ``context.current_task``, stores every event, sends push notifications
    and fans events out to SubscribeToTask streams. With no message, nothing
    is added to the task's history.

    Callers hold the task's `Mem2ARequestHandler.turn_lock`. If the task
    reaches a terminal state while the turn is queued, the SDK drops the turn
    silently; the executor marks the turns that ran (see `Deliveries`).

    This is the only use of private SDK API in mem2a, hence the <1.3 pin.
    """
    registry = handler._active_task_registry  # private in a2a-sdk 1.2.x
    active = await registry.get_or_create(
        task_id, call_context=call_context, create_task_if_missing=False
    )
    request = RequestContext(call_context=call_context, task_id=task_id, context_id=context_id)
    async with aclosing(active.subscribe(request=request)) as events:
        async for _event in events:
            pass


# ============================================================== deliveries
class Deliveries:
    """Out-of-band delivery: engine change notifications become internal turns.

    Refreshes are coalesced per task (at most one running and one queued).
    The engine recomputes the dossier inside the turn, so access is checked
    at delivery time, and an unchanged dossier produces nothing.
    """

    def __init__(self, engine: MemoryEngine, handler: Mem2ARequestHandler) -> None:
        self._engine = engine
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._again: set[str] = set()
        self._running: set[asyncio.Task[None]] = set()
        engine.on_change(self.notify)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Deliver on `loop` (the server's), even if notified from another thread."""
        self._loop = loop

    def notify(self, task_ids: list[str]) -> None:
        """Engine change listener: schedule a refresh of each task."""
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop or running
        if loop is None:
            logger.warning('No event loop yet; not delivering updates for %s', task_ids)
        elif loop is running:
            self._schedule_refreshes(task_ids)
        else:
            loop.call_soon_threadsafe(self._schedule_refreshes, task_ids)

    async def settle(self, task_id: str) -> None:
        """Wait until any update being prepared for `task_id` is delivered."""
        pending = self._refreshing.get(task_id)
        if pending is not None:
            await asyncio.wait({pending})

    async def sweep(self) -> list[str]:
        """End every task past its ``expiresAt``; returns the task ids."""
        due = self._engine.due_for_expiry()
        await asyncio.gather(*(self._spawn(self._turn(task_id, 'expire')) for task_id in due))
        return due

    async def wait_idle(self) -> None:
        """Wait until every scheduled delivery has run."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._running):
            task.cancel()
        await asyncio.gather(*list(self._running), return_exceptions=True)

    def _schedule_refreshes(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            if task_id in self._refreshing:
                self._again.add(task_id)  # refresh once more when the current one ends
            else:
                self._refreshing[task_id] = self._spawn(self._refresh_until_settled(task_id))

    async def _refresh_until_settled(self, task_id: str) -> None:
        try:
            while True:
                self._again.discard(task_id)
                await self._turn(task_id, 'refresh')
                if task_id not in self._again:
                    return
        finally:
            self._refreshing.pop(task_id, None)

    async def _turn(self, task_id: str, kind: InternalTurn) -> None:
        async with self._handler.turn_lock(task_id):
            record = self._engine.task(task_id)
            if record is None or not record.open:
                return
            call_context = ServerCallContext(
                user=Mem2AUser(record.identity),  # the task's owner
                requested_extensions={C.EXTENSION_URI},
                state={IDENTITY_KEY: record.identity, TURN_KEY: kind},
            )
            try:
                await run_internal_turn(self._handler, task_id, record.context_id, call_context)
            except (TaskNotFoundError, UnsupportedOperationError):
                pass  # the task ended before the turn could start
            except Exception:
                logger.exception('Internal %s turn failed for task %s', kind, task_id)
                return
        if not call_context.state.get(TURN_RAN_KEY):
            logger.info('Task %s ended before its %s could be delivered', task_id, kind)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task


# ============================================================== middleware
class AuthMiddleware:
    """Authenticates requests to the A2A endpoint.

    Missing or invalid credentials get HTTP 401 with a ``WWW-Authenticate:
    Bearer`` challenge and a JSON-RPC error body. The Agent Card stays public.
    """

    def __init__(self, app: ASGIApp, authenticator: Authenticator, paths: Collection[str]) -> None:
        self.app = app
        self.authenticator = authenticator
        self.paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http' or scope['path'] not in self.paths:
            await self.app(scope, receive, send)
            return
        try:
            identity = await self.authenticator.authenticate(HTTPConnection(scope))
        except AuthenticationError as error:
            response = JSONResponse(
                {
                    'jsonrpc': '2.0',
                    'id': None,
                    'error': {'code': AUTH_ERROR_CODE, 'message': f'Unauthenticated: {error}'},
                },
                status_code=401,
                headers={'WWW-Authenticate': 'Bearer realm="mem2a"'},
            )
            await response(scope, receive, send)
            return
        scope[IDENTITY_KEY] = identity
        await self.app(scope, receive, send)


class ExtensionEchoMiddleware:
    """Adds ``A2A-Extensions: <activated URIs>`` to responses (spec 6.4).

    `Mem2AContextBuilder` records activation in a per-request set that this
    middleware puts in the ASGI scope. Works for JSON and SSE responses: the
    SDK builds the call context before any response starts.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        activated: set[str] = set()
        scope[ACTIVATED_KEY] = activated

        async def send_with_header(message: ASGIMessage) -> None:
            if message['type'] == 'http.response.start' and activated:
                MutableHeaders(scope=message).append(
                    HTTP_EXTENSION_HEADER, ','.join(sorted(activated))
                )
            await send(message)

        await self.app(scope, receive, send_with_header)


class Mem2AContextBuilder(DefaultServerCallContextBuilder):
    """The default builder, plus the caller's `Identity` and Mem2A activation."""

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        identity = request.scope.get(IDENTITY_KEY)
        if isinstance(identity, Identity):
            context.user = Mem2AUser(identity)
            context.state[IDENTITY_KEY] = identity
        activated = request.scope.get(ACTIVATED_KEY)
        if isinstance(activated, set) and C.EXTENSION_URI in context.requested_extensions:
            activated.add(C.EXTENSION_URI)
        return context


# ===================================================================== app
@dataclass
class Mem2AServer:
    """A memory ready to serve: the ASGI app plus handles for operators and tests."""

    app: Starlette
    engine: MemoryEngine
    handler: Mem2ARequestHandler
    push: QueuedPushSender
    card: AgentCard

    @property
    def deliveries(self) -> Deliveries:
        return self.handler.deliveries

    async def wait_idle(self) -> None:
        """Wait until every pending update is delivered, pushes included."""
        await self.deliveries.wait_idle()
        await self.push.wait_idle()

    async def sweep(self) -> list[str]:
        """Expire overdue tasks now; returns the task ids."""
        return await self.deliveries.sweep()


def create_app(
    engine: MemoryEngine,
    *,
    url: str,
    authenticator: Authenticator,
    card: AgentCard | None = None,
    rpc_path: str = RPC_PATH,
    push_origins: Iterable[str] | None = None,
    push_url_validator: UrlValidator | None = validate_push_notification_url,
    push_client: httpx.AsyncClient | None = None,
    push_retries: int = 3,
    push_backoff: float = 0.25,
    max_push_configs: int = 5,
    validation: ValidationMode = 'log',
    sweep_interval: float | None = 60.0,
    dev_admin: bool = False,
) -> Mem2AServer:
    """Build the Starlette app that serves `engine` at ``url + rpc_path``.

    Args:
        url: Public base URL, used in the Agent Card.
        authenticator: Turns requests into identities (see `mem2a.auth`).
        card: The Agent Card; by default `build_agent_card` with the engine's
            watch timeout.
        push_origins: If set, the only webhook origins memory will call, for
            example ``['https://agents.example.com']``; ``http://127.0.0.1:*``
            allows any port (see `LOCALHOST_ORIGINS`). Other URLs get
            InvalidParams.
        push_url_validator: Without `push_origins`, screens webhook URLs. The
            default refuses private and loopback addresses (SSRF, spec 11.4);
            None allows any URL.
        push_client: The HTTP client that delivers push notifications. The app
            closes it on shutdown.
        push_retries, push_backoff: Retries per push notification, and the
            first backoff in seconds (it doubles each time).
        max_push_configs: Push notification configs allowed per task.
        validation: What to do if a payload memory sends breaks its schema:
            ``raise`` (tests; the turn fails), ``log`` (default) or ``off``.
        sweep_interval: Seconds between watch-expiry sweeps; None disables them.
        dev_admin: Also serve the unauthenticated ``/dev`` admin routes of
            `mem2a.admin`. For sandboxes only.
    """
    card = card or build_agent_card(f'{url}{rpc_path}', watch_timeout=engine.watch_timeout)
    policy = PushPolicy(push_origins, push_url_validator)
    push_configs = InMemoryPushNotificationConfigStore()
    push_client = push_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    push = QueuedPushSender(
        push_configs, push_client, policy, retries=push_retries, backoff=push_backoff
    )
    handler = Mem2ARequestHandler(
        engine,
        agent_card=card,
        task_store=InMemoryTaskStore(),
        push_config_store=push_configs,
        push_sender=push,
        push_policy=policy,
        max_push_configs=max_push_configs,
        validation=validation,
    )
    deliveries = handler.deliveries

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        deliveries.bind(asyncio.get_running_loop())
        sweeper = None
        if sweep_interval and engine.watch_timeout is not None:
            sweeper = asyncio.create_task(_sweep_forever(deliveries, sweep_interval))
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            await deliveries.aclose()
            await handler.aclose()
            await push.aclose()
            await push_client.aclose()

    routes: list[BaseRoute] = [
        *create_agent_card_routes(agent_card=card),
        *create_jsonrpc_routes(
            request_handler=handler, rpc_url=rpc_path, context_builder=Mem2AContextBuilder()
        ),
    ]
    if dev_admin:
        logger.warning(
            'Dev admin routes are ON: anyone who can reach %s/dev can read and change this '
            'memory without authenticating. Never expose them.',
            url,
        )
        routes += admin_routes(engine, deliveries)
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(ExtensionEchoMiddleware),
            Middleware(AuthMiddleware, authenticator=authenticator, paths={rpc_path}),
        ],
        lifespan=lifespan,
    )
    return Mem2AServer(app, engine, handler, push, card)


def listen_socket(host: str = '127.0.0.1', port: int = 0) -> socket.socket:
    """A listening TCP socket to pass to uvicorn (``sockets=[...]``), so the
    port is known before serving; port 0 picks a free one. Connections made
    before uvicorn starts wait in the backlog.

    It sets TCP_NODELAY, which asyncio skips on sockets it didn't create:
    without it, every response stalls about 40 ms (Nagle's algorithm meets
    delayed ACKs).
    """
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.bind((host, port))
        sock.listen(2048)  # uvicorn's default backlog
    except OSError:
        sock.close()
        raise
    return sock


async def _sweep_forever(deliveries: Deliveries, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await deliveries.sweep()
        except Exception:
            logger.exception('Watch-expiry sweep failed')
