# SPDX-License-Identifier: Apache-2.0
"""End to end, over real HTTP: Mem2AClient and raw JSON-RPC against the server."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from a2a.utils.errors import UnsupportedOperationError
from google.protobuf.json_format import MessageToDict

from mem2a import (
    EXTENSION_URI,
    MemoryEngine,
    PushTarget,
    dossier_of,
    error_of,
    phase_of,
    question_of,
    receipt_of,
    state_of,
    text_of,
    update_of,
    validation,
)
from mem2a.client import parse_push
from mem2a.constants import INTENT

from conftest import MakeMemory, Memory
from stories import (
    LEGAL_MEETING,
    MAYA,
    MAYA_INTENT,
    PRIYA,
    PRIYA_AS_SALES_ASSISTANT,
    PRIYA_INTENT,
    TOM,
    TOM_INTENT,
    legal_clears,
    seed_acme,
    tom_commit,
)
from wire import check


def changes(update: object) -> list[tuple[str, str]]:
    assert update is not None
    return [(c.id, c.change) for c in update.changes]  # type: ignore[attr-defined]


def intent_request(intent: dict[str, object], message_id: str = 'msg-1') -> dict[str, object]:
    return {
        'message': {
            'messageId': message_id,
            'role': 'ROLE_USER',
            'parts': [{'mediaType': INTENT, 'data': intent}],
            'extensions': [EXTENSION_URI],
        }
    }


async def test_agent_card_declares_mem2a(memory: Memory) -> None:
    response = await memory.http.get(f'{memory.url}/.well-known/agent-card.json')
    card = response.json()
    [entry] = [e for e in card['capabilities']['extensions'] if e['uri'] == EXTENSION_URI]
    assert entry['required'] is True
    validation.validate('extension-params', entry['params'])
    assert entry['params']['listen'] == ['push', 'subscribe']
    assert entry['params']['watchTimeoutSeconds'] == 7 * 24 * 3600
    assert card['capabilities']['streaming'] is True
    assert card['capabilities']['pushNotifications'] is True
    assert [s['id'] for s in card['skills']] == ['negotiate', 'commit']
    scheme = card['securitySchemes']['dev-token']['httpAuthSecurityScheme']
    assert scheme['scheme'] == 'Bearer'
    assert card['securityRequirements'] == [{'schemes': {'dev-token': {}}}]


async def test_acme_story_with_push_and_subscribe(memory: Memory) -> None:
    tom = await memory.client(TOM)
    push = PushTarget(memory.webhook_url, token='tok-tom', bearer='single-use-secret')

    # Negotiate: the dossier comes back in the blocking response (artifact first).
    task = check(await tom.negotiate(TOM_INTENT, push=push))
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
    first = dossier_of(task)
    assert first is not None
    assert text_of(task) == first.summary
    assert [c.id for c in first.constraints] == ['c-17']
    assert set(first.watching) == {'f-311', 'f-208', 'p-4', 'c-17'}

    # Listen both ways: a SubscribeToTask stream, and the webhook.
    stream = memory.collect(tom.subscribe(task.id))
    await stream.wait_for(lambda events: len(events) >= 1)
    assert stream.items[0].WhichOneof('payload') == 'task'
    await memory.webhook.wait_for(lambda calls: len(calls) >= 3)  # the first turn
    pushed = len(memory.webhook.items)

    legal_clears(memory.engine)

    await memory.webhook.wait_for(lambda calls: len(calls) >= pushed + 2)
    artifact_call, status_call = memory.webhook.items[pushed : pushed + 2]
    for call in (artifact_call, status_call):
        assert call['headers']['authorization'] == 'Bearer single-use-secret'
        assert call['headers']['x-a2a-notification-token'] == 'tok-tom'
    second = dossier_of(artifact_call['body'])
    update = update_of(status_call['body'])
    assert second is not None and update is not None
    assert (update.previous_version, update.dossier_version) == (first.version, second.version)
    assert changes(update) == [
        ('f-340', 'added'),
        ('f-311', 'removed'),
        ('c-17', 'removed'),
        ('p-4', 'removed'),
    ]
    assert update.summary == 'Legal cleared Acme pricing.'
    assert phase_of(status_call['body']) == 'awaiting-commit'
    assert artifact_call['body']['artifactUpdate']['artifact']['artifactId'] == 'dossier'

    await stream.wait_for(lambda events: len(events) >= 3)
    kinds = [event.WhichOneof('payload') for event in stream.items[1:3]]
    assert kinds == ['artifact_update', 'status_update']
    assert dossier_of(stream.items[1]) == second
    assert update_of(stream.items[2]) == update

    # GetTask reflects the update too, and the dossier was replaced, not appended.
    current = check(await tom.get(task.id))
    assert dossier_of(current) == second
    assert [a.artifact_id for a in current.artifacts] == ['dossier']

    # Commit against the version relied on: a claim and a receipt.
    done = check(await tom.commit(task, tom_commit(second.version)))
    assert (state_of(done), phase_of(done)) == ('TASK_STATE_COMPLETED', 'committed')
    receipt = receipt_of(done)
    assert receipt is not None and receipt.based_on == second.version
    [recorded] = receipt.recorded
    claim = memory.engine.fact(recorded.fact_id)
    assert claim is not None and claim.status == 'claim'
    assert claim.claimed_by is not None
    assert (claim.claimed_by.agent, claim.claimed_by.on_behalf_of) == (
        'agent:sales-assistant',
        'user:tom',
    )
    assert claim.sources == (f'commit:{receipt.commit_id}',)

    # The stream ends with the task, and the webhook hears about it.
    await asyncio.wait_for(memory.background[0], 5)
    assert state_of(stream.items[-1]) == 'TASK_STATE_COMPLETED'
    await memory.webhook.wait_for(
        lambda calls: any(state_of(c['body']) == 'TASK_STATE_COMPLETED' for c in calls)
    )


async def test_commit_right_after_an_update_push_gets_its_own_answer(memory: Memory) -> None:
    # The update's push is POSTed before its turn ends. A commit sent the
    # moment it lands must still be answered with its own result.
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    await memory.webhook.wait_for(lambda calls: len(calls) >= 3)
    legal_clears(memory.engine)
    await memory.webhook.wait_for(lambda calls: update_of(calls[-1]['body']) is not None)
    version = update_of(memory.webhook.items[-1]['body']).dossier_version  # type: ignore[union-attr]

    done = check(await tom.commit(task, tom_commit(version)))
    assert (state_of(done), phase_of(done)) == ('TASK_STATE_COMPLETED', 'committed')
    assert receipt_of(done) is not None


async def test_stale_commit_records_nothing_then_good_commit(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    first = dossier_of(task)
    assert first is not None
    legal_clears(memory.engine)
    await memory.settle()

    stale = check(await tom.commit(task, tom_commit(first.version)))
    assert (state_of(stale), phase_of(stale)) == ('TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
    error = error_of(stale)
    current = dossier_of(stale)
    assert error is not None and current is not None
    assert error.code == 'stale-dossier'
    assert error.current_version == current.version != first.version
    assert memory.engine.claims() == []

    conflict = {'id': 'f-340', 'explanation': 'The quote went out before I saw the approval.'}
    done = check(await tom.commit(task, tom_commit(current.version, conflicts=[conflict])))
    assert phase_of(done) == 'committed'
    receipt = receipt_of(done)
    assert receipt is not None and receipt.conflicts_recorded == 1
    assert len(memory.engine.claims()) == 1
    [review] = memory.engine.review_queue
    assert (review.kind, review.item_id) == ('conflict', 'f-340')


async def test_titan_question_then_answer(memory: Memory) -> None:
    maya = await memory.client(MAYA)
    task = check(await maya.negotiate(MAYA_INTENT))
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_INPUT_REQUIRED', 'question')
    question = question_of(task)
    assert question is not None and question.id == 'q-1'
    assert question.options == ['Yes', 'No', 'Partly']
    assert text_of(task) == 'Before I pull this together: is the new date funded?'
    assert dossier_of(task) is None

    wrong = check(await maya.answer(task, 'q-7', 'No.'))
    assert phase_of(wrong) == 'question'
    assert error_of(wrong) is not None and error_of(wrong).code == 'unknown-question'  # type: ignore[union-attr]
    assert question_of(wrong) == question  # still asking

    answered = check(await maya.answer(task, 'q-1', 'No.'))
    assert phase_of(answered) == 'awaiting-commit'
    dossier = dossier_of(answered)
    assert dossier is not None
    assert [f.id for f in dossier.facts] == ['f-501', 'f-502']
    assert [p.id for p in dossier.precedent] == ['p-2']
    assert [(c.id, c.basis) for c in dossier.constraints] == [
        ('c-40', ['p-2']),
        ('c-41', ['policy:leadership-update-format']),
    ]
    assert set(dossier.watching) == {'f-501', 'f-502', 'p-2', 'c-40', 'c-41'}


async def test_principal_mismatch_is_refused_without_leaking(memory: Memory) -> None:
    priya = await memory.client(PRIYA_AS_SALES_ASSISTANT)
    task = check(await priya.negotiate({**TOM_INTENT, 'entities': ['account:acme']}))
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_REJECTED', 'refused')
    error = error_of(task)
    assert error is not None and error.code == 'principal-mismatch'
    assert not task.artifacts
    seen = json.dumps(MessageToDict(check(await priya.get(task.id))))
    assert 'f-311' not in seen and 'Legal' not in seen and 'pricing' not in seen


async def test_invalid_intent_is_refused(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate({'action': 'send_quote', 'onBehalfOf': 'user:tom'})
    check(task.status)  # the history holds the invalid intent itself
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_REJECTED', 'refused')
    assert error_of(task).code == 'invalid-intent'  # type: ignore[union-attr]


async def test_unactivated_request_gets_32008_and_leaves_no_task(memory: Memory) -> None:
    for method in ('SendMessage', 'SendStreamingMessage'):
        response = await memory.rpc(method, intent_request(TOM_INTENT), activate=False)
        assert response.json()['error']['code'] == -32008
        assert 'a2a-extensions' not in response.headers
    listed = (await memory.rpc('ListTasks', {})).json()['result']
    assert listed.get('tasks', []) == []
    assert memory.engine.open_tasks() == []

    # With activation, memory echoes the extension in the response header.
    response = await memory.rpc('SendMessage', intent_request(TOM_INTENT, 'msg-2'))
    assert response.headers['a2a-extensions'] == EXTENSION_URI
    check(response.json())


async def test_unauthenticated_requests_get_401(memory: Memory) -> None:
    for token in (None, 'not-a-dev-token'):
        response = await memory.rpc('SendMessage', intent_request(TOM_INTENT), token=token)
        assert response.status_code == 401
        assert response.headers['www-authenticate'].startswith('Bearer')
    assert memory.engine.open_tasks() == []


async def test_follow_up_without_context_id(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    version = dossier_of(task).version  # type: ignore[union-attr]
    message = {
        'messageId': 'msg-commit',
        'role': 'ROLE_USER',
        'taskId': task.id,  # and no contextId: A2A says to infer it
        'parts': [{'mediaType': 'application/vnd.mem2a.commit+json', 'data': tom_commit(version)}],
        'extensions': [EXTENSION_URI],
    }
    body = (await memory.rpc('SendMessage', {'message': message})).json()
    result = check(body)['result']['task']
    assert result['contextId'] == task.context_id
    assert result['status']['state'] == 'TASK_STATE_COMPLETED'


async def test_revoked_access_shows_as_removed(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    stream = memory.collect(tom.subscribe(task.id))
    await stream.wait_for(lambda events: len(events) >= 1)

    memory.engine.set_source_readers(LEGAL_MEETING, ['group:legal'])

    await stream.wait_for(lambda events: len(events) >= 3)
    update = update_of(stream.items[2])
    assert changes(update) == [('f-311', 'removed'), ('c-17', 'removed'), ('p-4', 'removed')]
    # Same wording as for retirement: nothing says access changed, or why.
    assert (
        update.summary
        == 'Dossier changed: 1 fact removed, 1 constraint removed, 1 precedent removed.'
    )  # type: ignore[union-attr]
    dossier = dossier_of(stream.items[1])
    assert dossier is not None and 'f-311' not in json.dumps(dossier.dump())


async def test_priya_hears_about_toms_claim(memory: Memory) -> None:
    legal_clears(memory.engine)
    tom, priya = await memory.client(TOM), await memory.client(PRIYA)
    tom_task = await tom.negotiate(TOM_INTENT)
    priya_task = check(await priya.negotiate(PRIYA_INTENT))
    before = dossier_of(priya_task)
    assert before is not None and before.constraints == []  # c-17 only binds quotes
    stream = memory.collect(priya.subscribe(priya_task.id))
    await stream.wait_for(lambda events: len(events) >= 1)

    done = await tom.commit(tom_task, tom_commit(dossier_of(tom_task).version))  # type: ignore[union-attr]
    [recorded] = receipt_of(done).recorded  # type: ignore[union-attr]

    await stream.wait_for(lambda events: len(events) >= 3)
    update = update_of(stream.items[2])
    assert changes(update) == [(recorded.fact_id, 'added')]
    assert update.summary == 'user:tom reported: Tom sent Acme a renewal quote at $1.2M a year.'  # type: ignore[union-attr]
    [claim] = [f for f in dossier_of(stream.items[1]).facts if f.id == recorded.fact_id]  # type: ignore[union-attr]
    assert claim.status == 'claim' and claim.claimed_by.on_behalf_of == 'user:tom'  # type: ignore[union-attr]
    assert claim.visibility == ['user:tom', 'group:sales']

    # The CRM confirms the claim: another update, and it is no longer a claim.
    memory.engine.confirm_fact(recorded.fact_id, by='system:crm')
    await stream.wait_for(lambda events: len(events) >= 5)
    assert changes(update_of(stream.items[4])) == [(recorded.fact_id, 'updated')]
    [fact] = [f for f in dossier_of(stream.items[3]).facts if f.id == recorded.fact_id]  # type: ignore[union-attr]
    assert (fact.status, fact.confirmed_by, fact.claimed_by) == ('confirmed', 'system:crm', None)


async def test_cancel_ends_the_watch(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    await memory.webhook.wait_for(lambda calls: len(calls) >= 3)

    canceled = check(await tom.cancel(task.id))
    assert (state_of(canceled), phase_of(canceled)) == ('TASK_STATE_CANCELED', 'canceled')
    await memory.webhook.wait_for(lambda calls: len(calls) >= 4)
    assert phase_of(memory.webhook.items[3]['body']) == 'canceled'

    legal_clears(memory.engine)
    await memory.settle()
    assert len(memory.webhook.items) == 4  # no updates after cancel
    with pytest.raises(UnsupportedOperationError):
        await tom.commit(task, tom_commit(dossier_of(task).version))  # type: ignore[union-attr]
    assert memory.engine.claims() == []


async def test_watch_expires(make_memory: MakeMemory) -> None:
    now = [datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)]
    engine = MemoryEngine(watch_timeout=timedelta(minutes=5), clock=lambda: now[0])
    seed_acme(engine)
    memory = await make_memory(engine, sweep_interval=None)
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    assert dossier_of(task).expires_at == now[0] + timedelta(minutes=5)  # type: ignore[union-attr]

    assert await memory.server.sweep() == []
    now[0] += timedelta(minutes=6)
    assert await memory.server.sweep() == [task.id]

    expired = await tom.get(task.id)
    assert (state_of(expired), phase_of(expired)) == ('TASK_STATE_CANCELED', 'expired')
    assert error_of(expired).code == 'watch-expired'  # type: ignore[union-attr]
    await memory.webhook.wait_for(
        lambda calls: any(phase_of(parse_push(c['body'])) == 'expired' for c in calls)
    )
