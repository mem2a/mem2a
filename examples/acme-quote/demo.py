# SPDX-License-Identifier: Apache-2.0
"""The Acme quote, end to end: one memory, Tom's agent and Priya's agent.

    pip install -e "python[dev]"
    python examples/acme-quote/demo.py

Starts a Mem2A memory on localhost, seeded with the spec's Acme story
(spec/v0.1/examples 02-05), and plays it:

1. Tom's sales agent wants to send Acme a renewal quote. Memory says hold:
   legal paused Acme pricing (fact f-311, constraint c-17, precedent p-4).
2. Legal clears the pricing (f-340 supersedes f-311). Memory pushes a new
   dossier to Tom's webhook, and c-17 lapses.
3. Priya's agent is about to draft a follow-up to Acme. It negotiates too,
   and keeps a SubscribeToTask stream open.
4. Tom's agent sends the quote and commits. Memory records a claim, and
   Priya's agent hears about it right away.
5. The CRM confirms the claim. Priya's agent hears that too, and cancels its
   follow-up: the quote already went out.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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
    MemoryEngine,
    PushTarget,
    Relevance,
    create_app,
    dossier_of,
    models,
    phase_of,
    receipt_of,
    state_of,
    update_of,
)


TOM = DevTokenAuthenticator.token('sales-assistant', 'user:tom', ['group:sales'])
PRIYA = DevTokenAuthenticator.token('account-manager', 'user:priya', ['group:sales'])


def seed(memory: MemoryEngine) -> None:
    """What Example Corp's memory knows on Monday morning (spec example 02)."""
    memory.add_source(
        'meeting:legal-weekly-2026-09-25',
        kind='meeting',
        title='Legal weekly',
        at=datetime(2026, 9, 25, 17, tzinfo=timezone.utc),
        readers=['group:sales', 'group:legal'],
    )
    memory.add_fact(
        'f-311',
        'Legal paused new pricing for Acme on Friday, pending a contract review.',
        source='meeting:legal-weekly-2026-09-25',
        confirmed_by='user:general-counsel',
        entities=['account:acme'],
    )
    memory.add_source('crm:opportunity/acme-renewal-2026', kind='record')
    memory.add_fact(
        'f-208',
        "Acme's current contract renews on October 31, 2026.",
        source='crm:opportunity/acme-renewal-2026',
        confirmed_by='system:crm',
        entities=['account:acme'],
    )
    memory.add_source('decision:globex-quote-withdrawal-2026-06', kind='decision')
    memory.add_precedent(
        'p-4',
        'In June, a renewal quote sent to Globex during a legal review had to be withdrawn.',
        source='decision:globex-quote-withdrawal-2026-06',
        decided_by='user:general-counsel',
        decided_at=datetime(2026, 6, 12, 15, tzinfo=timezone.utc),
        relevance=[
            Relevance(
                'Same situation: pricing sent while legal was still reviewing.',
                actions={'send_quote'},
                facts={'f-311'},
            )
        ],
    )
    memory.add_policy(
        'c-17',
        'Do not send Acme new pricing until legal clears it.',
        level='must',
        basis=['f-311'],
        actions=['send_quote'],
        until='Legal clears Acme pricing.',
    )
    # Who may see claims agents make about these entities.
    memory.set_entity_readers('account:acme', ['group:sales', 'group:legal'])
    memory.set_entity_readers('doc:acme-renewal-quote', ['group:sales'])


def legal_clears(memory: MemoryEngine) -> None:
    """Monday afternoon (spec example 03)."""
    memory.add_source(
        'email:legal-acme-approval-2026-09-28',
        kind='email',
        title='Acme pricing: approved',
        at=datetime(2026, 9, 28, 15, 2, tzinfo=timezone.utc),
        readers=['group:sales', 'group:legal'],
    )
    memory.add_fact(
        'f-340',
        'Legal approved Acme renewal pricing at $1.2M a year.',
        source='email:legal-acme-approval-2026-09-28',
        confirmed_by='user:general-counsel',
        entities=['account:acme'],
        supersedes=['f-311'],
        note='Legal cleared Acme pricing.',
    )


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


