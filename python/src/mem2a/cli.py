# SPDX-License-Identifier: Apache-2.0
"""The ``mem2a`` command (also ``python -m mem2a``).

    mem2a serve [--seed acme|titan|empty] [--host 127.0.0.1] [--port 8000] [--dev-admin]
                [--push-origin ORIGIN]...
    mem2a conform --url URL --token TOKEN ...     (same as mem2a-conform)

``mem2a serve`` runs a sandbox memory: seeded with one of the spec's stories,
accepting unsigned dev tokens, and sending push notifications only to the
webhook origins registered for agents: any port on this machine, unless
``--push-origin`` names others. Never expose it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

import httpx
import uvicorn

from mem2a import conform, seeds
from mem2a.auth import DevTokenAuthenticator
from mem2a.constants import EXTENSION_URI, INTENT
from mem2a.seeds import SEEDS, seeded_engine
from mem2a.server import (
    LOCALHOST_ORIGINS,
    RPC_PATH,
    Mem2AServer,
    PushOrigins,
    create_app,
    listen_socket,
)


def sandbox(
    seed: str = 'acme',
    *,
    url: str,
    dev_admin: bool = False,
    push_origins: PushOrigins = LOCALHOST_ORIGINS,
    **options: Any,
) -> Mem2AServer:
    """The memory ``mem2a serve`` runs: a seeded engine, dev tokens, and push
    notifications only to `push_origins`, registered for every agent (or per
    agent, given a mapping). `options` go to `create_app`."""
    # Registered origins work wherever they point (a LAN host, say), and
    # memory calls them directly, never through a proxy.
    options.setdefault('push_url_validator', None)
    options.setdefault('push_client', httpx.AsyncClient(timeout=10, trust_env=False))
    return create_app(
        seeded_engine(seed),
        url=url,
        authenticator=DevTokenAuthenticator(),
        push_origins=push_origins,
        dev_admin=dev_admin,
        **options,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['conform']:
        return conform.main(args[1:], prog='mem2a conform')
    parser = argparse.ArgumentParser(prog='mem2a', description='Mem2A v0.1 reference tools.')
    commands = parser.add_subparsers(dest='command', required=True, metavar='command')
    serve = commands.add_parser(
        'serve',
        help='run a sandbox memory with dev tokens',
        description='Run a sandbox memory seeded with one of the spec stories. It accepts '
        'unsigned dev tokens, and sends push notifications only to the webhook origins '
        'registered with --push-origin (by default, any port on this machine). Never '
        'expose it.',
    )
    serve.add_argument(
        '--seed',
        choices=sorted(SEEDS),
        default='acme',
        help='what memory knows at startup (default: acme)',
    )
    serve.add_argument('--host', default='127.0.0.1', help='default: 127.0.0.1')
    serve.add_argument('--port', type=int, default=8000, help='default: 8000; 0 picks a free port')
    serve.add_argument(
        '--dev-admin',
        action='store_true',
        help='also serve the UNAUTHENTICATED /dev admin routes that change memory',
    )
    serve.add_argument(
        '--push-origin',
        action='append',
        dest='push_origins',
        metavar='ORIGIN',
        help='register a webhook origin for every agent, such as http://10.0.0.5:9000 or '
        'http://10.0.0.5:* for any port (repeatable). Replaces the default: '
        'http(s)://127.0.0.1, localhost and [::1], any port',
    )
    commands.add_parser('conform', help='check any memory against Mem2A v0.1', add_help=False)
    parsed = parser.parse_args(args)
    return serve_sandbox(
        seed=parsed.seed,
        host=parsed.host,
        port=parsed.port,
        dev_admin=parsed.dev_admin,
        push_origins=parsed.push_origins or LOCALHOST_ORIGINS,
    )


def serve_sandbox(
    *, seed: str, host: str, port: int, dev_admin: bool, push_origins: Sequence[str]
) -> int:
    """Bind, print how to talk to the memory, then serve until interrupted."""
    try:
        sock = listen_socket(host, port)
    except OSError as error:
        print(f'mem2a serve: cannot listen on {host}:{port}: {error.strerror}', file=sys.stderr)
        return 1
    port = sock.getsockname()[1]
    shown = '127.0.0.1' if host in ('0.0.0.0', '::', '') else host
    url = f'http://{f"[{shown}]" if ":" in shown else shown}:{port}'

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    try:
        server = sandbox(seed, url=url, dev_admin=dev_admin, push_origins=push_origins)
    except ValueError as error:  # a --push-origin that isn't an origin
        print(f'mem2a serve: {error}', file=sys.stderr)
        sock.close()
        return 2
    print(banner(seed, url, dev_admin=dev_admin), flush=True)
    config = uvicorn.Config(server.app, log_level='info', timeout_graceful_shutdown=2)
    uvicorn.Server(config).run(sockets=[sock])
    return 0


def banner(seed: str, url: str, *, dev_admin: bool) -> str:
    """What ``mem2a serve`` prints on startup: URLs, dev tokens, two curls."""
    token, intent = (
        (seeds.MAYA, seeds.MAYA_INTENT) if seed == 'titan' else (seeds.TOM, seeds.TOM_INTENT)
    )
    request = {
        'jsonrpc': '2.0',
        'id': '1',
        'method': 'SendMessage',
        'params': {
            'message': {
                'messageId': 'msg-1',
                'role': 'ROLE_USER',
                'parts': [{'mediaType': INTENT, 'data': intent}],
                'extensions': [EXTENSION_URI],
            }
        },
    }
    lines = [
        f'Mem2A sandbox memory, seeded with {seed}: {SEEDS[seed].description}',
        '',
        f'  Agent Card:  {url}/.well-known/agent-card.json',
        f'  JSON-RPC:    {url}{RPC_PATH}',
        '',
        'Dev tokens (unsigned: for this sandbox only):',
        f'  Tom:    {seeds.TOM}',
        f'  Priya:  {seeds.PRIYA}',
        *([f'  Maya:   {seeds.MAYA}'] if seed == 'titan' else []),
        '',
        'Try it:',
        '',
        f'  curl -s {url}/.well-known/agent-card.json',
        '',
        f'  curl -s {url}{RPC_PATH} \\',
        "    -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \\",
        f"    -H 'A2A-Extensions: {EXTENSION_URI}' \\",
        f"    -H 'Authorization: Bearer {token}' \\",
        f"    -d '{_shell_quoted(json.dumps(request))}'",
        '',
    ]
    if dev_admin:
        lines += [
            'WARNING: dev admin routes are ON. They are UNAUTHENTICATED: anyone who can reach',
            'this port can read and change memory. Never expose them.',
            f'  GET  {url}/dev/state',
            f'  POST {url}/dev/facts',
            f'  POST {url}/dev/facts/{{id}}/retire',
            f'  POST {url}/dev/facts/{{id}}/confirm',
            f'  POST {url}/dev/sources/{{ref}}/readers',
            f'  POST {url}/dev/scenarios/acme/legal-clears',
            '',
        ]
    return '\n'.join(lines)


def _shell_quoted(text: str) -> str:
    """`text` for use inside a single-quoted shell string."""
    return text.replace("'", "'\\''")
