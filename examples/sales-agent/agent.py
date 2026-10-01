# SPDX-License-Identifier: Apache-2.0
"""A sales agent that asks memory before it quotes, and waits when told to.

Run the sandbox, the agent, then clear legal's hold, in three terminals:

    mem2a serve --seed acme --dev-admin
    python examples/sales-agent/agent.py
    curl -X POST http://127.0.0.1:8000/dev/scenarios/acme/legal-clears

Or all at once: ``python examples/sales-agent/agent.py --self-test``.

The agent acts for Tom. It tells memory it is about to send Acme a renewal
quote, holds while the dossier carries a ``must`` constraint, sends the quote
as soon as memory says the constraint lifted, and commits what it did against
the dossier version it relied on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import aclosing
from typing import Any

import httpx
import uvicorn

from mem2a import Mem2AClient, dossier_of, error_of, models, phase_of, receipt_of
from mem2a.cli import sandbox
from mem2a.seeds import TOM, TOM_INTENT
from mem2a.server import listen_socket


def send_quote() -> str:
    """Stand-in for the real action: email Acme the quote. Returns its id."""
    return 'email:<acme-renewal-quote@mail.example.com>'


def report(
    based_on: str, evidence: str, conflicts: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """The commit: what the agent did, against the dossier it relied on."""
    commit: dict[str, Any] = {
        'basedOn': based_on,
        'action': 'send_quote',
        'outcome': 'done',
        'summary': 'Sent Acme the renewal quote.',
        'claims': [
            {
                'statement': 'Tom sent Acme a renewal quote.',
                'entities': ['account:acme', 'doc:acme-renewal-quote'],
                'evidence': [{'kind': 'email', 'ref': evidence}],
            }
        ],
    }
    if conflicts:
        commit['conflicts'] = conflicts
    return commit


def blocking(dossier: models.Dossier) -> list[models.Constraint]:
    return [c for c in dossier.constraints if c.level == 'must']


async def run(url: str, token: str = TOM, holding: asyncio.Event | None = None) -> int:
    """Negotiate, hold while blocked, act, commit. `holding` is set once the
    agent waits for memory (the self-test uses it)."""
    print(f'sales-agent: acting for user:tom against {url}')
    async with await Mem2AClient.connect(url, token=token) as memory:
        print(f'Asking memory first: {TOM_INTENT["summary"]}.')
        task = await memory.negotiate(TOM_INTENT)
        if phase_of(task) != 'awaiting-commit':
            print(f'Memory did not clear the action ({phase_of(task)}): {error_of(task)}')
            return 1

        evidence = None
        async with aclosing(memory.watch(task.id)) as dossiers:
            async for dossier, update in dossiers:
                if update is not None:
                    changes = ', '.join(f'{c.change} {c.kind} {c.id}' for c in update.changes)
                    print(f'Memory called back: {update.summary}')
                    print(f'  Dossier {dossier.version}: {changes}.')
                else:
                    print(f'Dossier {dossier.version}: {dossier.summary}')
                rules = blocking(dossier)
                if rules:
                    print(f'Holding the quote: {rules[0].statement} Waiting for memory...')
                    if holding is not None:
                        holding.set()
                    continue
                print('Nothing blocks the quote now. Sending it.')
                evidence = send_quote()
                break
        if evidence is None:
            print('The task ended before memory cleared the quote; nothing sent.')
            return 1

        done = await memory.commit(task, report(dossier.version, evidence))
        error = error_of(done)
        if error is not None and error.code == 'stale-dossier':
            # Memory changed between our last dossier and the commit. The quote
            # is out already: commit against the current dossier and say which
            # of its rules the quote goes against.
            current = dossier_of(done)
            assert current is not None
            conflicts = [
                {'id': c.id, 'explanation': 'The quote went out before this rule reached me.'}
                for c in blocking(current)
            ]
            done = await memory.commit(task, report(current.version, evidence, conflicts))
        receipt = receipt_of(done)
        if receipt is None:
            print(f'Memory did not record the commit: {error_of(done)}')
            return 1
        claims = ', '.join(f'{r.fact_id} ({r.status})' for r in receipt.recorded)
        print(
            f'Committed against dossier {receipt.based_on}: receipt {receipt.commit_id}, {claims}.'
        )
    return 0


async def self_test() -> int:
    """Start a sandbox memory, run the agent, and clear legal's hold over HTTP
    (the curl in the README) once the agent is waiting."""
    sock = listen_socket()
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(
        uvicorn.Config(sandbox('acme', url=url, dev_admin=True).app, log_level='warning')
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        holding = asyncio.Event()
        agent = asyncio.create_task(run(url, holding=holding))
        await asyncio.wait_for(holding.wait(), 10)
        print(f'(self-test: POST {url}/dev/scenarios/acme/legal-clears)')
        async with httpx.AsyncClient(trust_env=False) as http:
            cleared = await http.post(f'{url}/dev/scenarios/acme/legal-clears')
            cleared.raise_for_status()
        return await asyncio.wait_for(agent, 10)
    finally:
        server.should_exit = True
        await serving


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--url', default='http://127.0.0.1:8000', help="memory's base URL")
    parser.add_argument('--token', default=TOM, help="Tom's token (default: the sandbox's)")
    parser.add_argument('--self-test', action='store_true', help='start a sandbox and run it all')
    args = parser.parse_args()
    if args.self_test:
        return asyncio.run(self_test())
    return asyncio.run(run(args.url, args.token))


if __name__ == '__main__':
    sys.exit(main())
