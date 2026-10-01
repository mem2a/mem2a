# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the engine's rules, without A2A."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from mem2a import constants as C
from mem2a import seeds, validation
from mem2a.auth import DevTokenAuthenticator, Identity
from mem2a.engine import MemoryEngine, Reply, TaskRecord, When
from mem2a.models import Commit
from mem2a.seeds import seed_acme

from stories import (
    LEGAL_MEETING,
    MAYA,
    MAYA_INTENT,
    PRIYA,
    PRIYA_INTENT,
    TOM,
    TOM_INTENT,
    legal_clears,
    seeded_engine,
    tom_commit,
)


EXAMPLES = Path(__file__).resolve().parents[2] / 'spec' / 'v0.1' / 'examples'
needs_examples = pytest.mark.skipif(not EXAMPLES.is_dir(), reason='needs the spec examples')

TOM_ID = DevTokenAuthenticator.parse(TOM)
PRIYA_ID = DevTokenAuthenticator.parse(PRIYA)
MAYA_ID = DevTokenAuthenticator.parse(MAYA)
COUNSEL = Identity('agent:counsel-bot', 'user:general-counsel', frozenset({'group:legal'}))


def intent(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(C.INTENT, data)]


def commit(data: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(C.COMMIT, data)]


def answer(question_id: str, text: str) -> list[tuple[str, Any]]:
    return [(C.ANSWER, {'questionId': question_id, 'text': text})]


def valid(reply: Reply | None) -> Reply:
    """Every payload the engine produces validates against its schema."""
    assert reply is not None
    for kind in ('dossier', 'receipt', 'question', 'update', 'error'):
        payload = getattr(reply, kind)
        if payload is not None:
            validation.validate(kind, payload.dump())
    return reply


def ids(items: list[Any]) -> list[str]:
    return [item.id for item in items]


def changes(reply: Reply | None) -> list[tuple[str, str]]:
    assert reply is not None and reply.update is not None
    return [(c.id, c.change) for c in reply.update.changes]


def version(engine: MemoryEngine, task_id: str) -> str:
    task = engine.task(task_id)
    assert task is not None and task.dossier is not None
    return task.dossier.version


def _walk(node: Any) -> Iterator[Any]:
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        yield from _walk(child)


def example(name: str, media_type: str, index: int = 0) -> dict[str, Any]:
    parts = [
        part['data']
        for part in _walk(json.loads((EXAMPLES / name).read_text()))
        if isinstance(part, dict) and part.get('mediaType') == media_type
    ]
    return parts[index]  # type: ignore[no-any-return]


def without(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in keys}


# ------------------------------------------------------- the spec's stories
@needs_examples
def test_acme_dossiers_match_the_spec_examples() -> None:
    engine = seeds.seeded_engine('acme')
    first = valid(engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))).dossier
    assert first is not None and first.version == '12'
    loose = ('summary', 'expiresAt')  # wording and the clock are memory's own
    assert without(first.dump(), *loose) == without(example('02-negotiate.json', C.DOSSIER), *loose)

    legal_clears(engine)
    reply = valid(engine.refresh('t'))
    assert reply.dossier is not None and reply.dossier.version == '13'
    assert without(reply.dossier.dump(), *loose) == without(
        example('03-listen-update.json', C.DOSSIER), *loose
    )
    assert without(reply.update.dump(), 'summary') == without(  # type: ignore[union-attr]
        example('03-listen-update.json', C.UPDATE), 'summary'
    )

    priya = valid(engine.negotiate('p', 'c', PRIYA_ID, intent(PRIYA_INTENT))).dossier
    assert priya is not None
    assert without(priya.dump(), *loose) == without(
        example('09-listen-stream-claim.json', C.DOSSIER, 0), *loose
    )

    # Example 04: a commit against 12 is told what changed since.
    stale = valid(engine.respond('t', TOM_ID, commit(tom_commit('12'))))
    assert stale.error is not None and stale.error.dump() == example(
        '04-commit-stale.json', C.ERROR
    )
    assert without(stale.update.dump(), 'summary') == without(  # type: ignore[union-attr]
        example('04-commit-stale.json', C.UPDATE), 'summary'
    )

    # Example 09: Tom's claim reaches Priya as dossier 15.
    done = valid(engine.respond('t', TOM_ID, commit(tom_commit('13'))))
    assert done.receipt is not None and done.receipt.conflicts == []
    claim_id = done.receipt.recorded[0].fact_id
    reply = valid(engine.refresh('p'))
    assert reply.dossier is not None and reply.dossier.version == '15'
    expected = example('09-listen-stream-claim.json', C.DOSSIER, 1)
    ours = reply.dossier.dump()
    assert [f['id'] for f in ours['facts']] == ['f-340', 'f-208', claim_id]  # claims last
    claim, theirs = ours['facts'][2], expected['facts'][2]
    assert sorted(claim) == sorted(theirs)
    assert claim['source']['kind'] == 'agent-commit'
    assert claim['source']['ref'] == f'commit:{claim["claimedBy"]["commitId"]}'
    assert claim['evidence'] == theirs['evidence']
    assert changes(reply) == [(claim_id, 'added')]


