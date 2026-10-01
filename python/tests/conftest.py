# SPDX-License-Identifier: Apache-2.0
"""Test harness: a memory and a webhook receiver on real localhost sockets.

Both run under uvicorn as tasks in the test's event loop, so tests can
change the memory's content directly (as an operator would) and watch the
updates arrive. Real sockets are needed because httpx's ASGI transport
buffers whole responses, which would hang on an open SubscribeToTask stream.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mem2a import DevTokenAuthenticator, Mem2AClient, MemoryEngine, create_app
from mem2a.constants import EXTENSION_URI
from mem2a.server import LOCALHOST_ORIGINS, RPC_PATH, Mem2AServer, listen_socket


sys.path.insert(0, str(Path(__file__).parent))

import wire
from stories import TOM, seeded_engine


class Recorder:
    """An append-only list you can wait on."""

    def __init__(self) -> None:
        self.items: list[Any] = []
        self._changed = asyncio.Condition()

    async def add(self, item: Any) -> None:
        async with self._changed:
            self.items.append(item)
            self._changed.notify_all()

    async def wait_for(self, predicate: Callable[[list[Any]], bool], timeout: float = 5) -> None:
        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(lambda: predicate(self.items)), timeout)


def checked(item: Any, problems: list[str]) -> Any:
    """Apply the wire rules, recording a violation instead of raising it."""
    try:
        wire.check(item)
    except AssertionError as error:
        problems.append(repr(error))
    return item


class Webhook(Recorder):
    """An acting agent's push endpoint: records each POST's headers and body.

    Tests can script it: while `gate` is set and not open, every POST waits;
    then `statuses` are answered first, one per POST (a 3xx redirects to
    /elsewhere).
    """

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.statuses: list[int] = []
        self.gate: asyncio.Event | None = None
        self.attempts = 0
        self.app = Starlette(
            routes=[
                Route('/hook', self._receive, methods=['POST']),
                Route('/elsewhere', self._receive, methods=['POST']),
            ]
        )

    async def _receive(self, request: Request) -> Response:
        self.attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.statuses:
            status = self.statuses.pop(0)
            headers = {'Location': '/elsewhere'} if 300 <= status < 400 else None
            return Response(status_code=status, headers=headers)
        body = checked(await request.json(), self.problems)
        headers = {k.lower(): v for k, v in request.headers.items()}
        await self.add({'path': request.url.path, 'headers': headers, 'body': body})
        return JSONResponse({})

    def bodies(self) -> list[dict[str, Any]]:
        return [call['body'] for call in self.items]


class Served:
    """An ASGI app under uvicorn on a socket bound up front (no port races)."""

    def __init__(self) -> None:
        self._socket = listen_socket()
        self.url = f'http://127.0.0.1:{self._socket.getsockname()[1]}'

    async def start(self, app: Any) -> None:
        config = uvicorn.Config(app, log_level='warning', timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
        while not self._server.started:
            if self._task.done():
                self._task.result()  # raise the startup error
            await asyncio.sleep(0.005)

    async def stop(self) -> None:
        self._server.should_exit = True
        await asyncio.wait_for(self._task, 10)
        self._socket.close()


@dataclass
class Memory:
    """A running memory plus what the tests use to talk to it."""

    server: Mem2AServer
    url: str
    webhook: Webhook
    webhook_url: str
    http: httpx.AsyncClient  # for raw JSON-RPC
    clients: list[Mem2AClient] = field(default_factory=list)
    background: list[asyncio.Task[Any]] = field(default_factory=list)
    #: Wire-rule violations seen in background streams.
    problems: list[str] = field(default_factory=list)

    @property
    def engine(self) -> MemoryEngine:
        return self.server.engine

    @property
    def rpc_url(self) -> str:
        return f'{self.url}{RPC_PATH}'

    async def client(self, token: str) -> Mem2AClient:
        http = httpx.AsyncClient(trust_env=False, timeout=10)
        client = await Mem2AClient.connect(self.url, token=token, http=http)
        self.clients.append(client)
        return client

    def collect(self, stream: AsyncIterator[Any]) -> Recorder:
        """Drain `stream` into a Recorder in the background."""
        recorder = Recorder()

        async def drain() -> None:
            async for item in stream:
                await recorder.add(checked(item, self.problems))

        self.background.append(asyncio.create_task(drain()))
        return recorder

    async def rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        token: str | None = TOM,
        activate: bool = True,
    ) -> httpx.Response:
        """A raw JSON-RPC request (A2A-Version 1.0 is required)."""
        headers = {'A2A-Version': '1.0'}
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        if activate:
            headers['A2A-Extensions'] = EXTENSION_URI
        body = {'jsonrpc': '2.0', 'id': '1', 'method': method, 'params': params}
        return await self.http.post(self.rpc_url, json=body, headers=headers)

    async def sse(
        self, method: str, params: dict[str, Any], *, token: str = TOM, events: int = 1
    ) -> list[dict[str, Any]]:
        """The first `events` results of a raw streaming JSON-RPC request."""
        headers = {
            'A2A-Version': '1.0',
            'A2A-Extensions': EXTENSION_URI,
            'Authorization': f'Bearer {token}',
            'Accept': 'text/event-stream',
        }
        body = {'jsonrpc': '2.0', 'id': '1', 'method': method, 'params': params}
        results: list[dict[str, Any]] = []
        async with self.http.stream('POST', self.rpc_url, json=body, headers=headers) as response:
            if not response.headers['content-type'].startswith('text/event-stream'):
                return [json.loads(await response.aread())]
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    results.append(wire.check(json.loads(line[5:])))
                    if len(results) == events:
                        break
        return results

    async def settle(self) -> None:
        """Wait until memory has delivered every pending update."""
        await self.server.wait_idle()


MakeMemory = Callable[..., Awaitable[Memory]]


@pytest_asyncio.fixture
async def make_memory() -> AsyncIterator[MakeMemory]:
    """Start memories (seeded with both stories unless an engine is given)."""
    started: list[tuple[Memory, list[Served]]] = []

    async def make(engine: MemoryEngine | None = None, **options: Any) -> Memory:
        memory_host, webhook_host = Served(), Served()
        options.setdefault('push_origins', LOCALHOST_ORIGINS)  # the webhook is local
        options.setdefault('push_backoff', 0.05)
        server = create_app(
            engine or seeded_engine(),
            url=memory_host.url,
            authenticator=DevTokenAuthenticator(),
            push_client=httpx.AsyncClient(trust_env=False, timeout=5),
            validation='raise',
            **options,
        )
        webhook = Webhook()
        await memory_host.start(server.app)
        await webhook_host.start(webhook.app)
        memory = Memory(
            server=server,
            url=memory_host.url,
            webhook=webhook,
            webhook_url=f'{webhook_host.url}/hook',
            http=httpx.AsyncClient(trust_env=False, timeout=10),
        )
        started.append((memory, [memory_host, webhook_host]))
        return memory

    yield make

    for memory, hosts in started:
        for task in memory.background:
            task.cancel()
        await asyncio.gather(*memory.background, return_exceptions=True)
        for client in memory.clients:
            await client.close()
        await memory.http.aclose()
        await asyncio.gather(*(host.stop() for host in hosts))
    for memory, _ in started:
        assert not memory.problems + memory.webhook.problems, 'wire rules broken'


@pytest_asyncio.fixture
async def memory(make_memory: MakeMemory) -> Memory:
    return await make_memory()
