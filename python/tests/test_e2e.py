# SPDX-License-Identifier: Apache-2.0
"""End to end, over real HTTP: Mem2AClient and raw JSON-RPC against the server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from a2a.types import StreamResponse
from a2a.utils.errors import A2AError, InvalidParamsError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict, ParseDict

from mem2a import (
    EXTENSION_URI,
    IN_REPLY_TO_KEY,
    PHASE_KEY,
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
from mem2a.constants import ANSWER, COMMIT, INTENT
from mem2a.seeds import seed_acme

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
    TOM_WITHOUT_GROUPS,
    legal_clears,
    seeded_engine,
    tom_commit,
)
from wire import check


def changes(update: Any) -> list[tuple[str, str]]:
    assert update is not None
    return [(c.id, c.change) for c in update.changes]


def message(
    media_type: str,
    data: Any,
    message_id: str,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        'messageId': message_id,
        'role': 'ROLE_USER',
        'parts': [{'mediaType': media_type, 'data': data}],
        'extensions': [EXTENSION_URI],
    }
    if task is not None:
        body |= {'taskId': task['id'], 'contextId': task['contextId']}
    return body


def result_task(response: Any) -> dict[str, Any]:
    """The task in a JSON-RPC response (an httpx response or its body)."""
    body = response if isinstance(response, dict) else response.json()
    assert 'result' in body, body
    task = body['result'].get('task', body['result'])
    return task  # type: ignore[no-any-return]


def metadata(task: dict[str, Any]) -> dict[str, Any]:
    return task['status']['message'].get('metadata', {})  # type: ignore[no-any-return]


# ---------------------------------------------------------------- discovery
async def test_agent_card_declares_mem2a(memory: Memory) -> None:
    card = (await memory.http.get(f'{memory.url}/.well-known/agent-card.json')).json()
    [entry] = [e for e in card['capabilities']['extensions'] if e['uri'] == EXTENSION_URI]
    assert entry['required'] is True
    validation.validate('extension-params', entry['params'])
    assert entry['params']['listen'] == ['push', 'subscribe']
    assert entry['params']['watchTimeout'] == 'P7D'
    assert card['capabilities']['streaming'] is True
    assert card['capabilities']['pushNotifications'] is True
    assert card['defaultInputModes'] == [INTENT, ANSWER, COMMIT, 'text/plain']
    assert 'application/vnd.mem2a.update+json' in card['defaultOutputModes']
    assert [s['id'] for s in card['skills']] == ['negotiate', 'commit']
    scheme = card['securitySchemes']['dev-token']['httpAuthSecurityScheme']
    assert scheme['bearerFormat'] == 'dev:<agent>:<principal>[:<groups>]'
    assert card['securityRequirements'] == [{'schemes': {'dev-token': {}}}]


# -------------------------------------------------------------- the stories
async def test_acme_story_with_push_and_subscribe(memory: Memory) -> None:
    tom = await memory.client(TOM)
    push = PushTarget(memory.webhook_url, token='tok-tom', bearer='secret-for-this-task')

    # Negotiate: the dossier comes back in the blocking response (artifact first).
    task = check(await tom.negotiate(TOM_INTENT, push=push))
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
    first = dossier_of(task)
    assert first is not None and first.version == '12'
    assert text_of(task) == first.summary
    assert [c.id for c in first.constraints] == ['c-17']
    assert first.watching == ['f-311', 'f-208', 'p-4', 'c-17']

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
        assert call['headers']['content-type'] == 'application/a2a+json'
        assert call['headers']['authorization'] == 'Bearer secret-for-this-task'
        assert call['headers']['x-a2a-notification-token'] == 'tok-tom'
    assert artifact_call['body']['artifactUpdate']['append'] is False
    second = dossier_of(artifact_call['body'])
    update = update_of(status_call['body'])
    assert second is not None and update is not None and second.version == '13'
    assert (update.previous_version, update.dossier_version) == ('12', '13')
    assert changes(update) == [
        ('f-340', 'added'),
        ('f-311', 'removed'),
        ('c-17', 'removed'),
        ('p-4', 'removed'),
    ]
    assert update.summary == 'Legal cleared Acme pricing.'
    assert phase_of(status_call['body']) == 'awaiting-commit'
    assert (
        IN_REPLY_TO_KEY not in status_call['body']['statusUpdate']['status']['message']['metadata']
    )

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
    assert receipt is not None and receipt.based_on == '13' and receipt.conflicts == []
    [recorded] = receipt.recorded
    claim = memory.engine.fact(recorded.fact_id)
    assert claim is not None and claim.status == 'claim' and claim.origin is not None
    assert (claim.origin.agent, claim.origin.principal) == ('agent:sales-assistant', 'user:tom')
    assert claim.sources == (f'commit:{receipt.commit_id}',)

    # The stream ends with the task, and the webhook hears about it.
    await asyncio.wait_for(memory.background[0], 5)
    assert state_of(stream.items[-1]) == 'TASK_STATE_COMPLETED'
    await memory.webhook.wait_for(
        lambda calls: any(state_of(c['body']) == 'TASK_STATE_COMPLETED' for c in calls)
    )


async def test_replies_name_the_message_they_answer(memory: Memory) -> None:
    response = await memory.rpc('SendMessage', {'message': message(INTENT, TOM_INTENT, 'msg-1')})
    task = result_task(check(response.json()))
    assert response.headers['a2a-extensions'] == EXTENSION_URI
    assert metadata(task)[IN_REPLY_TO_KEY] == 'msg-1'

    legal_clears(memory.engine)  # an update: no inReplyTo
    await memory.settle()
    current = result_task(await memory.rpc('GetTask', {'id': task['id']}))
    assert metadata(current)[PHASE_KEY] == 'awaiting-commit'
    assert IN_REPLY_TO_KEY not in metadata(current)

    commit = message(COMMIT, tom_commit('13'), 'msg-2', task)
    done = result_task(await memory.rpc('SendMessage', {'message': commit}))
    assert metadata(done) == {PHASE_KEY: 'committed', IN_REPLY_TO_KEY: 'msg-2'}


async def test_stale_commit_records_nothing_then_good_commit(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    legal_clears(memory.engine)
    await memory.settle()

    stale = check(await tom.commit(task, tom_commit('12')))
    assert (state_of(stale), phase_of(stale)) == ('TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
    error, current = error_of(stale), dossier_of(stale)
    assert error is not None and current is not None
    assert (error.code, error.current_version, current.version) == ('stale-dossier', '13', '13')
    assert changes(update_of(stale)) == [  # example 04: what changed since 12
        ('f-340', 'added'),
        ('f-311', 'removed'),
        ('c-17', 'removed'),
        ('p-4', 'removed'),
    ]
    assert memory.engine.claims() == []

    conflict = {'id': 'f-340', 'explanation': 'The quote went out before I saw the approval.'}
    done = check(await tom.commit(task, tom_commit(current.version, conflicts=[conflict])))
    assert phase_of(done) == 'committed'
    receipt = receipt_of(done)
    assert receipt is not None and receipt.conflicts == ['f-340']
    assert len(memory.engine.claims()) == 1
    [review] = memory.engine.review_queue
    assert (review.kind, review.item_id) == ('conflict', 'f-340')


async def test_commit_right_after_an_update_push_gets_its_own_answer(memory: Memory) -> None:
    # The update's push goes out before its turn ends. A commit sent the
    # moment it lands must still be answered with its own result.
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    await memory.webhook.wait_for(lambda calls: len(calls) >= 3)
    legal_clears(memory.engine)
    await memory.webhook.wait_for(lambda calls: update_of(calls[-1]['body']) is not None)
    version = update_of(memory.webhook.items[-1]['body']).dossier_version  # type: ignore[union-attr]

    done = check(await tom.commit(task, tom_commit(version)))
    assert (state_of(done), phase_of(done)) == ('TASK_STATE_COMPLETED', 'committed')


async def test_retries_return_the_first_result(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    commit = tom_commit(dossier_of(task).version)  # type: ignore[union-attr]
    first = check(await tom.commit(task, commit, message_id='msg-commit-1'))
    again = check(await tom.commit(task, commit, message_id='msg-commit-1'))
    assert state_of(again) == 'TASK_STATE_COMPLETED'
    assert again.status == first.status and receipt_of(again) == receipt_of(first)
    assert len(memory.engine.claims()) == 1  # recorded once

    # A retry that names only the taskId gets the same; a new message is refused.
    retry = message(COMMIT, commit, 'msg-commit-1') | {'taskId': task.id}
    replayed = result_task(await memory.rpc('SendMessage', {'message': retry}))
    assert replayed['status'] == MessageToDict(first.status)
    with pytest.raises(UnsupportedOperationError):
        await tom.commit(task, commit)
    assert len(memory.engine.claims()) == 1


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
    assert error_of(wrong).code == 'unknown-question'  # type: ignore[union-attr]
    assert question_of(wrong) == question  # asked again

    raw = message(ANSWER, {'questionId': 'q-1', 'text': 'No.'}, 'msg-maya-002', MessageToDict(task))
    answered = result_task(check(await memory.rpc('SendMessage', {'message': raw}, token=MAYA)))
    assert metadata(answered) == {PHASE_KEY: 'awaiting-commit', IN_REPLY_TO_KEY: 'msg-maya-002'}
    dossier = (await maya.get(task.id)).artifacts[0]
    assert dossier.artifact_id == 'dossier'
    titan = dossier_of(await maya.get(task.id))
    assert titan is not None
    assert [f.id for f in titan.facts] == ['f-501', 'f-502']
    assert [p.id for p in titan.precedent] == ['p-2']
    assert [(c.id, c.basis) for c in titan.constraints] == [
        ('c-40', ['p-2']),
        ('c-41', ['policy:leadership-update-format']),
    ]
    # A retried answer gets the task back, not an "unexpected message".
    again = result_task(await memory.rpc('SendMessage', {'message': raw}, token=MAYA))
    assert again['status'] == answered['status']


async def test_unexpected_messages_get_an_error(memory: Memory) -> None:
    maya = await memory.client(MAYA)
    task = MessageToDict(await maya.negotiate(MAYA_INTENT))
    cases = [
        message(COMMIT, tom_commit('1'), 'msg-a', task),  # a commit while asking
        message(INTENT, MAYA_INTENT, 'msg-b', task),  # an intent on an existing task
        {**message(INTENT, MAYA_INTENT, 'msg-c', task), 'parts': [{'text': 'Hi!'}]},  # no payload
    ]
    for case in cases:
        reply = result_task(await memory.rpc('SendMessage', {'message': case}, token=MAYA))
        check(reply, agent_messages=False)
        assert (reply['status']['state'], metadata(reply)[PHASE_KEY]) == (
            'TASK_STATE_INPUT_REQUIRED',
            'question',
        )
        kinds = [p.get('mediaType') for p in reply['status']['message']['parts']]
        assert kinds == [
            None,
            'application/vnd.mem2a.question+json',
            'application/vnd.mem2a.error+json',
        ]
        error = reply['status']['message']['parts'][2]['data']
        assert error['code'] == 'unexpected-message'

    # A new message with two payloads is refused outright.
    two = message(INTENT, TOM_INTENT, 'msg-d')
    two['parts'] *= 2
    refused = result_task(await memory.rpc('SendMessage', {'message': two}))
    check(refused, agent_messages=False)
    assert (refused['status']['state'], metadata(refused)[PHASE_KEY]) == (
        'TASK_STATE_REJECTED',
        'refused',
    )
    assert refused['status']['message']['parts'][1]['data']['code'] == 'unexpected-message'


async def test_errors_fail_the_task_without_leaking(memory: Memory, monkeypatch: Any) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)

    def boom(*args: Any) -> Any:
        raise RuntimeError('secret detail 42')

    monkeypatch.setattr(memory.engine, 'respond', boom)
    failed = check(await tom.commit(task, tom_commit('12')))
    assert (state_of(failed), phase_of(failed)) == ('TASK_STATE_FAILED', 'failed')
    error = error_of(failed)
    assert error is not None and (error.code, error.message) == ('internal', 'Internal error.')
    assert 'secret' not in json.dumps(MessageToDict(failed))
    assert memory.engine.open_tasks() == []

    # Outside a turn too: a generic JSON-RPC error, never the exception's text.
    monkeypatch.setattr(memory.engine, 'visible_dossier', boom)
    body = (await memory.rpc('GetTask', {'id': task.id})).json()
    assert body['error']['code'] == -32603 and body['error']['message'] == 'Internal error.'


async def test_refusals(memory: Memory) -> None:
    priya = await memory.client(PRIYA_AS_SALES_ASSISTANT)
    task = check(await priya.negotiate({**TOM_INTENT, 'entities': ['account:acme']}))
    assert (state_of(task), phase_of(task)) == ('TASK_STATE_REJECTED', 'refused')
    assert error_of(task).code == 'principal-mismatch'  # type: ignore[union-attr]
    assert not task.artifacts
    seen = json.dumps(MessageToDict(check(await priya.get(task.id))))
    assert 'f-311' not in seen and 'Legal' not in seen and 'pricing' not in seen

    tom = await memory.client(TOM)
    invalid = await tom.negotiate({'action': 'send_quote', 'onBehalfOf': 'user:tom'})
    check(invalid.status)  # the history holds the invalid intent itself
    assert (phase_of(invalid), error_of(invalid).code) == ('refused', 'invalid-intent')  # type: ignore[union-attr]


async def test_open_task_limit(make_memory: MakeMemory) -> None:
    memory = await make_memory(seeded_engine(max_open_tasks=2))
    tom = await memory.client(TOM)
    first = await tom.negotiate(TOM_INTENT)
    await tom.negotiate(TOM_INTENT)
    third = check(await tom.negotiate(TOM_INTENT))
    assert (phase_of(third), error_of(third).code) == ('refused', 'limit-exceeded')  # type: ignore[union-attr]
    await tom.cancel(first.id)
    assert phase_of(await tom.negotiate(TOM_INTENT)) == 'awaiting-commit'


# ---------------------------------------------------------- access and reads
async def test_unactivated_request_gets_32008_and_leaves_no_task(memory: Memory) -> None:
    for method in ('SendMessage', 'SendStreamingMessage'):
        response = await memory.rpc(
            method, {'message': message(INTENT, TOM_INTENT, 'm')}, activate=False
        )
        assert response.json()['error']['code'] == -32008
        assert 'a2a-extensions' not in response.headers
    listed = (await memory.rpc('ListTasks', {})).json()['result']
    assert listed.get('tasks', []) == []
    assert memory.engine.open_tasks() == []


async def test_unauthenticated_requests_get_401(memory: Memory) -> None:
    for token in (None, 'not-a-dev-token', 'dev:sales-assistant:tom'):
        response = await memory.rpc(
            'SendMessage', {'message': message(INTENT, TOM_INTENT, 'm')}, token=token
        )
        assert response.status_code == 401
        assert response.headers['www-authenticate'].startswith('Bearer')
    assert 'dev:sales-assistant:user:tom:group:sales' in response.json()['error']['message']
    assert memory.engine.open_tasks() == []


async def test_follow_up_without_context_id(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    commit = message(COMMIT, tom_commit(dossier_of(task).version), 'msg-c')  # type: ignore[union-attr]
    commit['taskId'] = task.id  # and no contextId: A2A says to infer it
    result = result_task(check((await memory.rpc('SendMessage', {'message': commit})).json()))
    assert result['contextId'] == task.context_id
    assert result['status']['state'] == 'TASK_STATE_COMPLETED'


Request = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]
BINDING: dict[str, Request] = {
    'GetTask': lambda task: ('GetTask', {'id': task['id']}),
    'CancelTask': lambda task: ('CancelTask', {'id': task['id']}),
    'SubscribeToTask': lambda task: ('SubscribeToTask', {'id': task['id']}),
    'SendMessage': lambda task: (
        'SendMessage',
        {'message': message(COMMIT, tom_commit('12'), 'msg-x', task)},
    ),
    'SendStreamingMessage': lambda task: (
        'SendStreamingMessage',
        {'message': message(COMMIT, tom_commit('12'), 'msg-y', task)},
    ),
    'CreateTaskPushNotificationConfig': lambda task: (
        'CreateTaskPushNotificationConfig',
        {'taskId': task['id'], 'url': 'http://127.0.0.1:9/hook'},
    ),
    'GetTaskPushNotificationConfig': lambda task: (
        'GetTaskPushNotificationConfig',
        {'taskId': task['id'], 'id': 'push-1'},
    ),
    'ListTaskPushNotificationConfigs': lambda task: (
        'ListTaskPushNotificationConfigs',
        {'taskId': task['id']},
    ),
    'DeleteTaskPushNotificationConfig': lambda task: (
        'DeleteTaskPushNotificationConfig',
        {'taskId': task['id'], 'id': 'push-1'},
    ),
}


@pytest.mark.parametrize('operation', sorted(BINDING))
async def test_only_the_task_owner_can_touch_it(memory: Memory, operation: str) -> None:
    tom = await memory.client(TOM)
    task = MessageToDict(await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url)))
    method, params = BINDING[operation](task)
    for someone_else in (PRIYA, 'dev:inbox-agent:user:tom:group:sales'):  # principal, agent
        if method in ('SubscribeToTask', 'SendStreamingMessage'):
            [body] = await memory.sse(method, params, token=someone_else)
        else:
            body = (await memory.rpc(method, params, token=someone_else)).json()
        assert body['error']['code'] == -32001, body
    # Tom's task is untouched.
    mine = result_task(await memory.rpc('GetTask', {'id': task['id']}))
    assert (mine['status']['state'], dossier_of(mine).version) == (
        'TASK_STATE_INPUT_REQUIRED',
        '12',
    )  # type: ignore[union-attr]
    configs = (await memory.rpc('ListTaskPushNotificationConfigs', {'taskId': task['id']})).json()
    assert [c['id'] for c in configs['result']['configs']] == ['push-1']


async def test_other_principals_do_not_see_the_task_listed(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    mine = (await memory.rpc('ListTasks', {})).json()['result']['tasks']
    theirs = (await memory.rpc('ListTasks', {}, token=PRIYA)).json()['result'].get('tasks', [])
    assert [t['id'] for t in mine] == [task.id] and theirs == []


async def test_reads_use_current_access(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    full = dossier_of(task)
    assert full is not None and full.watching == ['f-311', 'f-208', 'p-4', 'c-17']

    # Tom's credentials no longer carry group:sales, which f-311's source needs.
    narrower = await memory.client(TOM_WITHOUT_GROUPS)
    shown = dossier_of(check(await narrower.get(task.id)))
    # A redacted view gets its own version, so a version always names one content.
    assert shown is not None and shown.version.startswith(f'{full.version}-redacted-')
    assert dossier_of(check(await narrower.get(task.id))).version == shown.version  # type: ignore[union-attr]
    assert (shown.watching, shown.precedent, shown.constraints) == (['f-208'], [], [])
    params = {'includeArtifacts': True}
    [listed] = (await memory.rpc('ListTasks', params, token=TOM_WITHOUT_GROUPS)).json()['result'][
        'tasks'
    ]
    assert dossier_of(listed).watching == ['f-208']  # type: ignore[union-attr]
    [snapshot] = await memory.sse('SubscribeToTask', {'id': task.id}, token=TOM_WITHOUT_GROUPS)
    assert dossier_of(snapshot['result']).watching == ['f-208']  # type: ignore[union-attr]
    assert dossier_of(await tom.get(task.id)) == full  # the original credentials see it all

    # A finished task's stored dossier is filtered too.
    await tom.commit(task, tom_commit(full.version))
    memory.engine.set_source_readers(LEGAL_MEETING, ['group:legal'])
    done = check(await tom.get(task.id))
    assert state_of(done) == 'TASK_STATE_COMPLETED'
    assert dossier_of(done).watching == ['f-208']  # type: ignore[union-attr]


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
    assert update.summary == (  # type: ignore[union-attr]
        'Dossier changed: 1 fact removed, 1 constraint removed, 1 precedent removed.'
    )
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
    assert update.summary == (  # type: ignore[union-attr]
        'Reported by user:tom, not yet confirmed: Tom sent Acme a renewal quote at $1.2M a year.'
    )
    [claim] = [f for f in dossier_of(stream.items[1]).facts if f.id == recorded.fact_id]  # type: ignore[union-attr]
    assert claim.status == 'claim' and claim.claimed_by.on_behalf_of == 'user:tom'  # type: ignore[union-attr]
    assert claim.visibility is None and claim.evidence is not None

    # The CRM confirms the claim: another update, and it is no longer a claim.
    memory.engine.confirm_fact(recorded.fact_id, by='system:crm')
    await stream.wait_for(lambda events: len(events) >= 5)
    assert changes(update_of(stream.items[4])) == [(recorded.fact_id, 'updated')]
    [fact] = [f for f in dossier_of(stream.items[3]).facts if f.id == recorded.fact_id]  # type: ignore[union-attr]
    assert (fact.status, fact.confirmed_by, fact.claimed_by) == ('confirmed', 'system:crm', None)


# ---------------------------------------------------------------- listening
async def test_watch_follows_the_dossier_and_resubscribes(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    original = tom.subscribe
    streams = 0

    async def drops_after_the_snapshot(task_id: str) -> AsyncGenerator[StreamResponse, None]:
        nonlocal streams
        streams += 1
        async for event in original(task_id):
            yield event
            if streams == 1:
                return  # the first stream closes early, as a flaky network might

    tom.subscribe = drops_after_the_snapshot  # type: ignore[method-assign]
    seen: list[tuple[str, Any]] = []

    async def follow() -> None:
        async for dossier, update in tom.watch(task.id):
            seen.append((dossier.version, update))

    watcher = asyncio.create_task(follow())
    while streams < 2:
        await asyncio.sleep(0.01)
    legal_clears(memory.engine)
    while len(seen) < 2:
        await asyncio.sleep(0.01)
    assert [version for version, _ in seen] == ['12', '13']

    await tom.cancel(task.id)  # the task ends, and so does the watch
    await asyncio.wait_for(watcher, 5)


async def test_watch_takes_the_working_phase_in_stride(memory: Memory) -> None:
    """A memory may say `working` (SUBMITTED or WORKING) before it has a
    dossier. The reference never does, but clients must cope."""
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)
    ids = {'taskId': task.id, 'contextId': task.context_id}
    real = MessageToDict(task)
    working = {
        'state': 'TASK_STATE_WORKING',
        'message': {
            'messageId': 'msg-working',
            'role': 'ROLE_AGENT',
            'parts': [{'text': 'Looking into it.'}],
            'metadata': {PHASE_KEY: 'working'},
            'extensions': [EXTENSION_URI],
        },
    }
    snapshot = ParseDict(
        {'task': {'id': task.id, 'contextId': task.context_id, 'status': working}},
        StreamResponse(),
    )
    still_working = ParseDict({'statusUpdate': {**ids, 'status': working}}, StreamResponse())
    assert (phase_of(still_working), dossier_of(snapshot)) == ('working', None)
    original = tom.subscribe

    async def working_first(task_id: str) -> AsyncGenerator[StreamResponse, None]:
        yield snapshot
        yield still_working
        artifact = {'artifactUpdate': {**ids, 'artifact': real['artifacts'][0]}}
        yield ParseDict(artifact, StreamResponse())
        yield ParseDict({'statusUpdate': {**ids, 'status': real['status']}}, StreamResponse())
        async for event in original(task_id):
            if event.WhichOneof('payload') != 'task':
                yield event

    tom.subscribe = working_first  # type: ignore[method-assign]
    async with aclosing(tom.watch(task.id)) as dossiers:
        dossier, update = await asyncio.wait_for(anext(dossiers), 5)
        assert (dossier.version, update) == ('12', None)
        legal_clears(memory.engine)
        dossier, update = await asyncio.wait_for(anext(dossiers), 5)
        assert dossier.version == '13' and update is not None
        assert update.previous_version == '12'


async def test_push_runs_in_the_background_with_retries(memory: Memory) -> None:
    hook = memory.webhook
    hook.gate = asyncio.Event()  # the webhook hangs until we open it
    hook.statuses = [503, 503]  # then fails twice

    tom = await memory.client(TOM)
    task = await asyncio.wait_for(tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url)), 5)
    assert phase_of(task) == 'awaiting-commit'  # answered while the webhook hangs
    assert hook.items == []
    hook.gate.set()
    await hook.wait_for(lambda calls: len(calls) >= 3)
    kinds = [next(iter(call['body'])) for call in hook.items]
    assert kinds == ['task', 'artifactUpdate', 'statusUpdate']  # in order
    assert hook.attempts == 5  # two failures, then three deliveries


async def test_push_does_not_follow_redirects(memory: Memory) -> None:
    memory.webhook.statuses = [307]  # the first push is redirected to /elsewhere
    tom = await memory.client(TOM)
    await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    await memory.settle()
    paths = [call['path'] for call in memory.webhook.items]
    assert paths == ['/hook', '/hook']  # the redirected event is dropped, not followed
    assert next(iter(memory.webhook.items[0]['body'])) == 'artifactUpdate'


async def test_push_configs_are_screened_capped_and_quiet(make_memory: MakeMemory) -> None:
    memory = await make_memory(max_push_configs=3)
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url, bearer='s1'))
    created = (
        await memory.rpc(
            'CreateTaskPushNotificationConfig',
            {
                'taskId': task.id,
                'url': memory.webhook_url,
                'token': 't2',
                'authentication': {'scheme': 'Bearer', 'credentials': 's2'},
            },
        )
    ).json()['result']
    assert created == {'id': 'push-2', 'taskId': task.id, 'url': memory.webhook_url}  # example 10
    await tom.add_push(task.id, PushTarget(memory.webhook_url))
    with pytest.raises(InvalidParamsError, match='limit-exceeded'):
        await tom.add_push(task.id, PushTarget(memory.webhook_url))
    configs = (await memory.rpc('ListTaskPushNotificationConfigs', {'taskId': task.id})).json()
    assert [c['id'] for c in configs['result']['configs']] == ['push-1', 'push-2', 'push-3']
    assert 'authentication' not in json.dumps(configs) and 't2' not in json.dumps(configs)


async def test_push_origins(make_memory: MakeMemory) -> None:
    allowlisted = await make_memory(push_origins=['https://agents.example.com'])
    tom = await allowlisted.client(TOM)
    with pytest.raises(InvalidParamsError):
        await tom.negotiate(TOM_INTENT, push=PushTarget(allowlisted.webhook_url))
    assert allowlisted.engine.open_tasks() == []

    screened = await make_memory(push_origins=None)  # the default SSRF screen
    tom = await screened.client(TOM)
    with pytest.raises(A2AError):
        await tom.negotiate(TOM_INTENT, push=PushTarget(screened.webhook_url))


async def test_cancel_ends_the_watch(memory: Memory) -> None:
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT, push=PushTarget(memory.webhook_url))
    await memory.webhook.wait_for(lambda calls: len(calls) >= 3)

    canceled = check(await tom.cancel(task.id))
    assert (state_of(canceled), phase_of(canceled)) == ('TASK_STATE_CANCELED', 'canceled')
    assert (
        text_of(canceled)
        == 'Canceled. Memory stopped watching this task and recorded nothing from it.'
    )
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
    card = (await memory.http.get(f'{memory.url}/.well-known/agent-card.json')).json()
    assert card['capabilities']['extensions'][0]['params']['watchTimeout'] == 'PT5M'
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
        lambda calls: any(phase_of(c['body']) == 'expired' for c in calls)
    )


# -------------------------------------------------------------- dev admin
async def test_dev_admin_routes(make_memory: MakeMemory) -> None:
    memory = await make_memory(dev_admin=True)
    tom = await memory.client(TOM)
    task = await tom.negotiate(TOM_INTENT)

    async def post(path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        response = await memory.http.post(f'{memory.url}/dev{path}', json=body or {})
        return response.status_code, response.json()

    state = (await memory.http.get(f'{memory.url}/dev/state')).json()
    assert {f['id']: f['version'] for f in state['facts']}['f-311'] == '3'
    assert [(t['id'], t['phase'], t['dossierVersion']) for t in state['tasks']] == [
        (task.id, 'awaiting-commit', '12')
    ]

    assert await post('/scenarios/acme/legal-clears') == (
        200,
        {'ok': True, 'updatedTasks': [task.id]},
    )
    assert (await post('/scenarios/acme/legal-clears'))[0] == 409
    fact = {
        'id': 'f-400',
        'statement': 'Acme asked for a two-year term.',
        'source': 'email:acme-terms',
        'confirmedBy': 'user:tom',
        'entities': ['account:acme'],
        'note': 'Acme wants two years.',
    }
    assert await post('/facts', fact) == (200, {'ok': True, 'updatedTasks': [task.id]})
    assert update_of(await tom.get(task.id)).summary == 'Acme wants two years.'  # type: ignore[union-attr]
    assert (await post('/facts', fact))[0] == 409
    sources = len((await memory.http.get(f'{memory.url}/dev/state')).json()['sources'])
    for body, status in [
        ({**fact, 'id': 'p-4', 'source': 'email:new'}, 409),  # ids are shared across kinds
        ({**fact, 'id': 'f-401', 'source': 'email:new', 'supersedes': ['f-999']}, 400),
        ({**fact, 'id': 'f-401', 'readers': ['group:sales']}, 409),  # an existing source
    ]:
        assert (await post('/facts', body))[0] == status
    state = (await memory.http.get(f'{memory.url}/dev/state')).json()
    assert len(state['sources']) == sources  # nothing was half-done
    assert await post('/facts/f-400/retire', {'note': 'Withdrawn.'}) == (
        200,
        {'ok': True, 'updatedTasks': [task.id]},
    )
    assert await post('/facts/f-208/confirm', {'by': 'system:crm'}) == (
        200,
        {'ok': True, 'updatedTasks': []},
    )
    readers = {'readers': ['group:legal']}
    path = '/sources/crm:opportunity/acme-renewal-2026/readers'
    assert await post(path, readers) == (200, {'ok': True, 'updatedTasks': [task.id]})
    assert dossier_of(await tom.get(task.id)).watching == ['f-340']  # type: ignore[union-attr]
    assert (await post('/facts/f-999/retire'))[0] == 404
    assert (await post('/facts', {'id': 'f-401'}))[0] == 400

    # The admin routes skip authentication; the A2A endpoint never does.
    assert (await memory.rpc('GetTask', {'id': task.id}, token=None)).status_code == 401