@needs_examples
def test_titan_dossier_matches_the_spec_example() -> None:
    engine = seeds.seeded_engine('titan')
    question = valid(engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT)))
    assert question.question is not None
    assert question.question.dump() == example('06-question.json', C.QUESTION)
    dossier = valid(engine.respond('m', MAYA_ID, answer('q-1', 'No.'))).dossier
    assert dossier is not None and dossier.version == '7'
    loose = ('summary', 'expiresAt')
    assert without(dossier.dump(), *loose) == without(example('07-answer.json', C.DOSSIER), *loose)


# ----------------------------------------------------------------- versions
def test_updates_follow_item_versions() -> None:
    engine = seeded_engine()
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    engine.update_fact('f-208', statement="Acme's contract renews on November 30, 2026.")
    engine.update_precedent('p-4', relevance='Same situation, and the same counsel.')
    engine.update_policy('c-17', statement='Do not send Acme any pricing until legal clears it.')
    engine.add_source('crm:acme-contacts', kind='record')
    engine.add_fact(
        'f-400',
        'Acme has a new procurement lead.',
        source='crm:acme-contacts',
        confirmed_by='system:crm',
        entities=['account:acme'],
    )
    reply = valid(engine.refresh('t'))
    assert changes(reply) == [
        ('f-400', 'added'),
        ('f-208', 'updated'),
        ('c-17', 'updated'),
        ('p-4', 'updated'),
    ]
    assert reply.update is not None and reply.dossier is not None
    assert reply.update.previous_version == first.version  # type: ignore[union-attr]
    items = {i.id: i.version for i in (*reply.dossier.facts, *reply.dossier.constraints)}
    assert (items['f-208'], items['c-17']) == ('2', '2')
    assert reply.dossier.precedent[0].version == '2'
    assert engine.refresh('t') is None  # nothing changed since


def test_wording_alone_never_produces_an_update() -> None:
    summaries = iter(f'Brief number {n}.' for n in range(100))
    engine = seeded_engine(summarize=lambda *items: next(summaries))
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    before = version(engine, 't')
    engine.set_source_readers(LEGAL_MEETING, ['group:sales', 'group:legal', 'group:exec'])
    assert engine.refresh('t') is None
    assert version(engine, 't') == before


