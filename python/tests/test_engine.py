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

    # A commit against 12 records nothing and is told what changed since.
    stale = valid(engine.respond('t', TOM_ID, commit(tom_commit('12'))))
    assert stale.error is not None and stale.error.code == 'stale-dossier'
    assert stale.error.current_version == '13'
    assert stale.update is not None and stale.update.previous_version == '12'
    assert engine.claims() == []

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
def test_globex_stale_commit_matches_the_spec_example() -> None:
    """Example 04: refused against 20, then recorded against 21 with a conflict."""
    engine = MemoryEngine(first_version=20)
    engine.add_source('crm:opportunity/globex-renewal-2026', kind='record')
    engine.add_fact(
        'f-401',
        'Globex asked for a 15% discount on its renewal.',
        source='crm:opportunity/globex-renewal-2026',
        confirmed_by='system:crm',
        entities=['account:globex', 'deal:globex-renewal-2026'],
    )
    sam = DevTokenAuthenticator.parse('dev:deal-desk-assistant:user:sam:group:sales')
    sam_intent = {
        'action': 'approve_discount',
        'summary': 'Approve a 15% renewal discount for Globex',
        'entities': ['account:globex', 'deal:globex-renewal-2026'],
        'onBehalfOf': 'user:sam',
    }
    first = valid(engine.negotiate('s', 'c', sam, intent(sam_intent))).dossier
    assert first is not None and first.version == '20' and first.watching == ['f-401']

    # 10:05: finance freezes discounts, and a policy turns the freeze into c-50.
    engine.add_source(
        'email:finance-discount-freeze-2026-09-30',
        kind='email',
        title='Discount freeze, effective now',
        at=datetime(2026, 9, 30, 10, 5, tzinfo=timezone.utc),
    )
    engine.add_fact(
        'f-410',
        'Finance froze discounts above 10% for the rest of the quarter.',
        source='email:finance-discount-freeze-2026-09-30',
        confirmed_by='user:cfo',
        entities=['account:globex', 'policy:discounts'],
    )
    engine.add_policy(
        'c-50',
        "Don't approve discounts above 10% this quarter without the CFO's sign-off.",
        level='must',
        basis=['f-410'],
        actions=['approve_discount'],
        until='The quarter ends, or the CFO signs off.',
    )

    # Sam's agent reports against 20 before it hears about 21.
    name = '04-commit-stale.json'
    stale = valid(engine.respond('s', sam, commit(example(name, C.COMMIT, 0))))
    assert stale.error is not None and stale.error.dump() == example(name, C.ERROR)
    assert stale.update is not None
    assert without(stale.update.dump(), 'summary') == without(example(name, C.UPDATE), 'summary')
    assert stale.dossier is not None
    loose = ('summary', 'expiresAt')  # wording and the clock are memory's own
    assert without(stale.dossier.dump(), *loose) == without(example(name, C.DOSSIER), *loose)
    assert engine.claims() == [] and engine.review_queue == []

    # It commits again against 21, naming the constraint its action went against.
    done = valid(engine.respond('s', sam, commit(example(name, C.COMMIT, 1))))
    assert done.receipt is not None and done.receipt.based_on == '21'
    expected = example(name, C.RECEIPT)
    assert done.receipt.conflicts == expected['conflicts'] == ['c-50']
    assert [r.status for r in done.receipt.recorded] == ['claim']
    assert [(r.kind, r.item_id) for r in engine.review_queue] == [('conflict', 'c-50')]


@needs_examples
def test_titan_dossier_matches_the_spec_example() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
    engine = seeds.seeded_engine('titan', clock=lambda: now)
    question = valid(engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT)))
    assert question.question is not None
    loose = ('summary', 'expiresAt')  # wording and the clock are memory's own
    assert without(question.question.dump(), *loose) == without(
        example('06-question.json', C.QUESTION), *loose
    )
    # Memory declares a watchTimeout (P7D), so the question says when it lapses.
    assert question.question.expires_at == now + timedelta(days=7)
    dossier = valid(engine.respond('m', MAYA_ID, answer('q-1', 'No.'))).dossier
    assert dossier is not None and dossier.version == '7'
    assert dossier.expires_at == now + timedelta(days=7)
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


def test_summaries_only_see_what_the_principal_may_see() -> None:
    # Spec 11.3: access filtering happens before any model (here, the
    # pluggable summarizer) sees candidate items.
    seen: list[str] = []

    def summarize(facts: Any, precedent: Any, constraints: Any) -> str:
        seen.extend(item.id for item in (*facts, *precedent, *constraints))
        return 'A brief.'

    engine = seeded_engine(summarize=summarize)
    zoe = Identity('agent:x', 'user:zoe', frozenset())
    engine.negotiate('z', 'c', zoe, intent({**TOM_INTENT, 'onBehalfOf': 'user:zoe'}))
    assert seen == ['f-208']


