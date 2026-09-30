# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the engine's rules, without A2A."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from mem2a import constants as C
from mem2a import validation
from mem2a.auth import DevTokenAuthenticator, Identity
from mem2a.engine import MemoryEngine, Reply

from stories import (
    LEGAL_MEETING,
    MAYA,
    MAYA_INTENT,
    PRIYA,
    PRIYA_INTENT,
    TOM,
    TOM_INTENT,
    legal_clears,
    seed_acme,
    seeded_engine,
    tom_commit,
)


EXAMPLES = Path(__file__).resolve().parents[2] / 'spec' / 'v0.1' / 'examples'

TOM_ID = DevTokenAuthenticator.parse(TOM)
PRIYA_ID = DevTokenAuthenticator.parse(PRIYA)
MAYA_ID = DevTokenAuthenticator.parse(MAYA)
LEGAL_ID = Identity('agent:counsel-bot', 'user:general-counsel', frozenset({'group:legal'}))


def intent(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(C.INTENT, data)]


def commit(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(C.COMMIT, data)]


def answer(question_id: str, text: str) -> list[tuple[str, Any]]:
    return [(C.ANSWER, {'questionId': question_id, 'text': text})]


def valid(reply: Reply) -> Reply:
    """Every payload the engine produces validates against its schema."""
    for kind in ('dossier', 'receipt', 'question', 'update', 'error'):
        payload = getattr(reply, kind)
        if payload is not None:
            validation.validate(kind, payload.dump())
    return reply


def ids(items: list[Any]) -> list[str]:
    return [item.id for item in items]


def example_dossier(name: str) -> dict[str, Any]:
    text = (EXAMPLES / name).read_text()
    [dossier] = [
        part['data']
        for part in _walk(json.loads(text))
        if isinstance(part, dict) and part.get('mediaType') == C.DOSSIER
    ][:1]
    return dossier


def _walk(node: Any) -> Any:
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    for child in children:
        yield from _walk(child)


def without_versions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if k != 'version'} for item in items]


@pytest.fixture
def engine() -> MemoryEngine:
    return seeded_engine()


# ------------------------------------------------------- the spec's stories
@pytest.mark.skipif(not EXAMPLES.is_dir(), reason='needs the spec examples')
def test_dossiers_match_the_spec_examples(engine: MemoryEngine) -> None:
    tom = valid(engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))).dossier
    assert tom is not None
    ours, theirs = tom.dump(), example_dossier('02-negotiate.json')
    assert without_versions(ours['facts']) == without_versions(theirs['facts'])
    assert ours['precedent'] == theirs['precedent']
    assert ours['constraints'] == theirs['constraints']

    legal_clears(engine)
    after = valid(engine.refresh('t')).dossier  # type: ignore[union-attr]
    ours, theirs = after.dump(), example_dossier('03-listen-update.json')  # type: ignore[union-attr]
    assert without_versions(ours['facts']) == without_versions(theirs['facts'])
    assert (ours['precedent'], ours['constraints']) == ([], [])
    assert ours['watching'] == theirs['watching']

    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    maya = valid(engine.respond('m', MAYA_ID, answer('q-1', 'No.'))).dossier
    ours, theirs = maya.dump(), example_dossier('07-answer.json')  # type: ignore[union-attr]
    assert without_versions(ours['facts']) == without_versions(theirs['facts'])
    assert ours['precedent'] == theirs['precedent']
    assert ours['constraints'] == theirs['constraints']
    assert sorted(ours['watching']) == sorted(theirs['watching'])


