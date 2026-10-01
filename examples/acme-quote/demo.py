# SPDX-License-Identifier: Apache-2.0
"""The Acme quote, end to end: one memory, Tom's agent and Priya's agent.

    pip install -e "python[dev]"
    python examples/acme-quote/demo.py

Starts a Mem2A memory on localhost, seeded with the spec's Acme story
(spec/v0.1/examples 02-05 and 09), and plays it:

1. Tom's sales agent wants to send Acme a renewal quote. Memory says hold:
   legal paused Acme pricing (fact f-311, constraint c-17, precedent p-4).
2. Legal clears the pricing (f-340 supersedes f-311). Memory pushes a new
   dossier to Tom's webhook, and c-17 lapses. A push is only a signal (spec
   8.3.7), so Tom's agent reads the task (GetTask) and acts on what it reads.
3. Priya's agent is about to draft a follow-up to Acme. It negotiates too,
   and keeps a stream open on its task (SubscribeToTask, through
   `Mem2AClient.watch`).
4. Tom's agent sends the quote and commits. Memory records a claim, and
   Priya's agent hears about it right away.
5. The CRM confirms the claim. Priya's agent hears that too, and cancels its
   follow-up: the quote already went out.
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import aclosing
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mem2a import (
    DevTokenAuthenticator,
    Mem2AClient,
    PushTarget,
    create_app,
    dossier_of,
    models,
    phase_of,
    receipt_of,
    state_of,
    update_of,
)
from mem2a.seeds import PRIYA, PRIYA_INTENT, TOM, TOM_INTENT, legal_clears, seeded_engine
from mem2a.server import listen_socket


# ------------------------------------------------------------- narration
def say(who: str, text: str) -> None:
    print(f'{who:>7} | {text}')


def describe(fact: models.Fact) -> str:
    if fact.claimed_by is not None:
        return f'claim by {fact.claimed_by.on_behalf_of}'
    return f'confirmed by {fact.confirmed_by}'


def show_dossier(dossier: models.Dossier) -> None:
    for fact in dossier.facts:
        say('', f'  fact {fact.id} ({describe(fact)}): {fact.statement}')
    for item in dossier.precedent:
        say('', f'  precedent {item.id}: {item.statement}')
    for rule in dossier.constraints:
        say(
            '',
            f'  {rule.level.upper()} {rule.id}: {rule.statement} (basis: {", ".join(rule.basis)})',
        )


def show_update(where: str, update: models.Update, dossier: models.Dossier) -> None:
    """Which version replaced which, then one line per change, with details for
    the items the agent can now read."""
    say('memory', f'{where}: dossier {dossier.version} replaces {update.previous_version}.')
    signs = {'added': '+', 'updated': '~', 'removed': '-'}
    facts = {fact.id: fact for fact in dossier.facts}
    for change in update.changes:
        line = f'  {signs[change.change]} {change.kind} {change.id}'
        if change.id in facts:
            line += f' ({describe(facts[change.id])}): {facts[change.id].statement}'
        say('', line)


def state(task: Any) -> str:
    return (state_of(task) or '').removeprefix('TASK_STATE_')


# ------------------------------------------------------------- plumbing
class Webhook:
    """Tom's agent's push endpoint: every POST lands in a queue."""

    def __init__(self) -> None:
        self.received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.app = Starlette(routes=[Route('/a2a/callbacks', self._receive, methods=['POST'])])

    async def _receive(self, request: Request) -> JSONResponse:
        if request.headers.get('authorization') == 'Bearer secret-for-this-task':
            await self.received.put(await request.json())
        return JSONResponse({})

    async def next_update(self) -> tuple[models.Dossier, models.Update]:
        """The next replaced dossier and the update right after it."""
        dossier = None
        while True:
            body = await asyncio.wait_for(self.received.get(), 5)
            dossier = dossier_of(body) or dossier
            update = update_of(body)
            if update is not None and dossier is not None:
                return dossier, update


class Host:
    """An ASGI app under uvicorn on a free localhost port."""

    def __init__(self) -> None:
        self.socket = listen_socket()
        self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'

    async def start(self, app: Any) -> None:
        config = uvicorn.Config(app, log_level='warning', timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        while not self.server.started:
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.server.should_exit = True
        await self.task


def local_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=10)  # localhost: skip any proxy


# ------------------------------------------------------------------ story
async def main() -> int:
    started = time.perf_counter()
    engine = seeded_engine('acme')  # dossier numbering starts at 12, as in the examples
    memory_host, webhook_host, webhook = Host(), Host(), Webhook()
    server = create_app(
        engine,
        url=memory_host.url,
        authenticator=DevTokenAuthenticator(),
        # The one webhook origin registered, for Tom's agent only (spec 8.3.10).
        push_origins={'agent:sales-assistant': [webhook_host.url]},
        push_client=local_http(),
        validation='raise',
    )
    await memory_host.start(server.app)
    await webhook_host.start(webhook.app)
    tom = await Mem2AClient.connect(memory_host.url, token=TOM, http=local_http())
    priya = await Mem2AClient.connect(memory_host.url, token=PRIYA, http=local_http())
    print(f'\nMem2A demo: the Acme quote. Memory at {memory_host.url}\n')

    try:
        # 1. Tom's agent negotiates before acting, and leaves a webhook.
        say('tom', 'About to send Acme a renewal quote. Asking memory first.')
        push = PushTarget(f'{webhook_host.url}/a2a/callbacks', bearer='secret-for-this-task')
        tom_task = await tom.negotiate(TOM_INTENT, push=push)
        dossier = dossier_of(tom_task)
        assert dossier is not None
        say('memory', f'dossier {dossier.version}, {phase_of(tom_task)}: "{dossier.summary}"')
        show_dossier(dossier)
        say('tom', 'Holding the quote. Memory will call my webhook if this changes.')

        # 2. Legal clears the pricing; memory calls Tom's agent back. The push
        #    is a signal: the agent reads the task and acts on what it reads.
        print()
        say('legal', 'Approves Acme pricing at $1.2M a year (f-340 supersedes f-311).')
        legal_clears(engine)
        pushed, update = await webhook.next_update()
        show_update('push to tom', update, pushed)
        say('', f'  Summary: "{update.summary}"')
        dossier = dossier_of(await tom.get(tom_task.id))  # what memory says now
        assert dossier is not None and not dossier.constraints
        say(
            'tom',
            f'Reads dossier {dossier.version} before acting: no constraints left. '
            'Sending the quote.',
        )

        # 3. Priya's agent negotiates too, and keeps a stream open instead.
        print()
        say('priya', 'About to draft a follow-up to Acme. Asking memory first.')
        priya_task = await priya.negotiate(PRIYA_INTENT)
        async with aclosing(priya.watch(priya_task.id)) as watch:
            priya_dossier, _ = await asyncio.wait_for(anext(watch), 5)
            say('memory', f'dossier {priya_dossier.version}: "{priya_dossier.summary}"')

            # 4. Tom's agent acted: it commits against the dossier it relied on.
            print()
            say('tom', f'Quote sent. Committing against dossier {dossier.version}.')
            done = await tom.commit(
                tom_task,
                {
                    'basedOn': dossier.version,
                    'action': 'send_quote',
                    'outcome': 'done',
                    'summary': 'Sent Acme the renewal quote at $1.2M a year.',
                    'claims': [
                        {
                            'statement': 'Tom sent Acme a renewal quote at $1.2M a year.',
                            'entities': ['account:acme', 'doc:acme-renewal-quote'],
                            'evidence': [
                                {'kind': 'email', 'ref': 'email:<acme-quote@mail.example.com>'}
                            ],
                        }
                    ],
                },
            )
            receipt = receipt_of(done)
            assert receipt is not None
            [recorded] = receipt.recorded
            say(
                'memory',
                f'receipt {receipt.commit_id}: {recorded.fact_id} recorded as a {recorded.status}.',
            )
            say('', f'  Task {state(done)}, phase {phase_of(done)}.')

            priya_dossier, priya_update = await asyncio.wait_for(anext(watch), 5)
            assert priya_update is not None
            show_update('stream to priya', priya_update, priya_dossier)

            # 5. A system of record confirms the claim.
            print()
            say('crm', f'The CRM shows the quote went out: confirms {recorded.fact_id}.')
            engine.confirm_fact(recorded.fact_id, by='system:crm')
            priya_dossier, priya_update = await asyncio.wait_for(anext(watch), 5)
            assert priya_update is not None
            show_update('stream to priya', priya_update, priya_dossier)
            say('priya', 'Tom already sent the quote, so no follow-up now. Canceling.')
            canceled = await priya.cancel(priya_task.id)
            say(
                'memory',
                f'Task {state(canceled)}, phase {phase_of(canceled)}. Memory stopped watching.',
            )
    finally:
        await tom.close()
        await priya.close()
        await memory_host.stop()  # memory sends its last push notifications first
        await webhook_host.stop()

    print(f'\nDone in {time.perf_counter() - started:.1f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