def test_versions_are_memory_wide_strings() -> None:
    engine = seeded_engine()
    a = engine.negotiate('a', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    b = engine.negotiate('b', 'c', PRIYA_ID, intent(PRIYA_INTENT)).dossier
    legal_clears(engine)
    a2 = valid(engine.refresh('a')).dossier
    assert a is not None and b is not None and a2 is not None
    assert [a.version, b.version, a2.version] == ['12', '13', '14']


def test_ids_are_shared_across_kinds_and_urls_are_https() -> None:
    engine = seeded_engine()
    with pytest.raises(ValueError, match='already in use'):
        engine.add_policy('f-311', 'A rule.', level='should', policy='policy:x')
    with pytest.raises(ValueError, match='https'):
        engine.add_source('doc:plan', kind='document', url='http://example.com/plan')
    with pytest.raises(ValueError, match='commits'):
        engine.add_source('commit:cm-0', kind='agent-commit')
    with pytest.raises(ValueError, match='only confirmed facts'):
        engine.add_fact(
            'f-900',
            'A replacement.',
            source=LEGAL_MEETING,
            confirmed_by='user:x',
            entities=['account:acme'],
            supersedes=['p-4'],
        )


# ---------------------------------------------------------- facts and claims
def test_retired_and_superseded_facts_leave_dossiers() -> None:
    engine = seeded_engine()
    legal_clears(engine)
    dossier = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert dossier is not None
    assert ids(dossier.facts) == ['f-340', 'f-208']
    assert dossier.facts[0].supersedes == ['f-311']
    assert (dossier.constraints, dossier.precedent) == ([], [])

    engine.retire_fact('f-208', note='The CRM record was a duplicate.')
    reply = valid(engine.refresh('t'))
    assert changes(reply) == [('f-208', 'removed')]
    assert reply.update is not None and 'duplicate' not in reply.update.summary


def test_permissions_follow_every_source() -> None:
    engine = seeded_engine()
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
    assert tom is not None and cfo is not None
    assert 'f-500' not in ids(tom.facts)
    # The CFO can read both of f-500's sources, but not f-311's; what rests
    # on f-311 goes with it.
    assert 'f-500' in ids(cfo.facts) and 'f-311' not in ids(cfo.facts)
    assert (cfo.constraints, cfo.precedent) == ([], [])


def test_lost_access_reads_exactly_like_retirement() -> None:
    revoked = seeded_engine()
    revoked.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    revoked.set_source_readers(LEGAL_MEETING, ['group:legal'])

    retired = seeded_engine()
    retired.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    retired.retire_fact('f-311', note='Legal withdrew the pause.')

    a, b = valid(revoked.refresh('t')), valid(retired.refresh('t'))
    assert a.update is not None and b.update is not None
    assert (a.update.changes, a.update.summary, a.text) == (
        b.update.changes,
        b.update.summary,
        b.text,
    )


def test_claim_visibility() -> None:
    engine = seeded_engine()
    legal_clears(engine)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    receipt = engine.respond('t', TOM_ID, commit(tom_commit(version(engine, 't')))).receipt
    assert receipt is not None
    claim_id = receipt.recorded[0].fact_id

    def sees_claim(identity: Identity, task_id: str) -> bool:
        data = {**PRIYA_INTENT, 'onBehalfOf': identity.principal}
        dossier = engine.negotiate(task_id, 'c', identity, intent(data)).dossier
        assert dossier is not None
        return claim_id in ids(dossier.facts)

    # The claim names account:acme (sales, legal) and doc:acme-renewal-quote (sales).
    assert sees_claim(PRIYA_ID, 'p')
    assert not sees_claim(COUNSEL, 'l')
    assert sees_claim(Identity('agent:y', 'user:tom', frozenset()), 'tom-without-groups')
    # Without readers for an entity, only the claimant sees claims naming it.
    engine.set_entity_readers('doc:acme-renewal-quote', None)
    assert not sees_claim(PRIYA_ID, 'p2')


def test_claims_cannot_launder_what_the_claimant_read() -> None:
    engine = seeded_engine()
    # Everyone may read claims about account:acme, but Tom's dossier rests on
    # f-311, from a meeting only sales and legal may read.
    engine.set_entity_readers('account:acme', ['group:everyone'])
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    data = tom_commit(version(engine, 't'))
    data['claims'][0]['entities'] = ['account:acme']
    data['claims'][0]['statement'] = 'Legal paused Acme pricing, so the quote waits.'
    claim_id = engine.respond('t', TOM_ID, commit(data)).receipt.recorded[0].fact_id  # type: ignore[union-attr]

    outsider = Identity('agent:z', 'user:zoe', frozenset({'group:everyone'}))
    dossier = engine.negotiate(
        'z', 'c', outsider, intent({**PRIYA_INTENT, 'onBehalfOf': 'user:zoe'})
    ).dossier
    assert dossier is not None and claim_id not in ids(dossier.facts)

    # Once Zoe may read the meeting too, she sees the claim.
    engine.set_source_readers(LEGAL_MEETING, ['group:sales', 'group:legal', 'group:everyone'])
    reply = valid(engine.refresh('z'))
    assert (claim_id, 'added') in changes(reply)


def test_claims_never_become_constraints() -> None:
    engine = seeded_engine()
    legal_clears(engine)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    receipt = engine.respond('t', TOM_ID, commit(tom_commit(version(engine, 't')))).receipt
    claim_id = receipt.recorded[0].fact_id  # type: ignore[union-attr]
    engine.add_policy('c-90', 'Do not send Acme a second quote.', level='must', basis=[claim_id])

    dossier = engine.negotiate('p', 'c', PRIYA_ID, intent(PRIYA_INTENT)).dossier
    assert dossier is not None
    assert claim_id in ids(dossier.facts) and dossier.constraints == []
    assert engine.fact(claim_id).status == 'claim'  # type: ignore[union-attr]

    # Once a system of record stands behind it, the policy applies.
    engine.confirm_fact(claim_id, by='system:crm')
    reply = valid(engine.refresh('p'))
    assert changes(reply) == [(claim_id, 'updated'), ('c-90', 'added')]
    fact = next(f for f in reply.dossier.facts if f.id == claim_id)  # type: ignore[union-attr]
    assert (fact.status, fact.confirmed_by, fact.claimed_by) == ('confirmed', 'system:crm', None)
    assert fact.source.kind == 'agent-commit'


def test_replaces_is_only_a_hint() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    data = tom_commit(version(engine, 't'))
    data['claims'][0]['replaces'] = ['f-208', 'f-311']
    engine.respond('t', TOM_ID, commit(data))
    assert not engine.fact('f-208').retired and not engine.fact('f-311').retired  # type: ignore[union-attr]
    assert [(r.kind, r.item_id) for r in engine.review_queue] == [
        ('replace-proposal', 'f-208'),
        ('replace-proposal', 'f-311'),
    ]


# -------------------------------------------------------------------- commit
def test_stale_commit_records_nothing() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    first = version(engine, 't')
    legal_clears(engine)
    engine.refresh('t')
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(first))))
    assert reply.phase == 'awaiting-commit'
    assert reply.error is not None and reply.error.code == 'stale-dossier'
    assert reply.error.current_version == version(engine, 't')
    assert reply.dossier is None  # the agent was sent it already
    assert changes(reply) == [
        ('f-340', 'added'),
        ('f-311', 'removed'),
        ('c-17', 'removed'),
        ('p-4', 'removed'),
    ]
    assert engine.claims() == [] and engine.review_queue == []

    unknown = valid(engine.respond('t', TOM_ID, commit(tom_commit('does-not-exist'))))
    assert unknown.error is not None and unknown.error.code == 'stale-dossier'
    assert unknown.update is None  # nothing to compare with