def test_update_lists_every_change(engine: MemoryEngine) -> None:
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    engine.update_fact('f-208', statement="Acme's contract renews on November 30, 2026.")
    engine.add_source('crm:acme-contacts', kind='record')
    engine.add_fact(
        'f-400',
        'Acme has a new procurement lead.',
        source='crm:acme-contacts',
        confirmed_by='system:crm',
        entities=['account:acme'],
    )
    engine.retire_fact('f-311')

    reply = valid(engine.refresh('t'))  # type: ignore[arg-type]
    assert reply is not None and reply.update is not None and reply.dossier is not None
    assert [(c.id, c.kind, c.change) for c in reply.update.changes] == [
        ('f-400', 'fact', 'added'),
        ('f-208', 'fact', 'updated'),
        ('f-311', 'fact', 'removed'),
        ('c-17', 'constraint', 'removed'),
        ('p-4', 'precedent', 'removed'),
    ]
    assert reply.update.previous_version == first.version  # type: ignore[union-attr]
    assert reply.update.dossier_version == reply.dossier.version
    [f208] = [f for f in reply.dossier.facts if f.id == 'f-208']
    assert f208.version == '2'
    assert engine.refresh('t') is None  # nothing changed since


def test_versions_are_memory_wide_strings(engine: MemoryEngine) -> None:
    a = engine.negotiate('a', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    b = engine.negotiate('b', 'c', PRIYA_ID, intent(PRIYA_INTENT)).dossier
    legal_clears(engine)
    a2 = engine.refresh('a').dossier  # type: ignore[union-attr]
    versions = [a.version, b.version, a2.version]  # type: ignore[union-attr]
    assert all(isinstance(v, str) for v in versions)
    assert [int(v) for v in versions] == sorted(int(v) for v in versions)
    assert len(set(versions)) == 3


def test_retired_and_superseded_facts_leave_dossiers(engine: MemoryEngine) -> None:
    legal_clears(engine)
    dossier = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert dossier is not None
    assert ids(dossier.facts) == ['f-340', 'f-208']
    assert dossier.facts[0].supersedes == ['f-311']
    assert (dossier.constraints, dossier.precedent) == ([], [])

    engine.retire_fact('f-208')
    reply = engine.refresh('t')
    assert reply is not None and reply.update is not None
    assert [(c.id, c.change) for c in reply.update.changes] == [('f-208', 'removed')]


# -------------------------------------------------------------- permissions
def test_permissions_follow_every_source(engine: MemoryEngine) -> None:
    engine.add_source('email:board-only', kind='email', readers=['group:board'])
    engine.add_source('crm:acme-health', kind='record')
    engine.add_fact(
        'f-500',
        'Acme is at risk of churning.',
        source='crm:acme-health',
        derived_from=['email:board-only'],
        confirmed_by='user:cfo',
        entities=['account:acme'],
    )
    board = Identity('agent:x', 'user:cfo', frozenset({'group:board'}))
    tom = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    cfo = engine.negotiate(
        'b', 'c', board, intent({**TOM_INTENT, 'onBehalfOf': 'user:cfo'})
    ).dossier
    assert 'f-500' not in ids(tom.facts)  # type: ignore[union-attr]
    # The CFO can read both of f-500's sources, but not f-311's.
    assert 'f-500' in ids(cfo.facts) and 'f-311' not in ids(cfo.facts)  # type: ignore[union-attr]
    # Constraints and precedent derived from invisible facts go too.
    assert (cfo.constraints, cfo.precedent) == ([], [])  # type: ignore[union-attr]


def test_lost_access_is_reported_like_retirement(engine: MemoryEngine) -> None:
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    engine.set_source_readers(LEGAL_MEETING, ['group:legal'])
    revoked = valid(engine.refresh('t'))  # type: ignore[arg-type]

    other = seeded_engine()
    other.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    other.retire_fact('f-311')
    retired = other.refresh('t')

    assert revoked is not None and retired is not None
    assert revoked.update.changes == retired.update.changes  # type: ignore[union-attr]
    assert revoked.update.summary == retired.update.summary  # type: ignore[union-attr]
    assert revoked.text == retired.text


def test_claim_visibility(engine: MemoryEngine) -> None:
    legal_clears(engine)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    receipt = engine.respond(
        't', TOM_ID, commit(tom_commit(engine.task('t').dossier.version))
    ).receipt  # type: ignore[union-attr]
    claim_id = receipt.recorded[0].fact_id  # type: ignore[union-attr]

    def sees_claim(identity: Identity, task_id: str) -> bool:
        data = {**PRIYA_INTENT, 'onBehalfOf': identity.principal}
        dossier = engine.negotiate(task_id, 'c', identity, intent(data)).dossier
        return claim_id in ids(dossier.facts)  # type: ignore[union-attr]

    # Tom's claim names account:acme (sales, legal) and doc:acme-renewal-quote (sales).
    assert sees_claim(PRIYA_ID, 'p')
    assert not sees_claim(LEGAL_ID, 'l')
    assert sees_claim(Identity('agent:y', 'user:tom', frozenset()), 'tom-without-groups')

    # An entity nobody configured readers for: only the claimant sees the claim.
    engine.set_entity_readers('doc:acme-renewal-quote', None)
    assert not sees_claim(PRIYA_ID, 'p2')


def test_claims_never_become_constraints(engine: MemoryEngine) -> None:
    legal_clears(engine)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    version = engine.task('t').dossier.version  # type: ignore[union-attr]
    receipt = engine.respond('t', TOM_ID, commit(tom_commit(version))).receipt
    claim_id = receipt.recorded[0].fact_id  # type: ignore[union-attr]
    engine.add_policy('c-90', 'Do not send Acme a second quote.', level='must', basis=[claim_id])

    dossier = engine.negotiate('p', 'c', PRIYA_ID, intent(PRIYA_INTENT)).dossier
    assert claim_id in ids(dossier.facts)  # type: ignore[union-attr]
    assert dossier.constraints == []  # type: ignore[union-attr]
    assert engine.fact(claim_id).status == 'claim'  # type: ignore[union-attr]

    # Once a system of record stands behind it, the policy applies.
    engine.confirm_fact(claim_id, by='system:crm')
    reply = valid(engine.refresh('p'))  # type: ignore[arg-type]
    assert [(c.id, c.change) for c in reply.update.changes] == [  # type: ignore[union-attr]
        (claim_id, 'updated'),
        ('c-90', 'added'),
    ]
    assert reply.dossier.constraints[0].basis == [claim_id]  # type: ignore[union-attr]


# ------------------------------------------------------------------ commit
def test_stale_commit_records_nothing(engine: MemoryEngine) -> None:
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    legal_clears(engine)
    engine.refresh('t')
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(first.version))))  # type: ignore[union-attr]
    assert reply.phase == 'awaiting-commit'
    assert reply.error is not None and reply.error.code == 'stale-dossier'
    assert reply.error.current_version == engine.task('t').dossier.version  # type: ignore[union-attr]
    assert (reply.dossier, reply.update) == (None, None)  # the agent already has it
    assert engine.claims() == [] and engine.review_queue == []


