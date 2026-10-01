# SPDX-License-Identifier: Apache-2.0
"""``mem2a-conform`` against ``mem2a serve``: the reference passes every check."""

from __future__ import annotations

import contextlib
import io
import json
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Iterator

from mem2a import conform
from mem2a.seeds import PRIYA, TOM


@contextlib.contextmanager
def sandbox(*args: str) -> Iterator[str]:
    """Run ``python -m mem2a serve`` on a free port; yields its base URL."""
    command = [sys.executable, '-m', 'mem2a', 'serve', '--port', '0', *args]
    server = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: queue.Queue[str] = queue.Queue()

    def drain() -> None:  # keep reading, so the server never blocks on a full pipe
        assert server.stdout is not None
        for line in server.stdout:
            lines.put(line)
        lines.put('')

    threading.Thread(target=drain, daemon=True).start()
    try:
        seen = []
        while True:
            line = lines.get(timeout=30)
            seen.append(line)
            match = re.search(r'Agent Card:\s+(http://\S+)/\.well-known/agent-card\.json', line)
            if match:
                yield match.group(1)
                return
            assert line, 'mem2a serve exited:\n' + ''.join(seen)
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_the_reference_memory_passes_every_check() -> None:
    with sandbox('--seed', 'acme', '--dev-admin') as url:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = conform.main(
                [
                    '--url', url,
                    '--token', TOM,
                    '--principal', 'user:tom',
                    '--entity', 'account:acme',
                    '--action', 'send_quote',
                    '--other-token', PRIYA,
                    '--allow-writes',
                    '--dev-admin',
                    '--json',
                ]
            )  # fmt: skip
    report = json.loads(output.getvalue())
    failed = [r for r in report['results'] if r['status'] != 'PASS']
    assert not failed, json.dumps(failed, indent=2)
    assert code == 0 and report['pass'] == len(report['results']) >= 14