def show_update(update: models.Update, dossier: models.Dossier) -> None:
    """One line per change; details for facts the agent can now read."""
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
        if request.headers.get('authorization') == 'Bearer single-use-secret':
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
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
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


async def updates(
    stream: AsyncIterator[Any],
) -> AsyncIterator[tuple[models.Dossier, models.Update]]:
    """Pairs of (replaced dossier, update) from a SubscribeToTask stream."""
    dossier = None
    async for event in stream:
        dossier = dossier_of(event) or dossier
        update = update_of(event)
        if update is not None and dossier is not None:
            yield dossier, update


def local_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=10)  # localhost: skip any proxy


# ------------------------------------------------------------------ story
async def main() -> int:
    started = time.perf_counter()
    engine = MemoryEngine(first_version=12)  # so the versions match the spec examples
    seed(engine)

    memory_host, webhook_host, webhook = Host(), Host(), Webhook()
    server = create_app(
        engine,
        url=memory_host.url,
        authenticator=DevTokenAuthenticator(),
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
        push = PushTarget(f'{webhook_host.url}/a2a/callbacks', bearer='single-use-secret')
        tom_task = await tom.negotiate(
            {
                'action': 'send_quote',
                'summary': 'Send Acme a renewal quote',
                'entities': ['account:acme', 'doc:acme-renewal-quote'],
                'onBehalfOf': 'user:tom',
            },
            push=push,
        )
        dossier = dossier_of(tom_task)
        assert dossier is not None
        say('memory', f'dossier {dossier.version}, {phase_of(tom_task)}: "{dossier.summary}"')
        show_dossier(dossier)
        say('tom', 'Holding the quote. Memory will call my webhook if this changes.')

        # 2. Legal clears the pricing; memory calls Tom's agent back.
        print()
        say('legal', 'Approves Acme pricing at $1.2M a year (f-340 supersedes f-311).')
        legal_clears(engine)
        dossier, update = await webhook.next_update()
        say('memory', f'push to tom: dossier {dossier.version} replaces {update.previous_version}.')
        say('', f'  "{update.summary}"')
        show_update(update, dossier)
        say('tom', 'No constraints left. Sending the quote.')

        # 3. Priya's agent negotiates too, and subscribes instead of a webhook.
        print()
        say('priya', 'About to draft a follow-up to Acme. Asking memory first.')
        priya_task = await priya.negotiate(
            {
                'action': 'draft_followup',
                'summary': 'Draft a follow-up to Acme about the renewal',
                'entities': ['account:acme'],
                'onBehalfOf': 'user:priya',
            }
        )
        priya_dossier = dossier_of(priya_task)
        assert priya_dossier is not None
        say('memory', f'dossier {priya_dossier.version}: "{priya_dossier.summary}"')
        priya_updates = updates(priya.subscribe(priya_task.id))

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

        priya_dossier, update = await asyncio.wait_for(anext(priya_updates), 5)
        say(
            'memory',
            f'stream to priya: dossier {priya_dossier.version} replaces {update.previous_version}.',
        )
        show_update(update, priya_dossier)

        # 5. A system of record confirms the claim.
        print()
        say('crm', f'The CRM shows the quote went out: confirms {recorded.fact_id}.')
        engine.confirm_fact(recorded.fact_id, by='system:crm')
        priya_dossier, update = await asyncio.wait_for(anext(priya_updates), 5)
        say(
            'memory',
            f'stream to priya: dossier {priya_dossier.version} replaces {update.previous_version}.',
        )
        show_update(update, priya_dossier)
        say('priya', 'Tom already sent the quote, so no follow-up now. Canceling.')
        canceled = await priya.cancel(priya_task.id)
        say(
            'memory',
            f'Task {state(canceled)}, phase {phase_of(canceled)}. Memory stopped watching.',
        )
        await priya_updates.aclose()
    finally:
        await tom.close()
        await priya.close()
        await webhook_host.stop()
        await memory_host.stop()

    print(f'\nDone in {time.perf_counter() - started:.1f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