def test_commit_before_the_update_arrives_is_stale(engine: MemoryEngine) -> None:
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    legal_clears(engine)  # the update is still on its way when the commit arrives
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(first.version))))  # type: ignore[union-attr]
    assert reply.error is not None and reply.error.code == 'stale-dossier'
    assert reply.dossier is not None and reply.update is not None
    assert reply.error.current_version == reply.dossier.version != first.version  # type: ignore[union-attr]
    assert engine.claims() == []
    assert engine.refresh('t') is None  # nothing more to deliver


def test_commit_records_claims_and_conflicts(engine: MemoryEngine) -> None:
    dossier = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    data = tom_commit(dossier.version, conflicts=[{'id': 'c-17', 'explanation': 'Sent anyway.'}])  # type: ignore[union-attr]
    data['claims'][0]['supersedes'] = ['f-208']
    reply = valid(engine.respond('t', TOM_ID, commit(data)))
    assert reply.phase == 'committed' and reply.receipt is not None
    assert reply.receipt.conflicts_recorded == 1
    [claim] = engine.claims()
    assert claim.status == 'claim' and claim.confirmed_by is None
    assert claim.claimed_by.commit_id == reply.receipt.commit_id  # type: ignore[union-attr]
    assert engine.fact('f-208').retired is False  # memory decides, not the agent
    kinds = [(r.kind, r.item_id) for r in engine.review_queue]
    assert kinds == [('supersede-proposal', 'f-208'), ('conflict', 'c-17')]
    assert engine.task('t').open is False  # type: ignore[union-attr]


