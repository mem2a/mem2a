# SPDX-License-Identifier: Apache-2.0
"""The memory itself: a transport-agnostic, in-memory Mem2A engine.

The engine knows nothing about A2A. The server (``mem2a.server``) calls
`MemoryEngine.negotiate`, `respond`, `refresh`, `cancel` and `expire`, and
turns each returned `Reply` into A2A artifacts and one status message.

How this reference memory decides what goes in a dossier:

* **Facts** are included when they are not retired, their entities intersect
  the intent's entities, and the principal can read *every* source the fact
  derives from ("permissions follow the source"). Claims are facts too.
* **Precedent** is included when it is not retired, the principal can read
  every source, and one of its `Relevance` rules matches the intent (by
  action, entities, answers given, or a *confirmed* fact in this dossier).
  The matching rule's text becomes the precedent's ``relevance``.
* **Constraints** come only from policies that people configured
  (`add_policy`). A policy fires when one of its basis items is a confirmed
  fact or a precedent in this very dossier (so its basis is visible to the
  principal), or, for a standing policy, when the intent matches its filters.
  Claims never trigger constraints: an agent cannot instruct other agents
  by writing to memory.
* ``watching`` lists every fact, precedent and constraint id in the dossier.

Listening: every change to memory (facts added, updated, retired or
superseded, reader sets changed, claims recorded) notifies listeners with the
ids of open ``awaiting-commit`` tasks whose entities intersect the change or
whose dossier watches a changed id. The server then calls `refresh` for each,
at delivery time, which recomputes the dossier with current permissions and
returns an update only if the content changed.

Versions are opaque strings. Dossier versions come from one memory-wide
counter; fact versions count each fact's revisions.

The engine is not thread-safe: call it from one event loop.
"""

from __future__ import annotations

import itertools
import logging
import re

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mem2a import models, validation
from mem2a.auth import Identity
from mem2a.constants import (
    AGENT_PAYLOADS,
    MEDIA_TYPES,
    ErrorCode,
    Phase,
)
from mem2a.models import SourceKind


logger = logging.getLogger(__name__)

#: Incoming Mem2A parts of one agent message, as (mediaType, data) pairs.
Payloads = Sequence[tuple[str, Any]]
#: Called with the ids of open tasks whose dossier may have changed.
ChangeListener = Callable[[list[str]], None]
#: Writes the dossier's one-line brief.
Summarizer = Callable[
    [Sequence[models.Fact], Sequence[models.Precedent], Sequence[models.Constraint]],
    str,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def entity_type(entity: str) -> str:
    """``account:acme`` -> ``account``."""
    return entity.split(':', 1)[0] if ':' in entity else ''


def _tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    """Ordered, de-duplicated tuple."""
    return tuple(dict.fromkeys(values or ()))


def _normalize_answer(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + '…'


# ============================================================== records
@dataclass
class SourceRecord:
    """Where memory learned something, and who may read it."""

    ref: str
    kind: SourceKind
    title: str | None = None
    url: str | None = None
    at: datetime | None = None
    #: Principals and groups allowed to read it. ``None``: everyone.
    readers: tuple[str, ...] | None = None

    def model(self) -> models.Source:
        return models.Source(
            ref=self.ref, kind=self.kind, title=self.title, url=self.url, at=self.at
        )


@dataclass
class FactRecord:
    id: str
    statement: str
    status: Literal['confirmed', 'claim']
    source: str
    entities: tuple[str, ...]
    derived_from: tuple[str, ...] = ()
    confirmed_by: str | None = None
    claimed_by: models.Attribution | None = None
    observed_at: datetime | None = None
    supersedes: tuple[str, ...] = ()
    #: Extra reader restriction on the fact itself (used for claims).
    readers: tuple[str, ...] | None = None
    evidence: tuple[models.Evidence, ...] = ()
    revision: int = 1
    retired: bool = False
    superseded_by: str | None = None

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.source, *self.derived_from)


@dataclass(frozen=True)
class Relevance:
    """When a precedent bears on an intent, and why.

    Every filter that is set must match: the intent's ``action``, an entity of
    the intent, a *confirmed* fact in the same dossier, all answer ``tags``
    the task collected. ``text`` becomes the precedent's ``relevance``.
    """

    text: str
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ('actions', 'entities', 'facts', 'tags'):
            object.__setattr__(self, name, frozenset(getattr(self, name)))


@dataclass
class PrecedentRecord:
    id: str
    statement: str
    source: str
    relevance: tuple[Relevance, ...]
    derived_from: tuple[str, ...] = ()
    decided_by: str | None = None
    decided_at: datetime | None = None
    entities: tuple[str, ...] = ()
    retired: bool = False

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.source, *self.derived_from)


