# SPDX-License-Identifier: Apache-2.0
"""Unauthenticated admin routes for sandboxes (``mem2a serve --dev-admin``).

They let a person play the rest of the company while agents hold open tasks:
add, retire and confirm facts, and change who may read a source. Anyone who
can reach them can read and change the memory, so never expose them.

JSON in, JSON out:

* ``GET /dev/state``: facts, precedent, policies, sources, and open tasks with
  their phase and dossier version.
* ``POST /dev/facts`` ``{id, statement, source, confirmedBy, entities,
  supersedes?, note?, readers?}``: add a confirmed fact. An unknown `source`
  ref is created first, readable by everyone unless `readers` is given
  (`readers` is refused for a source that already exists).
* ``POST /dev/facts/{id}/retire`` ``{note?}``
* ``POST /dev/facts/{id}/confirm`` ``{by}``
* ``POST /dev/sources/{ref}/readers`` ``{readers: [...]}`` (null: everyone)
* ``POST /dev/scenarios/acme/legal-clears``: ``f-340`` supersedes ``f-311``,
  so ``c-17`` and ``p-4`` lapse (the ``acme`` seed's example 03).

Each change answers ``{"ok": true, "updatedTasks": [...]}``: the open tasks
that got an update because of it. Errors answer ``{"ok": false, "error": ...}``
with status 400, 404 or 409.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast, get_args

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from mem2a.engine import FactRecord, MemoryEngine
from mem2a.models import SourceKind
from mem2a.seeds import legal_clears


if TYPE_CHECKING:
    from mem2a.server import Deliveries

Handler = Callable[[Request], Awaitable[JSONResponse]]

#: Source kinds an admin-created source can have (agent-commit is for commits).
_SOURCE_KINDS = frozenset(get_args(SourceKind)) - {'agent-commit'}


class _Invalid(Exception):
    """A request the admin API can't act on; answered as JSON."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def admin_routes(engine: MemoryEngine, deliveries: Deliveries) -> list[BaseRoute]:
    """The ``/dev`` routes for `engine`; changes are delivered by `deliveries`."""

    async def change(apply: Callable[[], object]) -> JSONResponse:
        """Apply a change, wait for the updates it causes, and list who got one."""
        before = {t.id: t.dossier.version for t in engine.open_tasks() if t.dossier}
        apply()
        await deliveries.wait_idle()
        updated = [
            task_id
            for task_id, version in before.items()
            if (task := engine.task(task_id)) and task.dossier and task.dossier.version != version
        ]
        return JSONResponse({'ok': True, 'updatedTasks': updated})

    async def state(request: Request) -> JSONResponse:
        return JSONResponse(_state(engine))

    async def add_fact(request: Request) -> JSONResponse:
        body = await _body(request)
        fact_id, statement = _text(body, 'id'), _text(body, 'statement')
        source, confirmed_by = _text(body, 'source'), _text(body, 'confirmedBy')
        entities, supersedes = _texts(body, 'entities'), _optional_texts(body, 'supersedes')
        note, readers = _optional_text(body, 'note'), _optional_texts(body, 'readers')
        # Check what the engine would refuse before creating a source for it.
        if (
            engine.fact(fact_id) is not None
            or any(p.id == fact_id for p in engine.precedents())
            or any(p.id == fact_id for p in engine.policies())
        ):
            raise _Invalid(f'{fact_id} is already in use (facts, precedent, policies)', 409)
        for old_id in supersedes or ():
            old = engine.fact(old_id)
            if old is None or old.status != 'confirmed':
                raise _Invalid(f'{old_id} is not a confirmed fact, so it cannot be superseded')
        known = source in {s.ref for s in engine.sources()}
        if known and readers is not None:
            raise _Invalid(
                f'Source {source} already exists; change its readers with '
                f'POST /dev/sources/{source}/readers',
                409,
            )

        def apply() -> None:
            if not known:
                engine.add_source(source, kind=_kind_of(source), readers=readers)
            engine.add_fact(
                fact_id,
                statement,
                source=source,
                confirmed_by=confirmed_by,
                entities=entities,
                supersedes=supersedes or (),
                note=note,
            )

        return await change(apply)

    async def retire_fact(request: Request) -> JSONResponse:
        fact = _fact(engine, request)
        note = _optional_text(await _body(request), 'note')
        return await change(lambda: engine.retire_fact(fact.id, note=note))

    async def confirm_fact(request: Request) -> JSONResponse:
        fact = _fact(engine, request)
        by = _text(await _body(request), 'by')
        return await change(lambda: engine.confirm_fact(fact.id, by=by))

    async def source_readers(request: Request) -> JSONResponse:
        ref = request.path_params['ref']
        if ref not in {s.ref for s in engine.sources()}:
            raise _Invalid(f'Unknown source {ref}', 404)
        body = await _body(request)
        if 'readers' not in body:
            raise _Invalid('readers is required: a list of principals and groups, or null')
        readers = _optional_texts(body, 'readers')
        return await change(lambda: engine.set_source_readers(ref, readers))

    async def acme_legal_clears(request: Request) -> JSONResponse:
        if engine.fact('f-311') is None:
            raise _Invalid('This memory was not seeded with acme', 409)
        if engine.fact('f-340') is not None:
            raise _Invalid('Legal already cleared Acme pricing', 409)
        return await change(lambda: legal_clears(engine))

    return [
        Route('/dev/state', _json(state), methods=['GET']),
        Route('/dev/facts', _json(add_fact), methods=['POST']),
        Route('/dev/facts/{id}/retire', _json(retire_fact), methods=['POST']),
        Route('/dev/facts/{id}/confirm', _json(confirm_fact), methods=['POST']),
        Route('/dev/sources/{ref:path}/readers', _json(source_readers), methods=['POST']),
        Route('/dev/scenarios/acme/legal-clears', _json(acme_legal_clears), methods=['POST']),
    ]


