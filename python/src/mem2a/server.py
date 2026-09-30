# SPDX-License-Identifier: Apache-2.0
"""Serve a `MemoryEngine` as an A2A v1.0 JSON-RPC agent that speaks Mem2A v0.1.

Built on a2a-sdk 1.2.x:

* `Mem2AExecutor` (an ``AgentExecutor``) turns engine `Reply` objects into A2A
  events: the dossier / receipt artifacts first, then one status message
  carrying the phase.
* `Mem2ARequestHandler` (a ``DefaultRequestHandlerV2``) rejects SendMessage
  without the Mem2A activation header (-32008) before a task exists, and
  fills in a missing ``contextId`` on follow-ups.
* `Deliveries` listens to the engine and runs *internal turns* through the
  SDK (`run_internal_turn`), so out-of-band updates are persisted, pushed to
  webhooks and streamed to SubscribeToTask exactly like client-driven turns.
* `AuthMiddleware` authenticates every JSON-RPC request (HTTP 401 otherwise);
  `ExtensionEchoMiddleware` echoes activated extensions in the response's
  ``A2A-Extensions`` header, which the SDK does not do.

`create_app` wires it all into a Starlette app.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Coroutine
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from a2a.auth.user import User
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
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
from mem2a import constants as C
from mem2a.auth import AuthenticationError, Authenticator, DevTokenAuthenticator, Identity
from mem2a.card import build_agent_card
from mem2a.engine import MemoryEngine, Payloads, Reply
from mem2a.models import Payload
from mem2a.validation import ValidationMode, check_outgoing


logger = logging.getLogger(__name__)

RPC_PATH = '/a2a/jsonrpc'

# Keys in the ASGI scope and in ServerCallContext.state. `state` is built on
# the server only, so clients cannot set these.
IDENTITY_KEY = 'mem2a.identity'
INTERNAL_TURN_KEY = 'mem2a.internal_turn'
ACTIVATED_KEY = 'a2a.activated_extensions'

#: JSON-RPC code in the body of HTTP 401 responses. A2A defines no code for
#: authentication errors; -32000 is the generic "server error" slot.
AUTH_ERROR_CODE = -32000

InternalTurn = Literal['refresh', 'expire']

TASK_STATE: dict[str, TaskState] = {
    'question': TaskState.TASK_STATE_INPUT_REQUIRED,
    'awaiting-commit': TaskState.TASK_STATE_INPUT_REQUIRED,
    'committed': TaskState.TASK_STATE_COMPLETED,
    'refused': TaskState.TASK_STATE_REJECTED,
    'expired': TaskState.TASK_STATE_CANCELED,
    'canceled': TaskState.TASK_STATE_CANCELED,
}


class Mem2AUser(User):
    """The SDK scopes tasks and push configs by ``user_name``. A task belongs
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
    Text and other parts are ignored for decisions."""
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

    def __init__(self, engine: MemoryEngine, *, validation: ValidationMode = 'raise'):
        self.engine = engine
        self.validation = validation

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id or '', context.context_id or ''
        state = context.call_context.state
        identity: Identity = state[IDENTITY_KEY]
        turn: InternalTurn | None = state.get(INTERNAL_TURN_KEY)

        reply: Reply | None
        if turn == 'refresh':
            reply = self.engine.refresh(task_id)
        elif turn == 'expire':
            reply = self.engine.expire(task_id)
        elif context.current_task is None:
            # Publish the Task first, with the intent in its history.
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
        updater = TaskUpdater(event_queue, context.task_id or '', context.context_id or '')
        reply = self.engine.cancel(context.task_id or '') or Reply(
            phase='canceled', text='Canceled.'
        )
        await self.publish(reply, updater)

    async def publish(self, reply: Reply, updater: TaskUpdater) -> None:
        """Artifacts first (a blocking SendMessage returns at the status), then
        one status message: text + at most one Mem2A payload + the phase."""
        for kind, artifact_id in (('dossier', C.DOSSIER_ARTIFACT), ('receipt', C.RECEIPT_ARTIFACT)):
            payload = getattr(reply, kind)
            if payload is not None:
                await updater.add_artifact(
                    parts=[self._part(kind, payload)],
                    artifact_id=artifact_id,
                    name=artifact_id,
                    extensions=[C.EXTENSION_URI],
                    append=False,  # same artifactId, append false: replace
                    last_chunk=True,
                )
        parts = [Part(text=reply.text)]
        for kind in ('question', 'update', 'error'):
            payload = getattr(reply, kind)
            if payload is not None:
                parts.append(self._part(kind, payload))
        message = updater.new_agent_message(parts, metadata={C.PHASE_KEY: reply.phase})
        message.extensions.append(C.EXTENSION_URI)
        await updater.update_status(TASK_STATE[reply.phase], message=message)

    def _part(self, kind: str, payload: Payload) -> Part:
        data = payload.dump()
        check_outgoing(kind, data, self.validation)
        return new_data_part(data, media_type=C.MEDIA_TYPE_OF[kind])  # type: ignore[index]


# ================================================================= handler
class Mem2ARequestHandler(DefaultRequestHandlerV2):
    """DefaultRequestHandlerV2 plus the Mem2A admission rules."""

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        await self._admit(params, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncIterator[Any]:
        await self._admit(params, context)
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def _admit(self, params: SendMessageRequest, context: ServerCallContext) -> None:
        # Checked here, before the SDK creates a task: raising from the
        # executor would leave a FAILED task behind.
        if C.EXTENSION_URI not in context.requested_extensions:
            raise ExtensionSupportRequiredError(
                message='This memory requires the Mem2A extension; send the '
                f'header {HTTP_EXTENSION_HEADER}: {C.EXTENSION_URI}',
                data={'uri': C.EXTENSION_URI},
            )
        if IDENTITY_KEY not in context.state:
            raise PermissionError('Unauthenticated request reached the handler')
        # a2a-sdk 1.2.1 generates a new contextId for a follow-up that names
        # only its taskId (spec 3.4.3 says to infer it); events then carry
        # the wrong contextId and the next turn fails. Infer it here.
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

    a2a-sdk 1.2.x has no public API for this. We do what
    ``DefaultRequestHandlerV2.on_message_send`` does internally, minus the
    message: get the task's ActiveTask and enqueue a request on it. The SDK
    then serializes it with client turns, re-reads the task into
    ``context.current_task``, persists every event, POSTs push notifications
    and fans events out to SubscribeToTask streams. No message means nothing
    is added to the task's history.

    This is the only use of private SDK API in mem2a (hence the <1.3 pin).
    """
    registry = handler._active_task_registry  # noqa: SLF001 - private in a2a-sdk 1.2.x
    active = await registry.get_or_create(
        task_id, call_context=call_context, create_task_if_missing=False
    )
    request = RequestContext(call_context=call_context, task_id=task_id, context_id=context_id)
    async with aclosing(active.subscribe(request=request)) as events:
        async for _event in events:
            pass