@dataclass
class PolicyRecord:
    """A rule people configured. Produces the constraint with the same id."""

    id: str
    statement: str
    level: Literal['must', 'should']
    #: Fact or precedent ids that trigger it (any one present suffices).
    basis: tuple[str, ...] = ()
    #: Standing policy reference, used as the basis when `basis` is empty.
    policy: str | None = None
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    until: str | None = None
    retired: bool = False


@dataclass
class QuestionRule:
    """A question memory asks before it can prepare a dossier."""

    id: str
    text: str
    options: tuple[str, ...] = ()
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    entity_types: frozenset[str] = frozenset()
    #: Normalized answer text -> tags the answer adds to the task.
    tags: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: Human-readable text part; defaults to the question text.
    prompt: str | None = None


@dataclass
class RefusalRule:
    """Intents memory declines to prepare a dossier for."""

    id: str
    message: str
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    entity_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReviewItem:
    """Something a person should look at, recorded from a commit."""

    kind: Literal['conflict', 'supersede-proposal']
    task_id: str
    commit_id: str
    item_id: str
    explanation: str
    agent: str
    principal: str
    at: datetime


@dataclass
class TaskRecord:
    id: str
    context_id: str
    identity: Identity
    intent: models.Intent | None
    phase: Phase
    created_at: datetime
    expires_at: datetime | None = None
    pending: QuestionRule | None = None
    answers: dict[str, str] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)
    dossier: models.Dossier | None = None

    @property
    def open(self) -> bool:
        return self.phase in ('question', 'awaiting-commit')


@dataclass(frozen=True)
class Reply:
    """What memory says on a task.

    The server emits `dossier` and `receipt` as artifacts first, then one
    status message with `text` plus the `question`, `update` or `error`
    payload (at most one of them), marked with `phase`.
    """

    phase: Phase
    text: str
    dossier: models.Dossier | None = None
    receipt: models.Receipt | None = None
    question: models.Question | None = None
    update: models.Update | None = None
    error: models.Error | None = None


def default_summary(
    facts: Sequence[models.Fact],
    precedent: Sequence[models.Precedent],
    constraints: Sequence[models.Constraint],
) -> str:
    """A deterministic one-line brief. Swap in your own (for example an LLM)."""
    musts = [c for c in constraints if c.level == 'must']
    shoulds = [c for c in constraints if c.level == 'should']
    if musts:
        more = f' (and {len(musts) - 1} more rule(s))' if len(musts) > 1 else ''
        text = f'Hold: {musts[0].statement}{more}'
    elif shoulds:
        text = 'Go ahead, with care: ' + ' '.join(c.statement for c in shoulds)
    elif facts:
        label = 'unconfirmed claim' if facts[0].status == 'claim' else 'most recent'
        text = f'Nothing here blocks this. {label.capitalize()}: {facts[0].statement}'
    else:
        text = 'Memory has nothing on this yet.'
    return _clip(text, 2048)