def test_drafts_stay_in_their_task() -> None:
    # Spec 10.6: memory never shows a draft to anyone else, or learns from it.
    engine = seeded_engine()
    engine.negotiate('m', 'c', MAYA_ID, intent(MAYA_INTENT))
    engine.respond('m', MAYA_ID, answer('q-1', 'No.'))
    sam = Identity('agent:y', 'user:sam', frozenset({'group:leadership'}))
    sams = {**without(MAYA_INTENT, 'draft'), 'onBehalfOf': 'user:sam'}
    replies = [engine.negotiate('s', 'c', sam, intent(sams))]
    replies.append(engine.respond('s', sam, answer('q-1', 'No.')))
    shown = json.dumps([r.dossier.dump() if r.dossier else r.text for r in replies])
    assert MAYA_INTENT['draft'] not in shown
    assert all(MAYA_INTENT['draft'] not in f.statement for f in engine.facts())


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
def test_receivers_ignore_members_this_version_does_not_define() -> None:
    engine = seeded_engine()
    data = {**TOM_INTENT, 'priority': 'high'}  # spec 2.5: ignored, not refused
    reply = valid(engine.negotiate('t', 'c', TOM_ID, intent(data)))
    assert reply.phase == 'awaiting-commit'
    assert 'priority' not in engine.task('t').intent.dump()  # type: ignore[union-attr]

    report = tom_commit(version(engine, 't'))
    report['claims'][0]['confidence'] = 'high'
    report['claims'][0]['evidence'][0]['sha256'] = 'f00d'
    done = valid(engine.respond('t', TOM_ID, commit({**report, 'reviewedBy': 'user:x'})))
    assert done.phase == 'committed'
    [claim] = engine.claims()
    assert claim.evidence[0].dump() == tom_commit('x')['claims'][0]['evidence'][0]

    # What this version does define is still validated.
    invalid = {**TOM_INTENT, 'priority': 'high', 'entities': 'account:acme'}
    reply = valid(engine.negotiate('u', 'c', TOM_ID, intent(invalid)))
    assert (reply.phase, reply.error.code) == ('refused', 'invalid-intent')  # type: ignore[union-attr]


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


# ------------------------------------------------------------------ access
def test_a_request_with_other_groups_re_evaluates_an_open_task() -> None:
    engine = seeded_engine()
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert first is not None and ids(first.facts) == ['f-311', 'f-208']
    # Someone else's request changes nothing, and neither do Tom's same groups.
    assert not engine.use_access('t', PRIYA_ID)
    assert not engine.use_access('t', TOM_ID)

    # Tom has left group:sales, which f-311's source needs (spec 10.3).
    without_groups = Identity(TOM_ID.agent, TOM_ID.principal, frozenset())
    assert engine.use_access('t', without_groups)
    reply = valid(engine.refresh('t'))  # an ordinary new version, and an update
    assert reply.dossier is not None and reply.dossier.version == '13'
    assert ids(reply.dossier.facts) == ['f-208']
    assert (reply.dossier.precedent, reply.dossier.constraints) == ([], [])
    assert changes(reply) == [('f-311', 'removed'), ('c-17', 'removed'), ('p-4', 'removed')]
    # A commit against that version is recorded.
    done = valid(engine.respond('t', without_groups, commit(tom_commit('13'))))
    assert done.phase == 'committed' and done.receipt is not None


def test_finished_tasks_follow_access_without_updates() -> None:
    engine = seeded_engine()
    notified: list[list[str]] = []
    engine.on_change(notified.append)
    first = engine.negotiate('t', 'c', TOM_ID, intent(TOM_INTENT)).dossier
    assert first is not None
    engine.respond('t', TOM_ID, commit(tom_commit(first.version)))  # the task is over
    assert engine.refilter('t') is None  # nothing changed

    engine.set_source_readers(LEGAL_MEETING, ['group:legal'])
    assert notified[-1] == ['t']  # the finished task is re-evaluated too
    assert engine.refresh('t') is None  # but gets no update: it is over
    shown = engine.refilter('t')
    assert shown is not None and shown.version == '13'
    validation.validate('dossier', shown.dump())
    assert ids(shown.facts) == ['f-208'] and (shown.precedent, shown.constraints) == ([], [])
    assert shown.watching == ['f-208'] and 'legal' not in shown.summary.lower()
    assert engine.task('t').dossier == shown  # type: ignore[union-attr]
    assert engine.refilter('t') is None  # stable until access changes again

    # Access comes back, and so do the items, under another new version.
    engine.set_source_readers(LEGAL_MEETING, ['group:sales', 'group:legal'])
    again = engine.refilter('t')
    assert again is not None and again.version == '14'
    assert again.watching == first.watching

    # A request with other groups re-evaluates a finished task the same way.
    assert engine.use_access('t', Identity(TOM_ID.agent, TOM_ID.principal, frozenset()))
    assert ids(engine.refilter('t').facts) == ['f-208']  # type: ignore[union-attr]


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