# ============================================================== deliveries
class Deliveries:
    """Out-of-band delivery: engine change notifications -> internal turns.

    Refreshes are coalesced per task (at most one running, one queued). The
    engine recomputes the dossier inside the turn, so permissions are
    re-checked at delivery time and unchanged dossiers produce nothing.
    """

    def __init__(self, engine: MemoryEngine, handler: DefaultRequestHandlerV2) -> None:
        self._engine = engine
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refreshing: dict[str, asyncio.Task[None]] = {}
        self._again: set[str] = set()
        self._running: set[asyncio.Task[None]] = set()
        engine.on_change(self.notify)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def notify(self, task_ids: list[str]) -> None:
        """Engine listener. Safe to call from another thread."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop or running
        if loop is None:
            logger.warning('No event loop yet; not delivering updates for %s', task_ids)
        elif running is loop:
            for task_id in task_ids:
                self._schedule_refresh(task_id)
        else:
            loop.call_soon_threadsafe(lambda: [self._schedule_refresh(t) for t in task_ids])

    def expire(self, task_id: str) -> asyncio.Task[None]:
        return self._spawn(self._turn(task_id, 'expire'))

    async def sweep(self) -> list[str]:
        """End every watch past its ``expiresAt``; returns the task ids."""
        due = self._engine.due_for_expiry()
        await asyncio.gather(*(self.expire(task_id) for task_id in due))
        return due

    async def wait_idle(self) -> None:
        """Wait until every scheduled delivery has run (useful in tests)."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._running):
            task.cancel()
        await asyncio.gather(*list(self._running), return_exceptions=True)

    def _schedule_refresh(self, task_id: str) -> None:
        if task_id in self._refreshing:
            self._again.add(task_id)
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
        record = self._engine.task(task_id)
        if record is None or not record.open:
            return
        call_context = ServerCallContext(
            user=Mem2AUser(record.identity),
            requested_extensions={C.EXTENSION_URI},
            state={IDENTITY_KEY: record.identity, INTERNAL_TURN_KEY: kind},
        )
        try:
            await run_internal_turn(self._handler, task_id, record.context_id, call_context)
        except (TaskNotFoundError, UnsupportedOperationError):
            logger.debug('Task %s finished before its %s turn ran', task_id, kind)
        except Exception:
            logger.exception('Internal %s turn failed for task %s', kind, task_id)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(coro)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task