def test_commit_before_the_update_arrives_is_stale() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    first = version(engine, 't')
    legal_clears(engine)  # the update is still on its way when the commit arrives
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(first))))
    assert reply.error is not None and reply.error.code == 'stale-dossier'
    assert reply.dossier is not None and reply.update is not None
    assert reply.error.current_version == reply.dossier.version != first
    assert engine.claims() == []
    assert engine.refresh('t') is None  # nothing more to deliver


def test_commit_records_claims_and_conflicts() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    conflicts = [
        {'id': 'c-17', 'explanation': 'Sent anyway.'},
        {'id': 'c-17', 'explanation': 'Twice, in fact.'},
        {'id': 'p-4', 'explanation': 'Like Globex.'},
    ]
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(version(engine, 't'), conflicts))))
    assert reply.phase == 'committed' and reply.receipt is not None
    assert reply.receipt.conflicts == ['c-17', 'p-4']
    [claim] = engine.claims()
    assert claim.status == 'claim' and claim.confirmed_by is None
    assert claim.origin is not None and claim.origin.commit_id == reply.receipt.commit_id
    assert claim.evidence[0].ref == 'email:<CAF1e2d3-acme-quote@mail.example.com>'
    assert [(r.kind, r.item_id) for r in engine.review_queue] == [
        ('conflict', 'c-17'),
        ('conflict', 'c-17'),
        ('conflict', 'p-4'),
    ]
    assert engine.task('t').open is False  # type: ignore[union-attr]


