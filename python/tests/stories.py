# SPDX-License-Identifier: Apache-2.0
"""The spec's stories as seed data (Acme quote, Titan update)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mem2a.auth import DevTokenAuthenticator
from mem2a.engine import MemoryEngine, Relevance


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


TOM = DevTokenAuthenticator.token('sales-assistant', 'user:tom', ['group:sales'])
PRIYA = DevTokenAuthenticator.token('account-manager', 'user:priya', ['group:sales'])
MAYA = DevTokenAuthenticator.token('chief-of-staff-agent', 'user:maya', ['group:leadership'])

TOM_INTENT: dict[str, Any] = {
    'action': 'send_quote',
    'summary': 'Send Acme a renewal quote',
    'entities': ['account:acme', 'doc:acme-renewal-quote'],
    'onBehalfOf': 'user:tom',
    'deadline': '2026-09-30T17:00:00Z',
}
PRIYA_INTENT: dict[str, Any] = {
    'action': 'draft_followup',
    'summary': 'Draft a follow-up to Acme about the renewal',
    'entities': ['account:acme'],
    'onBehalfOf': 'user:priya',
}
MAYA_INTENT: dict[str, Any] = {
    'action': 'update_leadership',
    'summary': 'Update leadership on the Titan delay',
    'entities': ['project:titan'],
    'onBehalfOf': 'user:maya',
    'draft': 'Titan will ship three weeks late, on November 20.',
}


def tom_commit(based_on: str) -> dict[str, Any]:
    return {
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


def seed_acme(engine: MemoryEngine) -> None:
    engine.add_source(
        'meeting:legal-weekly-2026-09-25',
        kind='meeting',
        title='Legal weekly',
        at=dt('2026-09-25T17:00:00Z'),
        readers=['group:sales', 'group:legal'],
    )
    engine.add_fact(
        'f-311',
        'Legal paused new pricing for Acme on Friday, pending a contract review.',
        source='meeting:legal-weekly-2026-09-25',
        confirmed_by='user:general-counsel',
        entities=['account:acme'],
    )
    engine.add_source('crm:opportunity/acme-renewal-2026', kind='record')
    engine.add_fact(
        'f-208',
        "Acme's current contract renews on October 31, 2026.",
        source='crm:opportunity/acme-renewal-2026',
        confirmed_by='system:crm',
        entities=['account:acme'],
    )
    engine.add_source('decision:globex-quote-withdrawal-2026-06', kind='decision')
    engine.add_precedent(
        'p-4',
        'In June, a renewal quote sent to Globex during a legal review had to be withdrawn.',
        source='decision:globex-quote-withdrawal-2026-06',
        decided_by='user:general-counsel',
        decided_at=dt('2026-06-12T15:00:00Z'),
        relevance=[
            Relevance(
                'Same situation: pricing sent while legal was still reviewing.',
                actions={'send_quote'},
                facts={'f-311'},
            )
        ],
    )
    engine.add_policy(
        'c-17',
        'Do not send Acme new pricing until legal clears it.',
        level='must',
        basis=['f-311'],
        actions=['send_quote'],
        until='Legal clears Acme pricing.',
    )
    engine.set_entity_readers('account:acme', ['group:sales', 'group:legal'])
    engine.set_entity_readers('doc:acme-renewal-quote', ['group:sales'])
    engine.add_source(
        'email:legal-acme-approval-2026-09-28',
        kind='email',
        title='Acme pricing: approved',
        at=dt('2026-09-28T15:02:00Z'),
        readers=['group:sales', 'group:legal'],
    )


def legal_clears(engine: MemoryEngine) -> None:
    engine.supersede_fact(
        'f-311',
        'f-340',
        'Legal approved Acme renewal pricing at $1.2M a year.',
        source='email:legal-acme-approval-2026-09-28',
        confirmed_by='user:general-counsel',
        entities=['account:acme'],
        note='Legal cleared Acme pricing.',
    )


def seed_titan(engine: MemoryEngine) -> None:
    engine.add_source('jira:TITAN-812', kind='ticket')
    engine.add_fact(
        'f-501',
        "Titan's launch date moved from October 30 to November 20.",
        source='jira:TITAN-812',
        confirmed_by='user:maya',
        entities=['project:titan'],
    )
    engine.add_source('meeting:titan-standup-2026-09-29', kind='meeting')
    engine.add_fact(
        'f-502',
        'Priya owns the Titan data migration rewrite.',
        source='meeting:titan-standup-2026-09-29',
        confirmed_by='user:maya',
        entities=['project:titan', 'user:priya'],
    )
    engine.add_source('decision:orion-slip-2026-06', kind='decision')
    engine.add_precedent(
        'p-2',
        'In June, leadership rejected a request to slip Orion by a month without new funding.',
        source='decision:orion-slip-2026-06',
        decided_by='group:leadership',
        decided_at=dt('2026-06-18T18:00:00Z'),
        entities=['project:orion'],
        relevance=[
            Relevance(
                'Same kind of request: an unfunded slip. Leadership asked for a cost plan first.',
                actions={'update_leadership'},
                tags={'unfunded-slip'},
            )
        ],
    )
    engine.add_policy(
        'c-40', 'Lead with how the extra cost will be covered.', level='should', basis=['p-2']
    )
    engine.add_policy(
        'c-41',
        'Keep leadership updates to three bullets.',
        level='should',
        policy='policy:leadership-update-format',
        actions=['update_leadership'],
    )
    engine.add_question(
        'q-1',
        'Is the new date funded?',
        options=['Yes', 'No', 'Partly'],
        actions=['update_leadership'],
        entity_types=['project'],
        tags={'no': ['unfunded-slip'], 'partly': ['unfunded-slip']},
        prompt='Before I pull this together: is the new date funded?',
    )