# ============================================================== middleware
class AuthMiddleware:
    """Authenticates requests to the A2A endpoint(s).

    Missing or invalid credentials get HTTP 401 with a ``WWW-Authenticate:
    Bearer`` challenge (A2A: use the binding's native error) and a JSON-RPC
    error body. The Agent Card stays public.
    """

    def __init__(self, app: ASGIApp, authenticator: Authenticator, paths: Collection[str]):
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
    """Adds ``A2A-Extensions: <activated URIs>`` to responses.

    `Mem2AContextBuilder` records activation in a per-request set that this
    middleware put in the ASGI scope. Works for JSON and SSE responses (the
    SDK builds the context before any response starts).
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
    """Default builder + the caller's `Identity` and Mem2A activation."""

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


# ================================================================== app
@dataclass
class Mem2AServer:
    """A running memory: the ASGI app plus handles for operators and tests."""

    app: Starlette
    engine: MemoryEngine
    handler: Mem2ARequestHandler
    deliveries: Deliveries
    card: AgentCard

    async def wait_idle(self) -> None:
        await self.deliveries.wait_idle()

    async def sweep(self) -> list[str]:
        return await self.deliveries.sweep()


def create_app(
    engine: MemoryEngine,
    *,
    url: str,
    authenticator: Authenticator,
    card: AgentCard | None = None,
    rpc_path: str = RPC_PATH,
    push_url_validator: Callable[[str], Awaitable[bool]] | None = None,
    validation: ValidationMode = 'raise',
    sweep_interval: float | None = 60.0,
) -> Mem2AServer:
    """Build the Starlette app serving `engine` at ``url + rpc_path``.

    Args:
        url: Public base URL, used in the Agent Card.
        authenticator: Turns requests into identities (see `mem2a.auth`).
        push_url_validator: Screens webhook URLs. Pass
            ``a2a.utils.push_url_validator.validate_push_notification_url``
            in production; the default accepts any URL (including localhost).
        validation: What to do if a payload memory sends breaks its schema.
        sweep_interval: Seconds between expiry sweeps; None disables them.
    """
    card = card or build_agent_card(f'{url}{rpc_path}', watch_timeout=engine.watch_timeout)
    push_configs = InMemoryPushNotificationConfigStore()
    push_client = httpx.AsyncClient(timeout=10)
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
                request_handler=handler,
                rpc_url=rpc_path,
                context_builder=Mem2AContextBuilder(),
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
            logger.exception('Expiry sweep failed')


def main() -> None:
    """``python -m mem2a.server``: an empty memory with dev tokens, for poking at."""
    import uvicorn

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    server = create_app(
        MemoryEngine(),
        url=f'http://{args.host}:{args.port}',
        authenticator=DevTokenAuthenticator(),
    )
    uvicorn.run(server.app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
