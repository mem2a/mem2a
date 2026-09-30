# SPDX-License-Identifier: Apache-2.0
"""Test harness: a memory and a webhook receiver on real localhost sockets.

Both run under uvicorn as tasks in the test's event loop, so tests can
change the memory's content directly (as an operator would) and watch the
updates arrive. Real sockets are needed because httpx's ASGI transport
buffers whole responses, which would hang on an open SubscribeToTask stream.
"""

from __future__ import annotations

import asyncio
import socket
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
from starlette.responses import JSONResponse
from starlette.routing import Route

from mem2a import DevTokenAuthenticator, Mem2AClient, MemoryEngine, create_app
from mem2a.constants import EXTENSION_URI
from mem2a.server import RPC_PATH, Mem2AServer


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
    """An acting agent's push endpoint: records each POST's headers and body."""

    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []
        self.app = Starlette(routes=[Route('/hook', self._receive, methods=['POST'])])

    async def _receive(self, request: Request) -> JSONResponse:
        body = checked(await request.json(), self.problems)
        await self.add(
            {'headers': {k.lower(): v for k, v in request.headers.items()}, 'body': body}
        )
        return JSONResponse({})

    def bodies(self) -> list[dict[str, Any]]:
        return [call['body'] for call in self.items]


class Served:
    """An ASGI app under uvicorn on a socket bound up front (no port races)."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(('127.0.0.1', 0))
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
        for host in hosts:
            await host.stop()
    for memory, _ in started:
        assert not memory.problems + memory.webhook.problems, 'wire rules broken'


@pytest_asyncio.fixture
async def memory(make_memory: MakeMemory) -> Memory:
    return await make_memory()
