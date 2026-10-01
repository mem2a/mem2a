# SPDX-License-Identifier: Apache-2.0
"""A tool gateway enforces company memory for an agent that has never heard of Mem2A.

    pip install -e "python[dev]"
    python examples/tool-gateway/demo.py

Starts a Mem2A memory on localhost, seeded with the spec's Acme story, and a
gateway in front of a pretend CRM. A plain agent asks the gateway to send
Acme a renewal quote:

1. The gateway turns the tool call into an intent and asks memory first.
   Legal paused Acme pricing, so the dossier carries a `must` constraint, and
   the gateway holds the call. The CRM is never called.
2. Legal approves the pricing, and memory gives the gateway's task a new
   dossier version.
3. The agent retries. The gateway reads the current dossier, finds nothing in
   the way, runs the call, and commits what happened, with the CRM's quote id
   as evidence. Memory records it as a claim.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx
import uvicorn
from gateway import Governed, Held, MemoryGateway

from mem2a import DevTokenAuthenticator, Mem2AClient, create_app, dossier_of
from mem2a.seeds import legal_clears, seeded_engine
from mem2a.server import listen_socket


#: The gateway's own token: the agent is the gateway, acting for Tom.
GATEWAY = 'dev:tool-gateway:user:tom:group:sales'


def say(who: str, text: str) -> None:
    print(f'{who:>7} | {text}')


class Crm:
    """A pretend CRM: the system of record the gateway holds credentials for."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_quote(self, account: str, price: str) -> dict[str, str]:
        self.calls += 1
        quote = f'Q-{1041 + self.calls}'
        say('crm', f'Quote {quote} sent to {account.title()}: {price}.')
        return {'quote_id': quote}


#: How the gateway describes calls to `crm.send_quote` to memory.
SEND_QUOTE = Governed(
    action='send_quote',
    summary=lambda args: f'Send {args["account"].title()} a renewal quote',
    entities=lambda args: [f'account:{args["account"]}', f'doc:{args["account"]}-renewal-quote'],
    claim=lambda args, result: (
        f'Tom sent {args["account"].title()} a renewal quote at {args["price"]}.'
    ),
    evidence=lambda args, result: {'kind': 'record', 'ref': f'crm:quote/{result["quote_id"]}'},
)


async def main() -> int:
    started = time.perf_counter()
    engine = seeded_engine('acme')
    socket = listen_socket()
    url = f'http://127.0.0.1:{socket.getsockname()[1]}'
    app = create_app(engine, url=url, authenticator=DevTokenAuthenticator(), validation='raise').app
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    serving = asyncio.create_task(server.serve(sockets=[socket]))
    while not server.started:
        await asyncio.sleep(0.01)
    http = httpx.AsyncClient(trust_env=False, timeout=10)  # localhost: skip any proxy
    memory = await Mem2AClient.connect(url, token=GATEWAY, http=http)
    crm = Crm()
    gateway = MemoryGateway(
        memory,
        user='user:tom',
        tools={'crm.send_quote': crm.send_quote},
        governed={'crm.send_quote': SEND_QUOTE},
    )
    print(f'\nMem2A demo: a tool gateway. Memory at {url}\n')

    try:
        # 1. A plain agent calls a tool. The gateway asks memory first.
        say('agent', "Calls crm.send_quote(account='acme', price='$1.2M a year').")
        say('gateway', 'Asks memory first: send Acme a renewal quote, for user:tom.')
        try:
            await gateway.call('crm.send_quote', account='acme', price='$1.2M a year')
            raise AssertionError('the gateway should have held the call')
        except Held as held:
            say('memory', f'dossier {held.dossier.version}: "{held.dossier.summary}"')
            say('gateway', 'Holds the call. The agent gets a tool error, and the CRM is untouched:')
            say('', f'  {held}')
            assert crm.calls == 0
            task_id, held_version = held.task_id, held.dossier.version

        # 2. Legal clears the pricing. Memory updates the gateway's open task.
        print()
        say('legal', 'Approves Acme pricing at $1.2M a year.')
        legal_clears(engine)
        for _ in range(500):
            dossier = dossier_of(await memory.get(task_id))
            if dossier is not None and dossier.version != held_version:
                break
            await asyncio.sleep(0.01)
        assert dossier is not None
        say('memory', f'The gateway\'s task now has dossier {dossier.version}: "{dossier.summary}"')

        # 3. The agent retries. The gateway reads, runs the call, and reports.
        print()
        say('agent', 'Retries the same call.')
        outcome = await gateway.call('crm.send_quote', account='acme', price='$1.2M a year')
        assert outcome.receipt is not None and outcome.dossier is not None and crm.calls == 1
        [recorded] = outcome.receipt.recorded
        fact = engine.fact(recorded.fact_id)
        assert fact is not None and fact.evidence
        say(
            'gateway',
            f'Read dossier {outcome.dossier.version} right before acting; nothing blocked it. '
            'Reported what ran.',
        )
        say(
            'memory',
            f'receipt {outcome.receipt.commit_id}: {recorded.fact_id} recorded as a '
            f'{recorded.status}, with evidence {fact.evidence[0].ref}.',
        )
        print()
        say('', 'The agent never spoke Mem2A. The gateway held the call while the rule applied,')
        say('', 'and memory knows what actually ran.')
    finally:
        await memory.close()
        server.should_exit = True
        await serving

    print(f'\nDone in {time.perf_counter() - started:.1f} s.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
