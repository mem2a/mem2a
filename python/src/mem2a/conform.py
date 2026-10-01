# SPDX-License-Identifier: Apache-2.0
"""``mem2a-conform``: a black-box check of any Mem2A v0.1 memory.

    mem2a-conform --url URL --token TOKEN --principal P --entity E --action A
                  [--other-token TOKEN] [--allow-writes] [--dev-admin] [--json]

It speaks raw A2A JSON-RPC over HTTP (no SDK client), validates every Mem2A
payload memory sends against the packaged schemas with formats enforced, and
prints one line per check with its spec section: PASS, FAIL or SKIP. It exits
1 if any check fails.

By default it records nothing in memory: it opens tasks for `--principal`
about `--entity` and `--action`, sends commits a conforming memory must
refuse, and cancels what it opened. `--allow-writes` also commits a claim
(marked as a test) and retries it. `--dev-admin` uses a reference-style admin
API (as in ``mem2a serve --dev-admin``) to add test facts, then retire them:
it checks that a new fact reaches both a push webhook and a SubscribeToTask
stream (memory must be allowed to call this machine), and that a fact whose
source the principal can no longer read is removed in an update.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mem2a import validation
from mem2a.constants import (
    AGENT_PAYLOADS,
    ANSWER,
    COMMIT,
    EXTENSION_URI,
    IN_REPLY_TO_KEY,
    INTENT,
    MEDIA_TYPE_OF,
    MEDIA_TYPES,
    PHASE_KEY,
    PayloadKind,
)
from mem2a.server import listen_socket


Status = Literal['PASS', 'FAIL', 'SKIP']
Json = dict[str, Any]

#: Phases the spec allows for each task state (section 7.2).
PHASES = {
    'TASK_STATE_SUBMITTED': {'working'},
    'TASK_STATE_WORKING': {'working'},
    'TASK_STATE_INPUT_REQUIRED': {'question', 'awaiting-commit'},
    'TASK_STATE_AUTH_REQUIRED': {'reauth'},
    'TASK_STATE_COMPLETED': {'committed'},
    'TASK_STATE_REJECTED': {'refused'},
    'TASK_STATE_CANCELED': {'expired', 'canceled'},
    'TASK_STATE_FAILED': {'failed'},
}
OPEN_STATES = {'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', 'TASK_STATE_INPUT_REQUIRED'}
TEST_NOTE = 'mem2a-conform test data; safe to ignore.'


@dataclass(frozen=True)
class Options:
    """What to check, and as whom."""

    url: str
    token: str
    principal: str
    entity: str
    action: str
    other_token: str | None = None
    allow_writes: bool = False
    dev_admin: bool = False
    webhook_host: str = '127.0.0.1'
    timeout: float = 10.0


@dataclass(frozen=True)
class Result:
    check: str
    section: str
    status: Status
    detail: str


class _Fail(Exception):
    """A check's expectation did not hold."""


class _Skip(Exception):
    """A check that can't run with these options."""


def _expect(condition: object, message: str) -> None:
    if not condition:
        raise _Fail(message)


# ============================================================ JSON helpers
def _walk(node: Any) -> Iterator[Any]:
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        yield from _walk(child)


def _state(task: Json) -> str:
    return str(task.get('status', {}).get('state', ''))


def _message(task: Json) -> Json:
    message = task.get('status', {}).get('message', {})
    return message if isinstance(message, dict) else {}


def _phase(task: Json) -> str | None:
    phase = _message(task).get('metadata', {}).get(PHASE_KEY)
    return phase if isinstance(phase, str) else None


def _status_part(task: Json, kind: PayloadKind) -> Json | None:
    for part in _message(task).get('parts', []):
        if part.get('mediaType') == MEDIA_TYPE_OF[kind]:
            data = part.get('data')
            return data if isinstance(data, dict) else None
    return None


def _artifact_part(node: Json, kind: PayloadKind) -> Json | None:
    artifacts = node.get('artifacts') or ([node['artifact']] if 'artifact' in node else [])
    for artifact in artifacts:
        if artifact.get('artifactId') == kind:
            for part in artifact.get('parts', []):
                if part.get('mediaType') == MEDIA_TYPE_OF[kind]:
                    data = part.get('data')
                    return data if isinstance(data, dict) else None
    return None


def _code(body: Json) -> Any:
    return (body.get('error') or {}).get('code')


def _describe(body: Json) -> str:
    if 'error' in body:
        return f'error {body["error"].get("code")}: {body["error"].get("message")}'
    result = body.get('result', {})
    task = result.get('task', result)
    error = _status_part(task, 'error') if isinstance(task, dict) else None
    detail = f' ({error["code"]})' if error else ''
    return f'{_state(task) or "no task"}, phase {_phase(task)}{detail}'