def test_conflicts_must_name_items_of_the_dossier() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    data = tom_commit(version(engine, 't'), [{'id': 'f-501', 'explanation': 'Not in it.'}])
    reply = valid(engine.respond('t', TOM_ID, commit(data)))
    assert (reply.phase, reply.error.code) == ('awaiting-commit', 'invalid-commit')  # type: ignore[union-attr]
    assert engine.claims() == []


def test_a_commit_policy_can_refuse() -> None:
    def no_claims_on_fridays(task: TaskRecord, proposed: Commit) -> str | None:
        return (
            'Commits from this agent are paused.' if task.identity.agent == TOM_ID.agent else None
        )

    engine = seeded_engine(commit_policy=no_claims_on_fridays)
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    reply = valid(engine.respond('t', TOM_ID, commit(tom_commit(version(engine, 't')))))
    assert (reply.phase, reply.error.code) == ('awaiting-commit', 'commit-refused')  # type: ignore[union-attr]
    assert reply.error.message == 'Commits from this agent are paused.'  # type: ignore[union-attr]
    assert engine.claims() == []


# ------------------------------------------------------- unexpected messages
def test_unexpected_messages_change_nothing() -> None:
    engine = seeded_engine()
    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    before = version(engine, 't')
    cases = [
        ('m', MAYA_ID, commit(tom_commit('1'))),  # a commit while asking
        ('m', MAYA_ID, intent(MAYA_INTENT)),  # an intent on an existing task
        ('m', MAYA_ID, []),  # no payload
        ('t', TOM_ID, answer('q-1', 'Yes')),  # an answer while awaiting a commit
        ('t', TOM_ID, [*commit(tom_commit(before)), *commit(tom_commit(before))]),  # two
    ]
    for task_id, identity, payloads in cases:
        task = engine.task(task_id)
        assert task is not None
        phase = task.phase
        reply = valid(engine.respond(task_id, identity, payloads))
        assert (reply.phase, reply.error.code) == (phase, 'unexpected-message')  # type: ignore[union-attr]
        assert (reply.question is not None) == (phase == 'question')  # asked again
    assert version(engine, 't') == before and engine.claims() == []


def test_invalid_answers_and_commits_keep_the_phase() -> None:
    engine = seeded_engine()
    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    reply = valid(engine.respond('m', MAYA_ID, [(C.ANSWER, {'questionId': 'q-1'})]))
    assert (reply.phase, reply.error.code) == ('question', 'invalid-answer')  # type: ignore[union-attr]
    assert reply.question is not None
    reply = valid(engine.respond('m', MAYA_ID, answer('q-9', 'No.')))
    assert (reply.phase, reply.error.code) == ('question', 'unknown-question')  # type: ignore[union-attr]

    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    reply = valid(engine.respond('t', TOM_ID, commit({'basedOn': '1'})))
    assert (reply.phase, reply.error.code) == ('awaiting-commit', 'invalid-commit')  # type: ignore[union-attr]


# ---------------------------------------------------------------- negotiate
def test_refusals() -> None:
    engine = seeded_engine(max_open_tasks=1)

    def refused(identity: Identity, payloads: list[tuple[str, Any]]) -> str:
        reply = valid(engine.negotiate('x', 'c', identity, payloads))
        assert reply.phase == 'refused' and reply.dossier is None
        return reply.error.code  # type: ignore[union-attr]

    assert refused(TOM_ID, intent({**TOM_INTENT, 'entities': []})) == 'invalid-intent'
    assert refused(TOM_ID, commit(tom_commit('1'))) == 'unexpected-message'
    assert refused(TOM_ID, []) == 'unexpected-message'
    assert refused(PRIYA_ID, intent(TOM_INTENT)) == 'principal-mismatch'
    engine.restrict_action('send_quote', to=['group:sales'])
    assert refused(MAYA_ID, intent({**TOM_INTENT, 'onBehalfOf': 'user:maya'})) == 'not-authorized'
    engine.add_refusal('r-1', 'Memory does not advise on layoffs.', actions=['plan_layoffs'])
    assert refused(TOM_ID, intent({**TOM_INTENT, 'action': 'plan_layoffs'})) == 'refused'
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))  # Tom's one open task
    assert refused(TOM_ID, intent(TOM_INTENT)) == 'limit-exceeded'
    assert (
        valid(engine.negotiate('p', 'c', PRIYA_ID, intent(PRIYA_INTENT))).phase == 'awaiting-commit'
    )


