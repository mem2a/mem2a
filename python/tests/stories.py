# SPDX-License-Identifier: Apache-2.0
"""The spec's stories for the tests, on top of `mem2a.seeds`."""

from __future__ import annotations

from typing import Any

from mem2a.engine import MemoryEngine
from mem2a.seeds import (
    MAYA,
    MAYA_INTENT,
    PRIYA,
    PRIYA_INTENT,
    TOM,
    TOM_INTENT,
    legal_clears,
    seed_acme,
    seed_titan,
)


__all__ = [
    'LEGAL_MEETING',
    'MAYA',
    'MAYA_INTENT',
    'PRIYA',
    'PRIYA_AS_SALES_ASSISTANT',
    'PRIYA_INTENT',
    'TOM',
    'TOM_INTENT',
    'TOM_WITHOUT_GROUPS',
    'legal_clears',
    'seeded_engine',
    'tom_commit',
]

#: Tom's agent, but with Priya's credentials (example 08).
PRIYA_AS_SALES_ASSISTANT = 'dev:sales-assistant:user:priya:group:sales'
#: Tom's own agent and principal, after Tom left group:sales.
TOM_WITHOUT_GROUPS = 'dev:sales-assistant:user:tom'
#: The source of f-311, readable by sales and legal.
LEGAL_MEETING = 'meeting:legal-weekly-2026-09-25'


def tom_commit(based_on: str, conflicts: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Example 05's commit, against `based_on`."""
    commit: dict[str, Any] = {
        'basedOn': based_on,
        'action': 'send_quote',
        'outcome': 'done',
        'summary': 'Sent Acme the renewal quote at $1.2M a year.',
        'claims': [
            {
                'statement': 'Tom sent Acme a renewal quote at $1.2M a year.',
                'entities': ['account:acme', 'doc:acme-renewal-quote'],
                'evidence': [
                    {
                        'kind': 'email',
                        'ref': 'email:<CAF1e2d3-acme-quote@mail.example.com>',
                        'at': '2026-09-28T15:18:40Z',
                    }
                ],
            }
        ],
    }
    if conflicts is not None:
        commit['conflicts'] = conflicts
    return commit


def seeded_engine(**options: Any) -> MemoryEngine:
    """Both stories in one memory, numbering dossiers from 12 like the Acme examples."""
    options.setdefault('first_version', 12)
    engine = MemoryEngine(**options)
    seed_acme(engine)
    seed_titan(engine)
    return engine