# ============================================================== engine
class MemoryEngine:
    """An in-memory company memory that speaks Mem2A v0.1."""

    def __init__(
        self,
        *,
        watch_timeout: timedelta | None = timedelta(days=7),
        clock: Callable[[], datetime] = utcnow,
        summarize: Summarizer = default_summary,
    ) -> None:
        self.watch_timeout = watch_timeout
        self.clock = clock
        self.summarize = summarize
        self._sources: dict[str, SourceRecord] = {}
        self._facts: dict[str, FactRecord] = {}
        self._precedent: dict[str, PrecedentRecord] = {}
        self._policies: dict[str, PolicyRecord] = {}
        self._questions: dict[str, QuestionRule] = {}
        self._refusals: dict[str, RefusalRule] = {}
        self._entity_readers: dict[str, tuple[str, ...]] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._notes: dict[str, str] = {}
        self._listeners: list[ChangeListener] = []
        self._versions = itertools.count(1)
        self._commits = itertools.count(1)
        self._next_fact = 1
        self.review_queue: list[ReviewItem] = []

    # ------------------------------------------------------------ listeners
    def on_change(self, listener: ChangeListener) -> Callable[[], None]:
        """Register a listener; returns a function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # --------------------------------------------------- content: sources
    def add_source(
        self,
        ref: str,
        *,
        kind: SourceKind,
        title: str | None = None,
        url: str | None = None,
        at: datetime | None = None,
        readers: Iterable[str] | None = None,
    ) -> SourceRecord:
        record = SourceRecord(
            ref, kind, title, url, at, None if readers is None else _tuple(readers)
        )
        self._sources[ref] = record
        return record

    def set_source_readers(self, ref: str, readers: Iterable[str] | None) -> None:
        """Change who may read a source; affected dossiers are re-checked."""
        self._sources[ref].readers = None if readers is None else _tuple(readers)
        ids = {f.id for f in self._facts.values() if ref in f.sources}
        ids |= {p.id for p in self._precedent.values() if ref in p.sources}
        entities = {e for i in ids for e in self._entities_of(i)}
        for item_id in ids:
            self._notes.pop(item_id, None)  # never explain access changes
        self._changed(ids, entities)

    def set_entity_readers(self, entity: str, readers: Iterable[str] | None) -> None:
        """Who may read claims about `entity` (applies to future commits)."""
        if readers is None:
            self._entity_readers.pop(entity, None)
        else:
            self._entity_readers[entity] = _tuple(readers)

    # ----------------------------------------------------- content: facts
    def add_fact(
        self,
        fact_id: str,
        statement: str,
        *,
        source: str,
        confirmed_by: str,
        entities: Iterable[str],
        derived_from: Iterable[str] = (),
        observed_at: datetime | None = None,
        supersedes: Iterable[str] = (),
        note: str | None = None,
    ) -> FactRecord:
        """Add a confirmed fact. Its sources must be registered first."""
        if fact_id in self._facts:
            raise ValueError(f'Fact {fact_id} already exists')
        record = FactRecord(
            id=fact_id,
            statement=statement,
            status='confirmed',
            source=source,
            entities=_tuple(entities),
            derived_from=_tuple(derived_from),
            confirmed_by=confirmed_by,
            observed_at=observed_at,
            supersedes=_tuple(supersedes),
        )
        self._require_sources(record.sources)
        self._store_fact(record)
        self._changed({fact_id}, set(record.entities), notes={fact_id: note})
        return record

    def update_fact(
        self,
        fact_id: str,
        *,
        statement: str | None = None,
        entities: Iterable[str] | None = None,
        note: str | None = None,
    ) -> FactRecord:
        record = self._facts[fact_id]
        before = set(record.entities)
        if statement is not None:
            record.statement = statement
        if entities is not None:
            record.entities = _tuple(entities)
        record.revision += 1
        self._changed({fact_id}, before | set(record.entities), notes={fact_id: note})
        return record

    def retire_fact(self, fact_id: str) -> None:
        """The fact no longer holds. Dossiers show it as removed."""
        record = self._facts[fact_id]
        record.retired = True
        self._notes.pop(fact_id, None)
        self._changed({fact_id}, set(record.entities))

    def supersede_fact(
        self,
        old: str | Iterable[str],
        fact_id: str,
        statement: str,
        *,
        source: str,
        confirmed_by: str,
        entities: Iterable[str],
        derived_from: Iterable[str] = (),
        observed_at: datetime | None = None,
        note: str | None = None,
    ) -> FactRecord:
        """Replace fact(s) `old` with a new confirmed fact."""
        old_ids = _tuple([old] if isinstance(old, str) else old)
        record = FactRecord(
            id=fact_id,
            statement=statement,
            status='confirmed',
            source=source,
            entities=_tuple(entities),
            derived_from=_tuple(derived_from),
            confirmed_by=confirmed_by,
            observed_at=observed_at,
            supersedes=old_ids,
        )
        self._require_sources(record.sources)
        if fact_id in self._facts:
            raise ValueError(f'Fact {fact_id} already exists')
        entities_changed = set(record.entities)
        for old_id in old_ids:
            old_record = self._facts[old_id]
            old_record.retired = True
            old_record.superseded_by = fact_id
            self._notes.pop(old_id, None)
            entities_changed |= set(old_record.entities)
        self._store_fact(record)
        self._changed({fact_id, *old_ids}, entities_changed, notes={fact_id: note})
        return record

    def confirm_fact(self, fact_id: str, *, by: str, note: str | None = None) -> FactRecord:
        """A person or system of record stands behind a claim.

        Not part of the protocol. The schema forbids ``claimedBy`` on a
        confirmed fact, so the attribution is dropped; the source
        (``commit:<id>``) still says where the claim came from.
        """
        record = self._facts[fact_id]
        if record.status == 'confirmed':
            return record
        record.status = 'confirmed'
        record.confirmed_by = by
        record.claimed_by = None
        record.revision += 1
        self._changed(
            {fact_id},
            set(record.entities),
            notes={fact_id: note or f'{by} confirmed: {record.statement}'},
        )
        return record

    # -------------------------------------------- content: precedent, rules
    def add_precedent(
        self,
        precedent_id: str,
        statement: str,
        *,
        source: str,
        relevance: Iterable[Relevance],
        decided_by: str | None = None,
        decided_at: datetime | None = None,
        entities: Iterable[str] = (),
        derived_from: Iterable[str] = (),
    ) -> PrecedentRecord:
        record = PrecedentRecord(
            id=precedent_id,
            statement=statement,
            source=source,
            relevance=tuple(relevance),
            derived_from=_tuple(derived_from),
            decided_by=decided_by,
            decided_at=decided_at,
            entities=_tuple(entities),
        )
        if not record.relevance:
            raise ValueError('A precedent needs at least one Relevance rule')
        self._require_sources(record.sources)
        self._precedent[precedent_id] = record
        self._changed({precedent_id}, set(record.entities), broad=True)
        return record

    def retire_precedent(self, precedent_id: str) -> None:
        self._precedent[precedent_id].retired = True
        self._changed({precedent_id}, set(), broad=True)

    def add_policy(
        self,
        constraint_id: str,
        statement: str,
        *,
        level: Literal['must', 'should'],
        basis: Iterable[str] = (),
        policy: str | None = None,
        actions: Iterable[str] = (),
        entities: Iterable[str] = (),
        until: str | None = None,
    ) -> PolicyRecord:
        """Configure a constraint.

        Give `basis` (fact/precedent ids: the constraint applies while one of
        them is a confirmed fact or a precedent in the dossier) or `policy` (a
        standing policy reference, e.g. ``policy:leadership-update-format``,
        that applies whenever `actions`/`entities` match the intent).
        """
        record = PolicyRecord(
            id=constraint_id,
            statement=statement,
            level=level,
            basis=_tuple(basis),
            policy=policy,
            actions=frozenset(actions),
            entities=frozenset(entities),
            until=until,
        )
        if bool(record.basis) == bool(record.policy):
            raise ValueError('Give exactly one of basis= or policy=')
        self._policies[constraint_id] = record
        self._changed({constraint_id, *record.basis}, set(record.entities), broad=True)
        return record

    def retire_policy(self, constraint_id: str) -> None:
        self._policies[constraint_id].retired = True
        self._changed({constraint_id}, set(), broad=True)

    def add_question(
        self,
        question_id: str,
        text: str,
        *,
        options: Iterable[str] = (),
        actions: Iterable[str] = (),
        entities: Iterable[str] = (),
        entity_types: Iterable[str] = (),
        tags: Mapping[str, Iterable[str]] | None = None,
        prompt: str | None = None,
    ) -> QuestionRule:
        """Ask `text` before preparing dossiers for matching intents.

        `tags` maps answers (compared case- and punctuation-insensitively)
        to tags that `Relevance` rules can require.
        """
        rule = QuestionRule(
            id=question_id,
            text=text,
            options=_tuple(options),
            actions=frozenset(actions),
            entities=frozenset(entities),
            entity_types=frozenset(entity_types),
            tags={
                _normalize_answer(answer): frozenset(values)
                for answer, values in (tags or {}).items()
            },
            prompt=prompt,
        )
        self._questions[question_id] = rule
        return rule

    def add_refusal(
        self,
        rule_id: str,
        message: str,
        *,
        actions: Iterable[str] = (),
        entities: Iterable[str] = (),
        entity_types: Iterable[str] = (),
    ) -> RefusalRule:
        """Refuse matching intents outright (phase ``refused``, code ``refused``)."""
        rule = RefusalRule(
            rule_id,
            message,
            frozenset(actions),
            frozenset(entities),
            frozenset(entity_types),
        )
        self._refusals[rule_id] = rule
        return rule

    # ------------------------------------------------------------ inspection
    def task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def fact(self, fact_id: str) -> FactRecord | None:
        return self._facts.get(fact_id)

    def open_tasks(self) -> list[TaskRecord]:
        return [t for t in self._tasks.values() if t.open]

    # ======================================================= the protocol
    def negotiate(
        self,
        task_id: str,
        context_id: str,
        identity: Identity,
        payloads: Payloads,
    ) -> Reply:
        """First message of a task: an intent."""
        now = self.clock()
        task = TaskRecord(
            id=task_id,
            context_id=context_id,
            identity=identity,
            intent=None,
            phase='refused',
            created_at=now,
        )
        self._tasks[task_id] = task

        data, problem = self._single_payload(payloads, 'intent')
        if problem is None:
            problem = '; '.join(validation.errors('intent', data)) or None
        if problem is not None:
            return self._refuse(
                'invalid-intent',
                f'The first message must carry one valid intent: {problem}',
                'Refused: the intent is not valid.',
            )
        intent = models.Intent.model_validate(data)
        if intent.on_behalf_of != identity.principal:
            return self._refuse(
                'principal-mismatch',
                "The intent's onBehalfOf does not match the principal in your "
                'credentials.',
                f'Refused: this agent is signed in for {identity.principal}, '
                f'not {intent.on_behalf_of}.',
            )
        for rule in self._refusals.values():
            if self._matches(intent, rule.actions, rule.entities, rule.entity_types):
                return self._refuse('refused', rule.message, f'Refused: {rule.message}')

        task.intent = intent
        if self.watch_timeout is not None:
            task.expires_at = now + self.watch_timeout
        return self._question_or_dossier(task)

    def respond(self, task_id: str, identity: Identity, payloads: Payloads) -> Reply:
        """A follow-up message on an open task: an answer or a commit."""
        task = self._tasks[task_id]
        if task.identity.principal != identity.principal:
            # The A2A layer scopes tasks by owner, so this is a bug if hit.
            raise PermissionError('Task belongs to another principal')
        if task.phase == 'question':
            return self._answer(task, payloads)
        if task.phase == 'awaiting-commit':
            return self._commit(task, payloads)
        raise ValueError(f'Task {task_id} is {task.phase}; it takes no messages')

    def refresh(self, task_id: str) -> Reply | None:
        """Listen: recompute an ``awaiting-commit`` dossier (at delivery time).

        Returns the new dossier and an update if the content changed, else
        None. Permissions are evaluated now, so lost access shows up as
        ``removed``, exactly like retirement.
        """
        task = self._tasks.get(task_id)
        if task is None or task.phase != 'awaiting-commit' or task.dossier is None:
            return None
        previous = task.dossier
        dossier = self._fresh_dossier(task)
        if dossier is None:
            return None
        changes = _diff(previous, dossier)
        summary = self._update_summary(changes)
        update = models.Update(
            dossier_version=dossier.version,
            previous_version=previous.version,
            summary=summary,
            changes=changes,
        )
        return Reply(
            phase='awaiting-commit',
            text=f'{summary} Dossier is now version {dossier.version}.',
            dossier=dossier,
            update=update,
        )

    def cancel(self, task_id: str) -> Reply | None:
        """The agent canceled the task: stop watching."""
        task = self._tasks.get(task_id)
        if task is None or not task.open:
            return None
        task.phase = 'canceled'
        return Reply(phase='canceled', text='Canceled: memory stopped watching this task.')

    def due_for_expiry(self, now: datetime | None = None) -> list[str]:
        now = now or self.clock()
        return [
            t.id
            for t in self._tasks.values()
            if t.open and t.expires_at is not None and t.expires_at <= now
        ]

    def expire(self, task_id: str) -> Reply | None:
        """End a watch that ran past ``expiresAt`` without a commit."""
        task = self._tasks.get(task_id)
        if task is None or task_id not in self.due_for_expiry():
            return None
        task.phase = 'expired'
        return Reply(
            phase='expired',
            text='Expired: no commit arrived in time, so memory stopped watching.',
            error=models.Error(
                code='watch-expired',
                message='The watch expired before the agent committed. Negotiate '
                'again before acting.',
            ),
        )

    # ------------------------------------------------------ protocol steps
    def _question_or_dossier(self, task: TaskRecord) -> Reply:
        assert task.intent is not None
        rule = self._next_question(task)
        if rule is not None:
            task.phase = 'question'
            task.pending = rule
            return Reply(
                phase='question',
                text=rule.prompt or rule.text,
                question=models.Question(
                    id=rule.id, text=rule.text, options=list(rule.options) or None
                ),
            )
        task.phase = 'awaiting-commit'
        task.pending = None
        dossier = self._fresh_dossier(task)
        assert task.dossier is not None
        return Reply(
            phase='awaiting-commit',
            text=task.dossier.summary,
            dossier=dossier or task.dossier,
        )

    def _answer(self, task: TaskRecord, payloads: Payloads) -> Reply:
        assert task.pending is not None
        pending = task.pending
        data, problem = self._single_payload(payloads, 'answer')
        if problem is None:
            problem = '; '.join(validation.errors('answer', data)) or None
        if problem is not None:
            return self._stay(
                task, 'invalid-answer', f'Expected an answer to {pending.id}: {problem}'
            )
        answer = models.Answer.model_validate(data)
        if answer.question_id != pending.id:
            return self._stay(
                task,
                'unknown-question',
                f'No open question {answer.question_id!r}; memory is waiting '
                f'for an answer to {pending.id}: {pending.text}',
            )
        task.answers[pending.id] = answer.text
        task.tags |= pending.tags.get(_normalize_answer(answer.text), frozenset())
        return self._question_or_dossier(task)

    def _commit(self, task: TaskRecord, payloads: Payloads) -> Reply:
        assert task.dossier is not None and task.intent is not None
        data, problem = self._single_payload(payloads, 'commit')
        if problem is None:
            problem = '; '.join(validation.errors('commit', data)) or None
        if problem is not None:
            return self._stay(task, 'invalid-commit', f'Not a valid commit: {problem}')
        commit = models.Commit.model_validate(data)

        # Check against memory's view *now*: something may have changed
        # after the last dossier went out and before this commit arrived.
        refreshed = self._fresh_dossier(task)
        current = task.dossier.version
        if refreshed is not None or commit.based_on != current:
            return Reply(
                phase='awaiting-commit',
                text=f'Not recorded: your commit is based on dossier '
                f'{commit.based_on}, but the current version is {current}.',
                dossier=refreshed,
                error=models.Error(
                    code='stale-dossier',
                    message=f'Commit is based on dossier {commit.based_on}; the '
                    f'current version is {current}. Read the current dossier '
                    'and commit again.',
                    current_version=current,
                ),
            )
        unknown = [c.id for c in commit.conflicts or () if c.id not in task.dossier.watching]
        if unknown:
            return self._stay(
                task,
                'invalid-commit',
                f'Conflicts must name items in dossier {current}; unknown: '
                + ', '.join(unknown),
            )
        return self._record_commit(task, commit)

    def _record_commit(self, task: TaskRecord, commit: models.Commit) -> Reply:
        """Record claims (never confirmed), queue conflicts, tell other tasks."""
        assert task.intent is not None
        now = self.clock()
        identity = task.identity
        commit_id = f'cm-{next(self._commits)}'
        source = f'commit:{commit_id}'
        self._sources[source] = SourceRecord(ref=source, kind='agent-commit', at=now)

        recorded: list[models.RecordedFact] = []
        notes: dict[str, str | None] = {}
        entities_changed: set[str] = set()
        for claim in commit.claims:
            entities = _tuple(claim.entities or task.intent.entities)
            record = FactRecord(
                id=self._new_fact_id(),
                statement=claim.statement,
                status='claim',
                source=source,
                entities=entities,
                claimed_by=models.Attribution(
                    agent=identity.agent,
                    on_behalf_of=identity.principal,
                    at=now,
                    commit_id=commit_id,
                ),
                readers=self._claim_readers(identity, entities),
                evidence=tuple(claim.evidence or ()),
            )
            self._store_fact(record)
            recorded.append(models.RecordedFact(fact_id=record.id, version='1'))
            notes[record.id] = f'{identity.principal} reported: {claim.statement}'
            entities_changed |= set(entities)
            for old_id in claim.supersedes or ():
                self.review_queue.append(
                    ReviewItem(
                        'supersede-proposal',
                        task.id,
                        commit_id,
                        old_id,
                        f'Claim {record.id} says it replaces {old_id}.',
                        identity.agent,
                        identity.principal,
                        now,
                    )
                )
        for conflict in commit.conflicts or ():
            self.review_queue.append(
                ReviewItem(
                    'conflict',
                    task.id,
                    commit_id,
                    conflict.id,
                    conflict.explanation,
                    identity.agent,
                    identity.principal,
                    now,
                )
            )

        task.phase = 'committed'  # before notifying: this task stops watching
        receipt = models.Receipt(
            commit_id=commit_id,
            based_on=commit.based_on,
            recorded=recorded,
            conflicts_recorded=len(commit.conflicts or ()),
            at=now,
        )
        self._changed({r.fact_id for r in recorded}, entities_changed, notes=notes)

        claims = 'as a claim' if len(recorded) == 1 else f'as {len(recorded)} claims'
        text = (
            f'Recorded {claims} until a person or a system of record confirms '
            f'{"it" if len(recorded) == 1 else "them"}.'
        )
        if receipt.conflicts_recorded:
            text += f' {receipt.conflicts_recorded} conflict(s) sent for review.'
        return Reply(phase='committed', text=text, receipt=receipt)

    # --------------------------------------------------- dossier building
    def _fresh_dossier(self, task: TaskRecord) -> models.Dossier | None:
        """Recompute the task's dossier. Returns it (and stores it) only if
        its content differs from the current one."""
        facts, precedent, constraints = self._select(task)
        watching = [x.id for x in (*facts, *precedent, *constraints)]
        summary = self.summarize(facts, precedent, constraints)
        content = models.Dossier(
            version='-',
            summary=summary,
            facts=facts,
            precedent=precedent,
            constraints=constraints,
            watching=watching,
            expires_at=task.expires_at,
        )
        if task.dossier is not None and _same_content(task.dossier, content):
            return None
        task.dossier = content.model_copy(update={'version': str(next(self._versions))})
        return task.dossier

    def _select(
        self, task: TaskRecord
    ) -> tuple[list[models.Fact], list[models.Precedent], list[models.Constraint]]:
        assert task.intent is not None
        intent, identity = task.intent, task.identity
        wanted = set(intent.entities)

        facts = [
            f
            for f in self._facts.values()
            if not f.retired
            and not wanted.isdisjoint(f.entities)
            and self._readable(identity, f)
        ]
        # Newest first (undated last), then by id.
        facts.sort(key=lambda f: f.id)
        facts.sort(key=self._timestamp, reverse=True)
        confirmed = {f.id for f in facts if f.status == 'confirmed'}

        precedent: list[tuple[PrecedentRecord, Relevance]] = []
        for p in self._precedent.values():
            if p.retired or not self._readable(identity, p):
                continue
            for rule in p.relevance:
                if self._relevant(rule, intent, task.tags, confirmed):
                    precedent.append((p, rule))
                    break
        precedent.sort(key=lambda pr: pr[0].id)
        precedent.sort(
            key=lambda pr: pr[0].decided_at.timestamp() if pr[0].decided_at else 0.0,
            reverse=True,
        )
        bases = confirmed | {p.id for p, _ in precedent}

        constraints: list[models.Constraint] = []
        for policy in self._policies.values():
            if policy.retired or not self._matches(intent, policy.actions, policy.entities):
                continue
            if policy.basis:
                basis = [b for b in policy.basis if b in bases]  # never a claim
                if not basis:
                    continue
            else:
                assert policy.policy is not None
                basis = [policy.policy]
            constraints.append(
                models.Constraint(
                    id=policy.id,
                    statement=policy.statement,
                    level=policy.level,
                    basis=basis,
                    until=policy.until,
                )
            )
        constraints.sort(key=lambda c: (c.level != 'must', c.id))
        return (
            [self._fact_model(f) for f in facts],
            [self._precedent_model(p, rule) for p, rule in precedent],
            constraints,
        )

    def _fact_model(self, f: FactRecord) -> models.Fact:
        metadata = (
            {'evidence': [e.dump() for e in f.evidence]} if f.evidence else None
        )
        return models.Fact(
            id=f.id,
            version=str(f.revision),
            statement=f.statement,
            status=f.status,
            source=self._sources[f.source].model(),
            confirmed_by=f.confirmed_by,
            claimed_by=f.claimed_by,
            entities=list(f.entities) or None,
            observed_at=f.observed_at,
            supersedes=list(f.supersedes) or None,
            visibility=self._visibility(f.sources, f.readers),
            metadata=metadata,
        )

    def _precedent_model(self, p: PrecedentRecord, rule: Relevance) -> models.Precedent:
        return models.Precedent(
            id=p.id,
            statement=p.statement,
            relevance=rule.text,
            source=self._sources[p.source].model(),
            decided_by=p.decided_by,
            decided_at=p.decided_at,
            entities=list(p.entities) or None,
        )

    def _timestamp(self, f: FactRecord) -> float:
        when = (
            f.observed_at
            or (f.claimed_by.at if f.claimed_by else None)
            or self._sources[f.source].at
        )
        return when.timestamp() if when else 0.0

    # ---------------------------------------------------------- permissions
    def _readable(self, identity: Identity, item: FactRecord | PrecedentRecord) -> bool:
        """The principal can read every source the item derives from."""
        if not all(identity.can_read(self._sources[s].readers) for s in item.sources):
            return False
        return not isinstance(item, FactRecord) or identity.can_read(item.readers)

    def _visibility(
        self, sources: Sequence[str], extra: tuple[str, ...] | None
    ) -> list[str] | None:
        """Informational: refs in every restricted reader set (None if public)."""
        reader_sets = [self._sources[s].readers for s in sources]
        restricted = [r for r in (*reader_sets, extra) if r is not None]
        if not restricted:
            return None
        common = [ref for ref in restricted[0] if all(ref in r for r in restricted[1:])]
        return common or None

    def _claim_readers(self, identity: Identity, entities: Sequence[str]) -> tuple[str, ...]:
        """Conservative: the committing principal, plus whoever may read claims
        about *every* one of the claim's entities that has a reader set."""
        configured = [self._entity_readers[e] for e in entities if e in self._entity_readers]
        shared: list[str] = []
        if configured:
            shared = [ref for ref in configured[0] if all(ref in r for r in configured[1:])]
        return _tuple([identity.principal, *shared])

    # --------------------------------------------------------------- rules
    @staticmethod
    def _matches(
        intent: models.Intent,
        actions: frozenset[str],
        entities: frozenset[str],
        entity_types: frozenset[str] = frozenset(),
    ) -> bool:
        if actions and intent.action not in actions:
            return False
        if entities and entities.isdisjoint(intent.entities):
            return False
        return not entity_types or any(
            entity_type(e) in entity_types for e in intent.entities
        )

    def _relevant(
        self, rule: Relevance, intent: models.Intent, tags: set[str], confirmed: set[str]
    ) -> bool:
        return (
            self._matches(intent, rule.actions, rule.entities)
            and (not rule.facts or not rule.facts.isdisjoint(confirmed))
            and rule.tags <= tags
        )

    def _next_question(self, task: TaskRecord) -> QuestionRule | None:
        assert task.intent is not None
        for rule in self._questions.values():
            if rule.id not in task.answers and self._matches(
                task.intent, rule.actions, rule.entities, rule.entity_types
            ):
                return rule
        return None

    # -------------------------------------------------------------- helpers
    def _single_payload(
        self, payloads: Payloads, expected: str
    ) -> tuple[Any, str | None]:
        """Exactly one Mem2A payload per agent message, of the expected kind."""
        kinds = [MEDIA_TYPES.get(media_type) for media_type, _ in payloads]
        if len(payloads) != 1:
            return None, f'expected exactly one Mem2A payload, got {len(payloads)}'
        kind = kinds[0]
        if kind != expected:
            got = kind if kind in AGENT_PAYLOADS else payloads[0][0]
            return None, f'expected a {expected}, got {got}'
        return payloads[0][1], None

    def _refuse(self, code: ErrorCode, message: str, text: str) -> Reply:
        return Reply(
            phase='refused', text=text, error=models.Error(code=code, message=message)
        )

    def _stay(self, task: TaskRecord, code: ErrorCode, message: str) -> Reply:
        """Reject a follow-up without changing the task's phase."""
        text = f'Not accepted: {message}'
        return Reply(
            phase=task.phase,
            text=_clip(text, 4096),
            error=models.Error(code=code, message=_clip(message, 2048)),
        )

    def _require_sources(self, refs: Iterable[str]) -> None:
        missing = [r for r in refs if r not in self._sources]
        if missing:
            raise KeyError(f'Unknown source(s): {", ".join(missing)}; add_source first')

    def _store_fact(self, record: FactRecord) -> None:
        self._facts[record.id] = record
        match = re.fullmatch(r'f-(\d+)', record.id)
        if match:
            self._next_fact = max(self._next_fact, int(match.group(1)) + 1)

    def _new_fact_id(self) -> str:
        while f'f-{self._next_fact}' in self._facts:
            self._next_fact += 1
        return f'f-{self._next_fact}'

    def _entities_of(self, item_id: str) -> tuple[str, ...]:
        if item_id in self._facts:
            return self._facts[item_id].entities
        if item_id in self._precedent:
            return self._precedent[item_id].entities
        return ()

    def _update_summary(self, changes: Sequence[models.Change]) -> str:
        """Notes people attached to what the principal can now see; otherwise
        a neutral count that does not say why things were removed."""
        notes = [
            self._notes[c.id]
            for c in changes
            if c.change != 'removed' and c.id in self._notes
        ]
        if notes:
            return _clip(' '.join(dict.fromkeys(notes)), 2048)
        counts: dict[tuple[str, str], int] = {}
        for c in changes:
            counts[(c.kind, c.change)] = counts.get((c.kind, c.change), 0) + 1
        parts = [
            f'{n} {kind}{"" if n == 1 else "s"} {change}'
            for (kind, change), n in counts.items()
        ]
        return 'Dossier changed: ' + ', '.join(parts) + '.'

    def _changed(
        self,
        ids: set[str],
        entities: set[str],
        *,
        notes: Mapping[str, str | None] | None = None,
        broad: bool = False,
    ) -> None:
        """Record notes and tell listeners which open tasks may be affected."""
        for item_id, note in (notes or {}).items():
            if note:
                self._notes[item_id] = note
            else:
                self._notes.pop(item_id, None)
        affected = [
            t.id
            for t in self._tasks.values()
            if t.phase == 'awaiting-commit'
            and t.dossier is not None
            and t.intent is not None
            and (
                broad
                or not ids.isdisjoint(t.dossier.watching)
                or not entities.isdisjoint(t.intent.entities)
            )
        ]
        if not affected:
            return
        for listener in list(self._listeners):
            try:
                listener(affected)
            except Exception:
                logger.exception('Change listener failed')


# ================================================================ diffing
def _same_content(a: models.Dossier, b: models.Dossier) -> bool:
    exclude = {'version'}
    return a.model_dump(exclude=exclude) == b.model_dump(exclude=exclude)


def _diff(old: models.Dossier, new: models.Dossier) -> list[models.Change]:
    """Changes by kind (facts, constraints, precedent), then added, updated,
    removed, in dossier order."""
    changes: list[models.Change] = []
    groups: tuple[tuple[Literal['fact', 'precedent', 'constraint'], str], ...] = (
        ('fact', 'facts'),
        ('constraint', 'constraints'),
        ('precedent', 'precedent'),
    )
    for kind, attr in groups:
        before = {item.id: item for item in getattr(old, attr)}
        after = {item.id: item for item in getattr(new, attr)}
        for item_id, item in after.items():
            if item_id not in before:
                changes.append(models.Change(id=item_id, kind=kind, change='added'))
        for item_id, item in after.items():
            if item_id in before and before[item_id] != item:
                changes.append(models.Change(id=item_id, kind=kind, change='updated'))
        for item_id in before:
            if item_id not in after:
                changes.append(models.Change(id=item_id, kind=kind, change='removed'))
    return changes