def test_question_phase_is_not_watched() -> None:
    engine = seeded_engine()
    notified: list[list[str]] = []
    engine.on_change(notified.append)
    assert engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT)).phase == 'question'
    engine.update_fact('f-501', statement="Titan's launch moved to November 27.")
    assert notified == [] and engine.refresh('m') is None


def test_changes_notify_only_affected_tasks() -> None:
    engine = seeded_engine()
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
    engine.add_question('q-1', 'Which quarter?', actions=['plan'])
    engine.negotiate('a', 'c', TOM_ID, intent(TOM_INTENT))
    engine.negotiate('b', 'c', TOM_ID, intent(TOM_INTENT))
    engine.negotiate('q', 'c', TOM_ID, intent({**TOM_INTENT, 'action': 'plan'}))
    assert valid(engine.cancel('a')).phase == 'canceled'
    assert engine.cancel('a') is None

    now[0] += timedelta(minutes=5)
    assert engine.due_for_expiry() == ['b', 'q']  # an unanswered question expires too
    for task_id in ('b', 'q'):
        reply = valid(engine.expire(task_id))
        assert (reply.phase, reply.error.code) == ('expired', 'watch-expired')  # type: ignore[union-attr]
    assert engine.open_tasks() == [] and engine.claims() == []


def test_failures_end_the_task() -> None:
    engine = seeded_engine()
    engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT))
    reply = valid(engine.fail('t'))
    assert reply.phase == 'failed' and reply.error is not None
    assert (reply.error.code, reply.error.message) == ('internal', 'Internal error.')
    assert engine.task('t').open is False  # type: ignore[union-attr]


# --------------------------------------------------------- reading dossiers
def test_stored_dossiers_are_filtered_by_current_access() -> None:
    engine = seeded_engine()
    dossier = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert dossier is not None
    engine.respond('t', TOM_ID, commit(tom_commit(dossier.version)))  # the task is over
    assert engine.visible_dossier('t', dossier) is dossier

    engine.set_source_readers(LEGAL_MEETING, ['group:legal'])
    shown = engine.visible_dossier('t', dossier)
    validation.validate('dossier', shown.dump())
    assert ids(shown.facts) == ['f-208'] and (shown.precedent, shown.constraints) == ([], [])
    assert shown.watching == ['f-208'] and shown.version.startswith(f'{dossier.version}-redacted-')
    assert 'legal' not in shown.summary.lower()

    # The caller's own credentials count too: the narrower access wins.
    engine.set_source_readers(LEGAL_MEETING, ['group:sales'])
    assert engine.visible_dossier('t', dossier) is dossier
    without_groups = Identity(TOM_ID.agent, TOM_ID.principal, frozenset())
    assert ids(engine.visible_dossier('t', dossier, without_groups).facts) == ['f-208']


def test_when_conditions() -> None:
    engine = seeded_engine()
    engine.add_source('decision:acme-discounts', kind='decision')
    engine.add_precedent(
        'p-9',
        'Acme was refused a discount last year.',
        relevance='Same customer, same ask.',
        source='decision:acme-discounts',
        when=[When(entities={'account:acme'}, facts={'f-340'})],
    )
    dossier = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert dossier is not None and 'p-9' not in ids(dossier.precedent)
    legal_clears(engine)  # f-340 is now a confirmed fact in the dossier
    assert ('p-9', 'added') in changes(engine.refresh('t'))