def test_invalid_follow_ups_keep_the_phase(engine: MemoryEngine) -> None:
    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    reply = valid(engine.respond('m', MAYA_ID, [(C.ANSWER, {'questionId': 'q-1'})]))
    assert (reply.phase, reply.error.code) == ('question', 'invalid-answer')  # type: ignore[union-attr]
    assert reply.question is not None

    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    for payloads in (
        commit({'basedOn': '1'}),
        answer('q-1', 'Yes'),
        [*commit(tom_commit('1')), *commit(tom_commit('1'))],
    ):
        reply = valid(engine.respond('t', TOM_ID, payloads))
        assert (reply.phase, reply.error.code) == ('awaiting-commit', 'invalid-commit')  # type: ignore[union-attr]
    assert engine.claims() == []


# -------------------------------------------------------------- negotiate
def test_refusals(engine: MemoryEngine) -> None:
    def refused(identity: Identity, data: dict[str, Any]) -> str:
        reply = valid(engine.negotiate('x', 'c', identity, intent(data)))
        assert reply.phase == 'refused' and reply.dossier is None
        return reply.error.code  # type: ignore[union-attr]

    assert refused(TOM_ID, {**TOM_INTENT, 'entities': []}) == 'invalid-intent'
    assert refused(PRIYA_ID, TOM_INTENT) == 'principal-mismatch'
    engine.restrict_action('send_quote', to=['group:sales'])
    assert refused(MAYA_ID, {**TOM_INTENT, 'onBehalfOf': 'user:maya'}) == 'not-authorized'
    engine.add_refusal('r-1', 'Memory does not advise on layoffs.', actions=['plan_layoffs'])
    assert refused(TOM_ID, {**TOM_INTENT, 'action': 'plan_layoffs'}) == 'refused'


def test_question_phase_is_not_watched(engine: MemoryEngine) -> None:
    notified: list[list[str]] = []
    engine.on_change(notified.append)
    reply = engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    assert reply.phase == 'question'
    engine.update_fact('f-501', statement="Titan's launch moved to November 27.")
    assert notified == [] and engine.refresh('m') is None


def test_changes_notify_only_affected_tasks(engine: MemoryEngine) -> None:
    notified: list[list[str]] = []
    engine.on_change(notified.append)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    engine.respond('m', MAYA_ID, answer('q-1', 'Yes'))
    legal_clears(engine)
    engine.update_fact('f-502', statement='Priya and Sam own the rewrite.')
    assert notified == [['t'], ['m']]


def test_cancel_and_expiry() -> None:
    now = [datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)]
    engine = MemoryEngine(watch_timeout=timedelta(minutes=5), clock=lambda: now[0])
    seed_acme(engine)
    engine.negotiate('a', 'c', TOM_ID, intent(TOM_INTENT))
    engine.negotiate('b', 'c', TOM_ID, intent(TOM_INTENT))
    assert valid(engine.cancel('a')).phase == 'canceled'  # type: ignore[arg-type]
    assert engine.cancel('a') is None

    now[0] += timedelta(minutes=5)
    assert engine.due_for_expiry() == ['b']
    reply = valid(engine.expire('b'))  # type: ignore[arg-type]
    assert (reply.phase, reply.error.code) == ('expired', 'watch-expired')  # type: ignore[union-attr]
    assert engine.open_tasks() == [] and engine.claims() == []
