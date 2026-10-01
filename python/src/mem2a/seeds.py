# SPDX-License-Identifier: Apache-2.0
"""Sample memories from the spec's stories, for the sandbox, demos and tests.

* ``acme``: examples 02, 03, 05, 09 and 10. Legal paused Acme pricing (fact
  ``f-311``, version 3), so policy ``c-17`` says to hold the quote; precedent
  ``p-4`` recalls the Globex quote that had to be withdrawn. Dossier numbering
  starts at 12, so the first intent gets dossier 12. `legal_clears` plays
  example 03: ``f-340`` supersedes ``f-311``, and ``c-17`` and ``p-4`` lapse.
* ``titan``: examples 06 and 07. Memory asks whether Titan's new date is
  funded; if not, it cites the Orion precedent. Numbering starts at 7.
* ``empty``: nothing at all.

The dev tokens and intents below go with the stories.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mem2a.engine import MemoryEngine, When


#: Development tokens for the stories' agents (see `mem2a.auth.DevTokenAuthenticator`).
TOM = 'dev:sales-assistant:user:tom:group:sales'
PRIYA = 'dev:account-assistant:user:priya:group:sales'
MAYA = 'dev:chief-of-staff-agent:user:maya:group:leadership'

#: Tom's agent is about to send Acme a renewal quote (example 02).
TOM_INTENT: dict[str, Any] = {
    'action': 'send_quote',
    'summary': 'Send Acme a renewal quote',
    'entities': ['account:acme', 'doc:acme-renewal-quote'],
    'onBehalfOf': 'user:tom',
}
#: Priya's agent is about to draft a follow-up to Acme (example 09).
PRIYA_INTENT: dict[str, Any] = {
    'action': 'draft_followup',
    'summary': 'Draft a follow-up email to Acme about the renewal',
    'entities': ['account:acme'],
    'onBehalfOf': 'user:priya',
}
#: Maya's agent is about to update leadership on the Titan delay (example 06).
MAYA_INTENT: dict[str, Any] = {
    'action': 'update_leadership',
    'summary': 'Update leadership on the Titan delay',
    'entities': ['project:titan'],
    'onBehalfOf': 'user:maya',
    'draft': 'Titan will ship three weeks late, on November 20.',
}


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def seed_acme(memory: MemoryEngine) -> None:
    """What Example Corp's memory knows on Monday morning (example 02)."""
    memory.add_source(
        'meeting:legal-weekly-2026-09-25',
        kind='meeting',
        title='Legal weekly',
        at=_at('2026-09-25T17:00:00'),
        readers=['group:sales', 'group:legal'],
    )
    memory.add_fact(
        'f-311',
        'Legal paused new pricing for Acme on Friday, pending a contract review.',
        source='meeting:legal-weekly-2026-09-25',
        confirmed_by='user:general-counsel',
        entities=['account:acme'],
        version=3,
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
        relevance='Same situation: pricing sent while legal was still reviewing.',
        source='decision:globex-quote-withdrawal-2026-06',
        decided_by='user:general-counsel',
        decided_at=_at('2026-06-12T15:00:00'),
        when=[When(actions={'send_quote'}, facts={'f-311'})],
    )
    memory.add_policy(
        'c-17',
        'Do not send Acme new pricing until legal clears it.',
        level='must',
        basis=['f-311'],
        actions=['send_quote'],
        until='Legal clears Acme pricing.',
    )
    # Who, besides the claimant, may see what agents report about these.
    memory.set_entity_readers('account:acme', ['group:sales', 'group:legal'])
    memory.set_entity_readers('doc:acme-renewal-quote', ['group:sales'])


def legal_clears(memory: MemoryEngine) -> None:
    """Monday afternoon (example 03): legal approves Acme's pricing."""
    memory.add_source(
        'email:legal-acme-approval-2026-09-28',
        kind='email',
        title='Acme pricing: approved',
        at=_at('2026-09-28T15:02:00'),
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


def seed_titan(memory: MemoryEngine) -> None:
    """Maya's Titan update (examples 06 and 07)."""
    memory.add_source('jira:TITAN-812', kind='ticket')
    memory.add_fact(
        'f-501',
        "Titan's launch date moved from October 30 to November 20.",
        source='jira:TITAN-812',
        confirmed_by='user:maya',
        entities=['project:titan'],
        version=2,
    )
    memory.add_source('meeting:titan-standup-2026-09-29', kind='meeting')
    memory.add_fact(
        'f-502',
        'Priya owns the Titan data migration rewrite.',
        source='meeting:titan-standup-2026-09-29',
        confirmed_by='user:maya',
        entities=['project:titan', 'user:priya'],
    )
    memory.add_source('decision:orion-slip-2026-06', kind='decision')
    memory.add_precedent(
        'p-2',
        'In June, leadership rejected a request to slip Orion by a month without new funding.',
        relevance='Same kind of request: an unfunded slip. Leadership asked for a cost plan first.',
        source='decision:orion-slip-2026-06',
        decided_by='group:leadership',
        decided_at=_at('2026-06-18T18:00:00'),
        entities=['project:orion'],
        when=[When(actions={'update_leadership'}, tags={'unfunded-slip'})],
    )
    memory.add_policy(
        'c-40', 'Lead with how the extra cost will be covered.', level='should', basis=['p-2']
    )
    memory.add_policy(
        'c-41',
        'Keep leadership updates to three bullets.',
        level='should',
        policy='policy:leadership-update-format',
        actions=['update_leadership'],
    )
    memory.add_question(
        'q-1',
        'Is the new date funded?',
        options=['Yes', 'No', 'Partly'],
        actions=['update_leadership'],
        entity_types=['project'],
        tags={'no': ['unfunded-slip'], 'partly': ['unfunded-slip']},
        prompt='Before I pull this together: is the new date funded?',
    )


@dataclass(frozen=True)
class Seed:
    """A sample memory: what it's about, and how to load it."""

    description: str
    first_version: int
    load: Callable[[MemoryEngine], None]


SEEDS: dict[str, Seed] = {
    'acme': Seed('The Acme quote (spec examples 02, 03, 05, 09, 10)', 12, seed_acme),
    'titan': Seed('The Titan update (spec examples 06-07)', 7, seed_titan),
    'empty': Seed('An empty memory', 1, lambda memory: None),
}


def seeded_engine(name: str, **options: Any) -> MemoryEngine:
    """A `MemoryEngine` loaded with the named seed; `options` go to its constructor."""
    seed = SEEDS[name]
    options.setdefault('first_version', seed.first_version)
    memory = MemoryEngine(**options)
    seed.load(memory)
    return memory