def _update_after(events: Sequence[Json], fact_id: str) -> tuple[Json, Json, Json] | None:
    """(dossier, status message, update) for the first dossier naming
    `fact_id` and the status update right after it, in stream-event order."""
    for i, event in enumerate(events):
        dossier = _artifact_part(event.get('artifactUpdate', {}), 'dossier')
        if dossier is None or fact_id not in dossier.get('watching', []):
            continue
        for later in events[i + 1 :]:
            status = later.get('statusUpdate')
            if status is not None:
                task = {'status': status.get('status', {})}
                update = _status_part(task, 'update')
                return (dossier, _message(task), update) if update else None
    return None


# ================================================================ checker
class Checker:
    """Runs the checks against one memory; see `check_memory`."""

    def __init__(self, options: Options, http: httpx.AsyncClient) -> None:
        self.options = options
        self.http = http
        self.results: list[Result] = []
        self.rpc_url: str | None = None
        self.params: Json = {}
        self.task: Json | None = None  # the main task, while it awaits a commit
        self.payloads = 0
        self.problems: list[str] = []
        self._opened: set[str] = set()
        self._ids = itertools.count(1)

    async def run(self) -> list[Result]:
        checks: list[tuple[str, str, Callable[[], Awaitable[str]]]] = [
            ('card', '5', self.card),
            ('activation', '6.3', self.activation),
            ('refusals', '8.1.3', self.refusals),
            ('negotiate', '8.1', self.negotiate),
            ('dossier rules', '8.1.5, 9.4', self.dossier_rules),
            ('stale commit', '8.4.3', self.stale_commit),
            ('invalid commit', '8.4.2', self.invalid_commit),
            ('unexpected message', '7.5', self.unexpected_message),
            ('binding', '10.7', self.binding),
            ('reads', '8.3.3', self.reads),
            ('cancel', '8.5', self.cancel),
            ('commit and retry', '8.4.5, 8.4.8', self.commit_and_retry),
            ('listen', '8.3.2', self.listen),
            ('access', '8.3.4, 10.3', self.access),
        ]
        try:
            for name, section, check in checks:
                self.results.append(await self._run(name, section, check))
        finally:
            await self._cleanup()
        self.results.append(self._payloads())
        return self.results

    def _payloads(self) -> Result:
        """Everything `inspect` found along the way."""
        problems = sorted(set(self.problems))
        if problems:
            more = f' (+{len(problems) - 3} more)' if len(problems) > 3 else ''
            return Result('payloads', '7', 'FAIL', '; '.join(problems[:3]) + more)
        if not self.payloads:
            return Result('payloads', '7', 'SKIP', 'memory sent no Mem2A payloads to check')
        return Result(
            'payloads', '7', 'PASS', f'{self.payloads} Mem2A payloads valid; message rules hold'
        )

    async def _run(self, name: str, section: str, check: Callable[[], Awaitable[str]]) -> Result:
        if name != 'card' and self.rpc_url is None:
            return Result(name, section, 'SKIP', 'no usable Agent Card')
        needs_task = name in (
            'dossier rules',
            'stale commit',
            'invalid commit',
            'unexpected message',
        )
        if needs_task and self.task is None:
            return Result(name, section, 'SKIP', 'negotiate produced no dossier')
        try:
            return Result(name, section, 'PASS', await check())
        except _Skip as skip:
            return Result(name, section, 'SKIP', str(skip))
        except _Fail as failure:
            return Result(name, section, 'FAIL', str(failure))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, asyncio.TimeoutError) as error:
            return Result(name, section, 'FAIL', f'{type(error).__name__}: {error}')

    # ------------------------------------------------------------- checks
    async def card(self) -> str:
        response = await self.http.get(f'{self.options.url}/.well-known/agent-card.json')
        _expect(
            response.status_code == 200,
            f'GET /.well-known/agent-card.json: HTTP {response.status_code}',
        )
        card = response.json()
        capabilities = card.get('capabilities', {})
        entries = [e for e in capabilities.get('extensions', []) if e.get('uri') == EXTENSION_URI]
        _expect(len(entries) == 1, f'expected one {EXTENSION_URI} entry, found {len(entries)}')
        _expect(entries[0].get('required') is True, 'the Mem2A extension is not required: true')
        params = entries[0].get('params', {})
        errors = validation.errors('extension-params', params)
        _expect(not errors, f'params: {errors[:1]}')
        if 'push' in params['listen']:
            _expect(
                capabilities.get('pushNotifications') is True,
                'listen has push, but pushNotifications is not true',
            )
        if 'subscribe' in params['listen']:
            _expect(
                capabilities.get('streaming') is True,
                'listen has subscribe, but streaming is not true',
            )
        _expect(
            card.get('securitySchemes') and card.get('securityRequirements'),
            'no security scheme or requirement',
        )
        missing = [
            m for m in (INTENT, ANSWER, COMMIT) if m not in card.get('defaultInputModes', [])
        ]
        _expect(not missing, f'defaultInputModes lacks {", ".join(missing)}')
        interfaces = [
            i for i in card.get('supportedInterfaces', []) if i.get('protocolBinding') == 'JSONRPC'
        ]
        _expect(interfaces, 'no JSONRPC interface in supportedInterfaces')
        self.rpc_url, self.params = interfaces[0]['url'], params
        timeout = params.get('watchTimeout')
        return f'required; listen {"+".join(params["listen"])}' + (
            f'; watchTimeout {timeout}' if timeout else ''
        )

    async def activation(self) -> str:
        before = await self._task_ids()
        body = await self.rpc(
            'SendMessage', {'message': self._message('intent', self._intent())}, activate=False
        )
        _expect(
            _code(body) == -32008,
            f'without A2A-Extensions: expected error -32008, got {_describe(body)}',
        )
        after = await self._task_ids()
        if before is None or after is None:
            return '-32008 without the header (ListTasks unavailable, so leftovers not checked)'
        _expect(after <= before, 'the refused request left a task behind')
        return '-32008 without the header; no task left behind'

    async def refusals(self) -> str:
        invalid = {k: v for k, v in self._intent().items() if k not in ('summary', 'entities')}
        self._expect_refused(await self.send(self._message('intent', invalid)), 'invalid-intent')
        someone_else = self._intent(onBehalfOf=f'{self.options.principal}.mem2a-conform')
        self._expect_refused(
            await self.send(self._message('intent', someone_else)), 'principal-mismatch'
        )
        return 'invalid-intent and principal-mismatch, both REJECTED with phase refused'

    async def negotiate(self) -> str:
        task = await self._past_questions(await self.send(self._message('intent', self._intent())))
        if _state(task) == 'TASK_STATE_REJECTED':
            code = (_status_part(task, 'error') or {}).get('code')
            return f'refused ({code}); the checks that need a dossier are skipped'
        if _phase(task) == 'question':
            return 'still asking after three answers; the checks that need a dossier are skipped'
        self._expect_phase(task, 'TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
        dossier = _artifact_part(task, 'dossier')
        _expect(dossier is not None, 'awaiting-commit, but the task has no dossier artifact')
        assert dossier is not None
        self.task = task
        return f'dossier {dossier["version"]}, inReplyTo set; {await self._artifact_order()}'

    async def dossier_rules(self) -> str:
        dossier = self._dossier()
        items = [
            item['id'] for key in ('facts', 'precedent', 'constraints') for item in dossier[key]
        ]
        _expect(len(items) == len(set(items)), 'an item id appears twice')
        watching = dossier['watching']
        _expect(
            sorted(watching) == sorted(items),
            f'watching {watching} is not exactly the items {items}',
        )
        claims = {fact['id'] for fact in dossier['facts'] if fact['status'] == 'claim'}
        resting = [c['id'] for c in dossier['constraints'] if claims & set(c['basis'])]
        _expect(not resting, f'constraints resting on claims: {resting}')
        if 'watchTimeout' in self.params:
            _expect('expiresAt' in dossier, 'no expiresAt, though the card declares watchTimeout')
        return f'watching lists exactly its {len(items)} items; no constraint rests on a claim'

    async def stale_commit(self) -> str:
        task = await self.send(self._message('commit', self._commit('does-not-exist'), self.task))
        self._expect_phase(task, 'TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
        error = _status_part(task, 'error') or {}
        _expect(
            error.get('code') == 'stale-dossier', f'expected stale-dossier, got {error.get("code")}'
        )
        self.task = task
        current = self._dossier()['version']
        _expect(
            error.get('currentVersion') == current,
            f'currentVersion {error.get("currentVersion")} is not {current}',
        )
        return f'stale-dossier with currentVersion {current}; still awaiting-commit'

    async def invalid_commit(self) -> str:
        invalid = {
            k: v for k, v in self._commit(self._dossier()['version']).items() if k != 'claims'
        }
        task = await self.send(self._message('commit', invalid, self.task))
        self._expect_error(task, 'awaiting-commit', 'invalid-commit')
        self.task = task
        return 'invalid-commit; still awaiting-commit'

    async def unexpected_message(self) -> str:
        answer = {'questionId': 'mem2a-conform', 'text': 'Nobody asked.'}
        task = await self.send(self._message('answer', answer, self.task))
        self._expect_error(task, 'awaiting-commit', 'unexpected-message')
        self.task = task
        return 'an answer while awaiting a commit: unexpected-message; phase unchanged'

    async def binding(self) -> str:
        if not self.options.other_token:
            raise _Skip('needs --other-token (another agent or principal)')
        if self.task is None:
            raise _Skip('negotiate produced no task')
        for method in ('GetTask', 'CancelTask'):
            body = await self.rpc(method, {'id': self.task['id']}, token=self.options.other_token)
            _expect(
                _code(body) == -32001,
                f'{method} as someone else: expected -32001, got {_describe(body)}',
            )
        return "someone else's GetTask and CancelTask: TaskNotFoundError"

    async def reads(self) -> str:
        if self.task is None:
            raise _Skip('negotiate produced no task')
        task = (await self.rpc('GetTask', {'id': self.task['id']})).get('result', {})
        _expect(
            _artifact_part(task, 'dossier') == self._dossier(),
            'GetTask does not return the current dossier',
        )
        if 'subscribe' not in self.params['listen']:
            return 'GetTask returns the current dossier (subscribe not declared)'
        async with aclosing(self.stream('SubscribeToTask', {'id': self.task['id']})) as events:
            first = await asyncio.wait_for(anext(events), self.options.timeout)
        _expect(
            first.get('task', {}).get('id') == self.task['id'],
            'the first SubscribeToTask event is not the task',
        )
        return 'GetTask returns the current dossier; SubscribeToTask starts with the task'

    async def cancel(self) -> str:
        task = await self.send(self._message('intent', self._intent()))
        _expect(
            _state(task) in OPEN_STATES,
            f'expected an open task to cancel, got {_describe({"result": task})}',
        )
        result = (await self.rpc('CancelTask', {'id': task['id']})).get('result', {})
        self._expect_phase(result, 'TASK_STATE_CANCELED', 'canceled')
        return 'CANCELED, phase canceled'

    async def commit_and_retry(self) -> str:
        if not self.options.allow_writes:
            raise _Skip('needs --allow-writes (records a test claim)')
        if self.task is None:
            raise _Skip('negotiate produced no dossier')
        dossier = self._dossier()
        conflicts = (
            [{'id': dossier['watching'][0], 'explanation': TEST_NOTE}]
            if dossier['watching']
            else []
        )
        message = self._message('commit', self._commit(dossier['version'], conflicts), self.task)
        first = await self.send(message)
        self._expect_phase(first, 'TASK_STATE_COMPLETED', 'committed')
        self.task = None
        receipt = _artifact_part(first, 'receipt')
        _expect(receipt is not None, 'no receipt artifact')
        assert receipt is not None
        _expect(receipt['basedOn'] == dossier['version'], f'receipt basedOn {receipt["basedOn"]}')
        _expect(
            receipt['conflicts'] == [c['id'] for c in conflicts],
            f'receipt conflicts {receipt["conflicts"]}',
        )
        _expect(
            [r['status'] for r in receipt['recorded']] == ['claim'],
            'the claim was not recorded as a claim',
        )
        again = await self.send(message)  # the same messageId: a retry
        _expect(
            again.get('status') == first.get('status')
            and _artifact_part(again, 'receipt') == receipt,
            f'the retried commit got a different result: {_describe({"result": again})}',
        )
        listed = receipt['conflicts']
        return f'receipt {receipt["commitId"]} lists conflicts {listed}; a retry gets the same'

    async def listen(self) -> str:
        if not self.options.dev_admin:
            raise _Skip('needs --dev-admin (a reference-style admin API)')
        secret = uuid.uuid4().hex
        async with _Webhook(self.options.webhook_host) as hook:
            config = {
                'url': hook.url,
                'token': 'mem2a-conform',
                'authentication': {'scheme': 'Bearer', 'credentials': secret},
            }
            first = await self.send(
                self._message('intent', self._intent()), {'taskPushNotificationConfig': config}
            )
            task = await self._past_questions(first)
            self._expect_phase(task, 'TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
            old = (_artifact_part(task, 'dossier') or {}).get('version')
            stream = _Events()
            reader = asyncio.create_task(
                stream.read(self.stream('SubscribeToTask', {'id': task['id']}))
            )
            fact_id = f'conform-{uuid.uuid4().hex[:8]}'
            added = False
            try:
                await stream.wait(lambda events: bool(events), self.options.timeout)
                updated = await self._admin(
                    '/dev/facts',
                    {
                        'id': fact_id,
                        'statement': TEST_NOTE,
                        'source': 'other:mem2a-conform',
                        'confirmedBy': 'system:mem2a-conform',
                        'entities': [self.options.entity],
                    },
                )
                added = True
                _expect(task['id'] in updated, f'updatedTasks {updated} does not list the task')
                seen = [
                    await self._delivered(where, events, fact_id, old)
                    for where, events in (('push', hook.events), ('subscribe', stream))
                ]
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if added:  # leave memory as we found it, as far as the admin API allows
                    await self._admin(f'/dev/facts/{fact_id}/retire', {'note': TEST_NOTE})
                await self._close(task['id'], hook)
            for body in hook.events.items:
                self.inspect(body)
            _expect(
                all(h.get('authorization') == f'Bearer {secret}' for h in hook.headers),
                'a push lacked the configured Authorization header',
            )
            _expect(
                all(h.get('x-a2a-notification-token') == 'mem2a-conform' for h in hook.headers),
                'a push lacked the X-A2A-Notification-Token header',
            )
            types = {h.get('content-type', '').split(';')[0] for h in hook.headers}
        note = (
            ''
            if types == {'application/a2a+json'}
            else f' (push Content-Type {", ".join(sorted(types))})'
        )
        return f'a new fact reached push and subscribe as dossier {seen[0]} (was {old}){note}'

    async def access(self) -> str:
        """A source the principal can no longer read: its fact is removed."""
        if not self.options.dev_admin:
            raise _Skip('needs --dev-admin (a reference-style admin API)')
        task = await self._past_questions(await self.send(self._message('intent', self._intent())))
        self._expect_phase(task, 'TASK_STATE_INPUT_REQUIRED', 'awaiting-commit')
        fact_id = f'conform-{uuid.uuid4().hex[:8]}'
        source = f'other:mem2a-conform-{fact_id}'
        added = False
        try:
            await self._admin(
                '/dev/facts',
                {
                    'id': fact_id,
                    'statement': TEST_NOTE,
                    'source': source,
                    'confirmedBy': 'system:mem2a-conform',
                    'entities': [self.options.entity],
                },
            )
            added = True
            before = _artifact_part(await self._get(task['id']), 'dossier') or {}
            _expect(fact_id in before.get('watching', []), 'a new fact did not reach the dossier')
            readers = {'readers': ['group:mem2a-conform-nobody']}
            updated = await self._admin(f'/dev/sources/{source}/readers', readers)
            _expect(task['id'] in updated, f'updatedTasks {updated} does not list the task')
            now = await self._get(task['id'])
            after = _artifact_part(now, 'dossier') or {}
            _expect(fact_id not in json.dumps(after), 'the dossier still shows the fact')
            _expect(after.get('version') != before['version'], 'the dossier version did not change')
            update = _status_part(now, 'update') or {}
            _expect(
                (update.get('previousVersion'), update.get('dossierVersion'))
                == (before['version'], after.get('version')),
                f'the update names {update.get("previousVersion")} -> '
                f'{update.get("dossierVersion")}',
            )
            _expect(
                {'id': fact_id, 'kind': 'fact', 'change': 'removed'} in update.get('changes', []),
                'the update does not list the fact as removed',
            )
            metadata = _message(now).get('metadata', {})
            _expect(
                metadata.get(PHASE_KEY) == 'awaiting-commit' and IN_REPLY_TO_KEY not in metadata,
                'the update is not an awaiting-commit status without inReplyTo',
            )
        finally:
            if added:  # leave memory as we found it, as far as the admin API allows
                await self._admin(f'/dev/facts/{fact_id}/retire', {'note': TEST_NOTE})
            await self._cancel(task['id'])
        return (
            f'unreadable source: its fact removed in dossier {after["version"]} '
            f'(was {before["version"]})'
        )

    # ----------------------------------------------------------- helpers
    async def _get(self, task_id: str) -> Json:
        task = (await self.rpc('GetTask', {'id': task_id})).get('result')
        if not isinstance(task, dict):
            raise _Fail(f'GetTask {task_id} returned no task')
        return task

    async def rpc(
        self, method: str, params: Json, *, token: str | None = None, activate: bool = True
    ) -> Json:
        """One JSON-RPC call; returns the response body, inspected."""
        assert self.rpc_url is not None
        body = {'jsonrpc': '2.0', 'id': str(next(self._ids)), 'method': method, 'params': params}
        response = await self.http.post(
            self.rpc_url, json=body, headers=self._headers(token, activate)
        )
        data = response.json()
        _expect(
            isinstance(data, dict), f'{method}: HTTP {response.status_code} without a JSON-RPC body'
        )
        self.inspect(data)
        return data  # type: ignore[no-any-return]

    async def stream(self, method: str, params: Json) -> AsyncGenerator[Json, None]:
        """A streaming JSON-RPC call; yields each event's result, inspected."""
        assert self.rpc_url is not None
        body = {'jsonrpc': '2.0', 'id': str(next(self._ids)), 'method': method, 'params': params}
        headers = {**self._headers(None, True), 'Accept': 'text/event-stream'}
        timeout = httpx.Timeout(self.options.timeout, read=None)
        async with self.http.stream(
            'POST', self.rpc_url, json=body, headers=headers, timeout=timeout
        ) as response:
            if not response.headers.get('content-type', '').startswith('text/event-stream'):
                data = json.loads(await response.aread())
                raise _Fail(f'{method}: {_describe(data)}')
            lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    lines.append(line[5:].strip())
                elif not line and lines:
                    event = json.loads('\n'.join(lines))
                    lines = []
                    self.inspect(event)
                    _expect('result' in event, f'{method}: {_describe(event)}')
                    yield event['result']

    async def send(self, message: Json, configuration: Json | None = None) -> Json:
        """SendMessage; returns the task after checking it names the message."""
        params: Json = {'message': message}
        if configuration:
            params['configuration'] = configuration
        body = await self.rpc('SendMessage', params)
        task = body.get('result', {}).get('task')
        _expect(isinstance(task, dict), f'SendMessage: expected a task, got {_describe(body)}')
        replied_to = _message(task).get('metadata', {}).get(IN_REPLY_TO_KEY)
        _expect(
            replied_to == message['messageId'],
            f'the reply has inReplyTo {replied_to!r}, not {message["messageId"]!r}',
        )
        if _state(task) in OPEN_STATES:
            self._opened.add(task['id'])
        return task  # type: ignore[no-any-return]

    def inspect(self, node: Any) -> None:
        """Check every Mem2A payload and memory message in `node`."""
        for item in _walk_memory(node):
            if not isinstance(item, dict):
                continue
            media_type = item.get('mediaType')
            if isinstance(media_type, str) and media_type.startswith('application/vnd.mem2a.'):
                kind = MEDIA_TYPES.get(media_type)
                if kind is None:
                    self.problems.append(f'unknown media type {media_type}')
                    continue
                self.payloads += 1
                errors = validation.errors(kind, item.get('data'))
                if errors:
                    self.problems.append(f'{kind}: {errors[0]}')
            if 'messageId' in item and item.get('role') == 'ROLE_AGENT':
                self._inspect_message(item)
            if str(item.get('state', '')).startswith('TASK_STATE_') and 'message' in item:
                phase = item['message'].get('metadata', {}).get(PHASE_KEY)
                if phase not in PHASES.get(item['state'], set()):
                    self.problems.append(f'phase {phase} with state {item["state"]}')
            if 'artifactId' in item:
                kinds = [MEDIA_TYPES.get(p.get('mediaType', '')) for p in item.get('parts', [])]
                mem2a = [k for k in kinds if k]
                if mem2a and (
                    len(mem2a) != 1
                    or item['artifactId'] != mem2a[0]
                    or item.get('name') != mem2a[0]
                ):
                    self.problems.append(
                        f'artifact {item["artifactId"]} does not use its reserved id'
                    )
                if mem2a and EXTENSION_URI not in item.get('extensions', []):
                    self.problems.append(
                        f'artifact {item["artifactId"]} does not list the extension'
                    )

    def _inspect_message(self, message: Json) -> None:
        parts = message.get('parts', [])
        kinds = {MEDIA_TYPES.get(p.get('mediaType', '')) for p in parts} - {None}
        if EXTENSION_URI not in message.get('extensions', []):
            self.problems.append('a memory message does not list the extension')
        if kinds & AGENT_PAYLOADS:
            self.problems.append('a memory message carries an agent payload')
        if not any('text' in p for p in parts):
            self.problems.append('a memory message has no text part')

    def _headers(self, token: str | None, activate: bool) -> dict[str, str]:
        headers = {
            'A2A-Version': '1.0',
            'Authorization': f'Bearer {token or self.options.token}',
        }
        if activate:
            headers['A2A-Extensions'] = EXTENSION_URI
        return headers

    def _intent(self, **changes: Any) -> Json:
        intent = {
            'action': self.options.action,
            'summary': f'mem2a-conform: checking this memory ({TEST_NOTE})',
            'entities': [self.options.entity],
            'onBehalfOf': self.options.principal,
        }
        return {**intent, **changes}

    def _commit(self, based_on: str, conflicts: list[Json] | None = None) -> Json:
        commit = {
            'basedOn': based_on,
            'action': self.options.action,
            'outcome': 'done',
            'summary': f'mem2a-conform: {TEST_NOTE}',
            'claims': [
                {'statement': f'mem2a-conform: {TEST_NOTE}', 'entities': [self.options.entity]}
            ],
        }
        return {**commit, 'conflicts': conflicts} if conflicts else commit

    def _message(self, kind: PayloadKind, data: Json, task: Json | None = None) -> Json:
        message = {
            'messageId': f'mem2a-conform-{uuid.uuid4()}',
            'role': 'ROLE_USER',
            'parts': [{'mediaType': MEDIA_TYPE_OF[kind], 'data': data}],
            'extensions': [EXTENSION_URI],
        }
        if task is not None:
            message |= {'taskId': task['id'], 'contextId': task['contextId']}
        return message

    def _dossier(self) -> Json:
        dossier = _artifact_part(self.task or {}, 'dossier')
        _expect(dossier is not None, 'the task has no dossier')
        assert dossier is not None
        return dossier

    async def _past_questions(self, task: Json) -> Json:
        """Answer up to three questions (with their first option) to reach a dossier."""
        for _ in range(3):
            question = _status_part(task, 'question')
            if _phase(task) != 'question' or question is None:
                break
            text = (question.get('options') or ['Yes'])[0]
            task = await self.send(
                self._message('answer', {'questionId': question['id'], 'text': text}, task)
            )
        return task

    async def _artifact_order(self) -> str:
        """With SendStreamingMessage, the dossier artifact must come before its status."""
        if 'subscribe' not in self.params['listen']:
            return 'artifact order not checked (no streaming)'
        events = [
            e
            async for e in self.stream(
                'SendStreamingMessage', {'message': self._message('intent', self._intent())}
            )
        ]
        _expect(events and 'task' in events[0], 'SendStreamingMessage did not start with the task')
        await self._cancel(events[0]['task']['id'])  # only its events matter
        kinds = [
            'dossier'
            if _artifact_part(e.get('artifactUpdate', {}), 'dossier')
            else next(iter(e), '')
            for e in events
        ]
        statuses = [
            i
            for i, e in enumerate(events)
            if e.get('statusUpdate', {}).get('status', {}).get('state')
            == 'TASK_STATE_INPUT_REQUIRED'
        ]
        if 'dossier' not in kinds:
            return 'artifact order not checked (memory asked a question)'
        _expect(
            statuses and kinds.index('dossier') < statuses[0],
            'the INPUT_REQUIRED status came before the dossier artifact',
        )
        return 'the dossier artifact precedes its status'

    async def _delivered(self, where: str, events: _Events, fact_id: str, old: str | None) -> str:
        found = await events.wait(lambda items: _update_after(items, fact_id), self.options.timeout)
        dossier, message, update = found
        new = dossier['version']
        _expect(new != old, f'{where}: the dossier version did not change')
        _expect(
            (update['dossierVersion'], update['previousVersion']) == (new, old),
            f'{where}: update names {update["previousVersion"]} -> {update["dossierVersion"]}',
        )
        _expect(
            {'id': fact_id, 'kind': 'fact', 'change': 'added'} in update['changes'],
            f'{where}: the update does not list the fact as added',
        )
        metadata = message.get('metadata', {})
        _expect(
            metadata.get(PHASE_KEY) == 'awaiting-commit',
            f'{where}: phase {metadata.get(PHASE_KEY)}',
        )
        _expect(
            IN_REPLY_TO_KEY not in metadata, f'{where}: an out-of-band update carries inReplyTo'
        )
        return str(new)

    async def _admin(self, path: str, body: Json) -> list[str]:
        response = await self.http.post(f'{self.options.url}{path}', json=body)
        data = (
            response.json()
            if response.headers.get('content-type', '').startswith('application/json')
            else {}
        )
        _expect(
            response.status_code == 200 and data.get('ok') is True,
            f'POST {path}: HTTP {response.status_code} {data or response.text[:200]}',
        )
        return list(data.get('updatedTasks', []))

    async def _close(self, task_id: str, hook: _Webhook) -> None:
        """Cancel the listen task while its webhook still answers, so memory
        isn't left retrying pushes to a closed port."""
        await self._cancel(task_id)

        def canceled(items: list[Json]) -> bool:
            states = [i.get('statusUpdate', {}).get('status', {}).get('state') for i in items]
            return 'TASK_STATE_CANCELED' in states

        with contextlib.suppress(asyncio.TimeoutError):
            await hook.events.wait(canceled, self.options.timeout)

    async def _task_ids(self) -> set[str] | None:
        body = await self.rpc('ListTasks', {})
        tasks = body.get('result', {}).get('tasks') if 'result' in body else None
        return None if tasks is None else {t['id'] for t in tasks}

    def _expect_phase(self, task: Json, state: str, phase: str) -> None:
        _expect(
            (_state(task), _phase(task)) == (state, phase),
            f'expected {state}, phase {phase}; got {_describe({"result": task})}',
        )

    def _expect_error(self, task: Json, phase: str, code: str) -> None:
        self._expect_phase(task, 'TASK_STATE_INPUT_REQUIRED', phase)
        error = _status_part(task, 'error') or {}
        _expect(error.get('code') == code, f'expected error {code}, got {error.get("code")}')

    def _expect_refused(self, task: Json, code: str) -> None:
        self._expect_phase(task, 'TASK_STATE_REJECTED', 'refused')
        error = _status_part(task, 'error') or {}
        _expect(error.get('code') == code, f'expected error {code}, got {error.get("code")}')
        _expect(_artifact_part(task, 'dossier') is None, f'a refusal ({code}) came with a dossier')

    async def _cancel(self, task_id: str) -> None:
        """Cancel a task this run opened (an error reply is fine: it may have ended)."""
        self._opened.discard(task_id)
        await self.rpc('CancelTask', {'id': task_id})

    async def _cleanup(self) -> None:
        """Cancel every task the checks opened and left open."""
        for task_id in sorted(self._opened):
            with contextlib.suppress(httpx.HTTPError, ValueError, _Fail):
                await self._cancel(task_id)


def _walk_memory(node: Any) -> Iterator[Any]:
    """Like `_walk`, but skips the agent's own messages (some are invalid on purpose)."""
    if isinstance(node, dict) and node.get('role') == 'ROLE_USER' and 'messageId' in node:
        return
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        yield from _walk_memory(child)


class _Events:
    """Events that arrive in the background, and a way to wait for one."""

    def __init__(self) -> None:
        self.items: list[Json] = []
        self.headers: list[dict[str, str]] = []
        self._changed = asyncio.Condition()

    async def add(self, item: Json, headers: dict[str, str] | None = None) -> None:
        async with self._changed:
            self.items.append(item)
            if headers is not None:
                self.headers.append(headers)
            self._changed.notify_all()

    async def read(self, events: AsyncGenerator[Json, None]) -> None:
        async with aclosing(events):
            async for event in events:
                await self.add(event)

    async def wait(self, found: Callable[[list[Json]], Any], timeout: float) -> Any:
        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(lambda: found(self.items)), timeout)
            return found(self.items)


class _Webhook:
    """A push notification receiver on this machine, for the listen check."""

    def __init__(self, host: str) -> None:
        self.events = _Events()
        self._socket = listen_socket(host)
        shown = f'[{host}]' if ':' in host else host
        self.url = f'http://{shown}:{self._socket.getsockname()[1]}/mem2a-conform'
        app = Starlette(routes=[Route('/mem2a-conform', self._receive, methods=['POST'])])
        self._server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        self._task: asyncio.Task[None] | None = None

    @property
    def headers(self) -> list[dict[str, str]]:
        return self.events.headers

    async def __aenter__(self) -> _Webhook:
        self._task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._socket.close()

    async def _receive(self, request: Request) -> JSONResponse:
        body = await request.json()
        headers = {key.lower(): value for key, value in request.headers.items()}
        await self.events.add(body, headers)
        return JSONResponse({})


# ================================================================ running
async def check_memory(options: Options) -> list[Result]:
    """Run every check against the memory at ``options.url``."""
    async with httpx.AsyncClient(timeout=options.timeout) as http:
        return await Checker(options, http).run()


def render(url: str, results: Sequence[Result]) -> str:
    """The human-readable report."""
    width = max(len(r.check) for r in results)
    sections = max(len(r.section) for r in results)
    lines = [f'mem2a-conform: {url}', '']
    lines += [
        f'{r.status:<4}  {r.section:<{sections}}  {r.check:<{width}}  {r.detail}' for r in results
    ]
    counts = {
        status: sum(r.status == status for r in results) for status in ('PASS', 'FAIL', 'SKIP')
    }
    lines += ['', f'{counts["PASS"]} passed, {counts["FAIL"]} failed, {counts["SKIP"]} skipped']
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None, *, prog: str = 'mem2a-conform') -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description='Check a running memory against Mem2A v0.1, from the outside. Exits 1 '
        'if any check fails.',
    )
    parser.add_argument(
        '--url', required=True, help="the memory's base URL (where its Agent Card is)"
    )
    parser.add_argument('--token', required=True, help='a bearer token for the principal')
    parser.add_argument('--principal', required=True, help='who the token acts for, e.g. user:tom')
    parser.add_argument(
        '--entity', required=True, help='an entity to name in intents, e.g. account:acme'
    )
    parser.add_argument(
        '--action', required=True, help='an action to name in intents, e.g. send_quote'
    )
    parser.add_argument(
        '--other-token', help='a token for another agent or principal (binding check)'
    )
    parser.add_argument('--allow-writes', action='store_true', help='also commit a test claim')
    parser.add_argument(
        '--dev-admin', action='store_true', help='also use the /dev admin API to check updates'
    )
    parser.add_argument(
        '--webhook-host', default='127.0.0.1', help='where the listen check receives pushes'
    )
    parser.add_argument(
        '--timeout', type=float, default=10.0, help='seconds to wait for each step (default 10)'
    )
    parser.add_argument('--json', action='store_true', help='print the results as JSON')
    args = parser.parse_args(argv)
    options = Options(
        url=args.url.rstrip('/'),
        token=args.token,
        principal=args.principal,
        entity=args.entity,
        action=args.action,
        other_token=args.other_token,
        allow_writes=args.allow_writes,
        dev_admin=args.dev_admin,
        webhook_host=args.webhook_host,
        timeout=args.timeout,
    )
    results = asyncio.run(check_memory(options))
    failed = any(r.status == 'FAIL' for r in results)
    if args.json:
        counts = {s.lower(): sum(r.status == s for r in results) for s in ('PASS', 'FAIL', 'SKIP')}
        print(
            json.dumps(
                {'url': options.url, 'results': [asdict(r) for r in results], **counts}, indent=2
            )
        )
    else:
        print(render(options.url, results))
    return 1 if failed else 0
