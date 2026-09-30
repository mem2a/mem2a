# SPDX-License-Identifier: Apache-2.0
"""Serve a `MemoryEngine` as an A2A v1.0 JSON-RPC agent that speaks Mem2A v0.1.

Built on a2a-sdk 1.2.x:

* `Mem2AExecutor` (an ``AgentExecutor``) turns engine `Reply` objects into A2A
  events: the dossier or receipt artifact first, then one status message
  that carries the phase.
* `Mem2ARequestHandler` (a ``DefaultRequestHandlerV2``) refuses SendMessage
  without Mem2A activation (-32008) before a task exists, fills in a missing
  ``contextId`` on follow-ups, and runs one turn at a time per task.
* `Deliveries` listens to the engine and runs *internal turns* through the
  SDK (`run_internal_turn`), so out-of-band updates are stored, pushed to
  webhooks and streamed to SubscribeToTask exactly like client-driven turns.
* `AuthMiddleware` authenticates every JSON-RPC request (HTTP 401 otherwise).
  `ExtensionEchoMiddleware` echoes the activated extensions in the
  ``A2A-Extensions`` response header, which the SDK does not do.

`create_app` wires it all into a Starlette app.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import weakref
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
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
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
    TaskUpdater,
)
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import (
    ExtensionSupportRequiredError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as ASGIMessage

from mem2a import constants as C
from mem2a.auth import AuthenticationError, Authenticator, DevTokenAuthenticator, Identity
from mem2a.card import build_agent_card
from mem2a.engine import MemoryEngine, Payloads, Reply
from mem2a.models import Payload
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

InternalTurn = Literal['refresh', 'expire']

TASK_STATE: dict[C.Phase, TaskState] = {
    'question': TaskState.TASK_STATE_INPUT_REQUIRED,
    'awaiting-commit': TaskState.TASK_STATE_INPUT_REQUIRED,
    'committed': TaskState.TASK_STATE_COMPLETED,
    'refused': TaskState.TASK_STATE_REJECTED,
    'expired': TaskState.TASK_STATE_CANCELED,
    'canceled': TaskState.TASK_STATE_CANCELED,
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


# ================================================================ executor
class Mem2AExecutor(AgentExecutor):
    """Runs the engine for each turn and publishes what it replies."""

    def __init__(self, engine: MemoryEngine, *, validation: ValidationMode = 'log') -> None:
        self.engine = engine
        self.validation = validation

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id or '', context.context_id or ''
        state = context.call_context.state
        identity: Identity = state[IDENTITY_KEY]
        turn: InternalTurn | None = state.get(TURN_KEY)

        reply: Reply | None
        if turn is not None:
            state[TURN_RAN_KEY] = True
            reply = (
                self.engine.refresh(task_id) if turn == 'refresh' else self.engine.expire(task_id)
            )
        elif context.current_task is None:
            # A new task: publish it first, with the intent in its history.
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message] if context.message else [],
                )
            )
            reply = self.engine.negotiate(
                task_id, context_id, identity, mem2a_parts(context.message)
            )
        else:
            reply = self.engine.respond(task_id, identity, mem2a_parts(context.message))

        if reply is not None:
            await self.publish(reply, TaskUpdater(event_queue, task_id, context_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """CancelTask: stop watching and end the task (phase ``canceled``)."""
        task_id = context.task_id or ''
        reply = self.engine.cancel(task_id) or Reply(phase='canceled', text='Canceled.')
        await self.publish(reply, TaskUpdater(event_queue, task_id, context.context_id or ''))

    async def publish(self, reply: Reply, updater: TaskUpdater) -> None:
        """Artifacts first (a blocking SendMessage returns at the status), then
        one status message: text, the Mem2A payloads, and the phase."""
        artifacts: dict[C.PayloadKind, Payload | None] = {
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
        payloads: dict[C.PayloadKind, Payload | None] = {
            'question': reply.question,
            'update': reply.update,
            'error': reply.error,
        }
        parts = [Part(text=reply.text)]
        parts += [self._part(kind, p) for kind, p in payloads.items() if p is not None]
        message = updater.new_agent_message(parts, metadata={C.PHASE_KEY: reply.phase})
        message.extensions.append(C.EXTENSION_URI)
        await updater.update_status(TASK_STATE[reply.phase], message=message)

    def _part(self, kind: C.PayloadKind, payload: Payload) -> Part:
        data = payload.dump()
        check_outgoing(kind, data, self.validation)
        return new_data_part(data, media_type=C.MEDIA_TYPE_OF[kind])


# ================================================================= handler
class Mem2ARequestHandler(DefaultRequestHandlerV2):
    """DefaultRequestHandlerV2 plus the Mem2A admission rules, and one turn
    at a time per task.

    Why the per-task lock: a2a-sdk 1.2.x answers a blocking SendMessage with
    the first INPUT_REQUIRED (or terminal) event it sees on the task's shared
    event stream, even if that event belongs to an earlier turn still in
    flight, such as an update memory is delivering. The commit would then be
    answered with the update's status and run unseen in the background.
    Client follow-ups and memory's internal turns (`Deliveries`) therefore
    take `turn_lock` for the whole turn.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._turn_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def turn_lock(self, task_id: str) -> asyncio.Lock:
        """The lock that serializes turns of `task_id`."""
        lock = self._turn_locks.get(task_id)
        if lock is None:
            lock = self._turn_locks[task_id] = asyncio.Lock()
        return lock

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        await self._admit(params, context)
        result: Task | Message
        if not params.message.task_id:  # a new task: no earlier turn to wait for
            result = await super().on_message_send(params, context)
        else:
            async with self.turn_lock(params.message.task_id):
                result = await super().on_message_send(params, context)
        return result

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        await self._admit(params, context)
        task_id = params.message.task_id
        async with self.turn_lock(task_id) if task_id else contextlib.nullcontext():
            async for event in super().on_message_send_stream(params, context):
                yield event

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        async with self.turn_lock(params.id):  # after any update in flight
            result: Task | None = await super().on_cancel_task(params, context)
        return result

    async def _admit(self, params: SendMessageRequest, context: ServerCallContext) -> None:
        # Checked before the SDK creates a task: raising from the executor
        # would leave a FAILED task behind (spec 6.3: SHOULD NOT create one).
        if C.EXTENSION_URI not in context.requested_extensions:
            raise ExtensionSupportRequiredError(
                message=f'This memory requires the Mem2A extension. Send the header '
                f'{HTTP_EXTENSION_HEADER}: {C.EXTENSION_URI}',
                data={'uri': C.EXTENSION_URI},
            )
        if IDENTITY_KEY not in context.state:
            raise RuntimeError('An unauthenticated request reached the handler')
        # a2a-sdk 1.2.1 gives a follow-up that names only its taskId a new,
        # random contextId (A2A says to infer the task's). Its events then
        # carry the wrong contextId and the next turn FAILS the task.
        message = params.message
        if message.task_id and not message.context_id:
            task = await self.on_get_task(GetTaskRequest(id=message.task_id), context)
            if task is not None:
                message.context_id = task.context_id


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
    ``context.current_task``, stores every event, POSTs push notifications
    and fans events out to SubscribeToTask streams. With no message, nothing
    is added to the task's history.

    Callers hold the task's `Mem2ARequestHandler.turn_lock`. If the task
    reaches a terminal state while the turn is queued, the SDK drops the turn
    silently; the executor marks the turns that ran (see `Deliveries`).

    This is the only use of private SDK API in mem2a, hence the <1.3 pin.
    """
    registry = handler._active_task_registry
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

    async def sweep(self) -> list[str]:
        """End every watch past its ``expiresAt``; returns the task ids."""
        due = self._engine.due_for_expiry()
        await asyncio.gather(*(self._spawn(self._turn(task_id, 'expire')) for task_id in due))
        return due

    async def wait_idle(self) -> None:
        """Wait until every scheduled delivery has run (useful in tests)."""
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
    deliveries: Deliveries
    card: AgentCard

    async def wait_idle(self) -> None:
        """Wait until every pending update has been delivered."""
        await self.deliveries.wait_idle()

    async def sweep(self) -> list[str]:
        """Expire overdue watches now; returns the task ids."""
        return await self.deliveries.sweep()


def create_app(
    engine: MemoryEngine,
    *,
    url: str,
    authenticator: Authenticator,
    card: AgentCard | None = None,
    rpc_path: str = RPC_PATH,
    push_url_validator: Callable[[str], Awaitable[bool]] | None = None,
    push_client: httpx.AsyncClient | None = None,
    validation: ValidationMode = 'log',
    sweep_interval: float | None = 60.0,
) -> Mem2AServer:
    """Build the Starlette app that serves `engine` at ``url + rpc_path``.

    Args:
        url: Public base URL, used in the Agent Card.
        authenticator: Turns requests into identities (see `mem2a.auth`).
        card: The Agent Card; by default `build_agent_card` with the engine's
            watch timeout.
        push_url_validator: Screens webhook URLs against SSRF (spec 11.4). In
            production pass ``a2a.utils.push_url_validator.
            validate_push_notification_url``; the default accepts any URL,
            including localhost.
        push_client: The HTTP client that delivers push notifications. The app
            closes it on shutdown.
        validation: What to do if a payload memory sends breaks its schema:
            ``raise`` (tests), ``log`` (default) or ``off``.
        sweep_interval: Seconds between watch-expiry sweeps; None disables them.
    """
    card = card or build_agent_card(f'{url}{rpc_path}', watch_timeout=engine.watch_timeout)
    push_configs = InMemoryPushNotificationConfigStore()
    push_client = push_client or httpx.AsyncClient(timeout=10)
    handler = Mem2ARequestHandler(
        agent_executor=Mem2AExecutor(engine, validation=validation),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=push_configs,
        push_sender=BasePushNotificationSender(
            httpx_client=push_client,
            config_store=push_configs,
            push_url_validator=push_url_validator,
        ),
        push_url_validator=push_url_validator,
    )
    deliveries = Deliveries(engine, handler)

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
            await push_client.aclose()

    app = Starlette(
        routes=[
            *create_agent_card_routes(agent_card=card),
            *create_jsonrpc_routes(
                request_handler=handler, rpc_url=rpc_path, context_builder=Mem2AContextBuilder()
            ),
        ],
        middleware=[
            Middleware(ExtensionEchoMiddleware),
            Middleware(AuthMiddleware, authenticator=authenticator, paths={rpc_path}),
        ],
        lifespan=lifespan,
    )
    return Mem2AServer(app, engine, handler, deliveries, card)


async def _sweep_forever(deliveries: Deliveries, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await deliveries.sweep()
        except Exception:
            logger.exception('Watch-expiry sweep failed')


def main() -> None:
    """Run an empty memory with dev tokens: ``python -m mem2a.server``."""
    import uvicorn

    parser = argparse.ArgumentParser(description='Run an empty Mem2A memory (dev tokens).')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    server = create_app(
        MemoryEngine(), url=f'http://{args.host}:{args.port}', authenticator=DevTokenAuthenticator()
    )
    uvicorn.run(server.app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