def _json(handler: Handler) -> Handler:
    """Answer errors as ``{"ok": false, "error": ...}``."""

    async def endpoint(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except _Invalid as error:
            return JSONResponse({'ok': False, 'error': str(error)}, status_code=error.status)
        except KeyError as error:
            return JSONResponse({'ok': False, 'error': f'Unknown id {error}'}, status_code=404)
        except ValueError as error:
            return JSONResponse({'ok': False, 'error': str(error)}, status_code=400)

    return endpoint


def _state(engine: MemoryEngine) -> dict[str, Any]:
    return {
        'facts': [_fact_state(f) for f in engine.facts()],
        'precedent': [
            {
                'id': p.id,
                'version': str(p.revision),
                'statement': p.statement,
                'relevance': p.relevance,
                'source': p.sources[0],
                'retired': p.retired,
            }
            for p in engine.precedents()
        ],
        'policies': [
            {
                'id': p.id,
                'version': str(p.revision),
                'statement': p.statement,
                'level': p.level,
                'basis': list(p.basis) or [p.policy],
                'retired': p.retired,
            }
            for p in engine.policies()
        ],
        'sources': [
            {
                'ref': s.ref,
                'kind': s.kind,
                'readers': None if s.readers is None else list(s.readers),
            }
            for s in engine.sources()
        ],
        'tasks': [
            {
                'id': t.id,
                'agent': t.identity.agent,
                'principal': t.identity.principal,
                'phase': t.phase,
                'dossierVersion': t.dossier.version if t.dossier else None,
                'action': t.intent.action if t.intent else None,
                'entities': list(t.intent.entities) if t.intent else [],
                'expiresAt': t.expires_at.isoformat() if t.expires_at else None,
            }
            for t in engine.open_tasks()
        ],
    }


def _fact_state(f: FactRecord) -> dict[str, Any]:
    state: dict[str, Any] = {
        'id': f.id,
        'version': str(f.revision),
        'status': f.status,
        'statement': f.statement,
        'source': f.sources[0],
        'entities': list(f.entities),
        'retired': f.retired,
    }
    if f.confirmed_by:
        state['confirmedBy'] = f.confirmed_by
    if f.origin:
        state['claimedBy'] = f.origin.principal
    if f.superseded_by:
        state['supersededBy'] = f.superseded_by
    if f.retired_note:
        state['note'] = f.retired_note
    return state


def _fact(engine: MemoryEngine, request: Request) -> FactRecord:
    fact = engine.fact(request.path_params['id'])
    if fact is None:
        raise _Invalid(f'Unknown fact {request.path_params["id"]}', 404)
    return fact


def _kind_of(ref: str) -> SourceKind:
    """``email:...`` -> ``email``; anything unknown -> ``other``."""
    prefix = ref.partition(':')[0]
    return cast(SourceKind, prefix) if prefix in _SOURCE_KINDS else 'other'


async def _body(request: Request) -> dict[str, Any]:
    if not (await request.body()).strip():
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise _Invalid('The body must be JSON') from None
    if not isinstance(body, dict):
        raise _Invalid('The body must be a JSON object')
    return body


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise _Invalid(f'{key} must be a non-empty string')
    return value


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    return None if body.get(key) is None else _text(body, key)


def _texts(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _Invalid(f'{key} must be a list of non-empty strings')
    return value


def _optional_texts(body: dict[str, Any], key: str) -> list[str] | None:
    return None if body.get(key) is None else _texts(body, key)
