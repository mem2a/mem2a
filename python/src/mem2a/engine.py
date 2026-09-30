# SPDX-License-Identifier: Apache-2.0
"""The memory itself: a transport-agnostic, in-memory Mem2A engine.

The engine knows nothing about A2A. The server (`mem2a.server`) calls
`MemoryEngine.negotiate`, `respond`, `refresh`, `cancel` and `expire`, and
turns each returned `Reply` into A2A artifacts and one status message.

How this reference memory decides what goes into a dossier:

* **Facts** are included when they are not retired, their entities intersect
  the intent's entities, and the principal may read *every* source the fact
  derives from ("permissions follow the source").
* **Claims** (facts recorded from commits) follow the same rules. In
  addition, only the committing principal and principals who may read every
  entity the claim names can see it. Entity reader sets are configured with
  `set_entity_readers`; an entity with none configured is readable by nobody
  but the claimant.
* **Precedent** is included when it is not retired, the principal may read
  every source, and one of its `Relevance` rules matches (by action,
  entities, answers given, or a confirmed fact in the same dossier). The
  matching rule's text becomes the precedent's ``relevance``.
* **Constraints** come only from policies people configured (`add_policy`).
  A policy applies when one of its basis items is a *confirmed* fact or a
  precedent in the same dossier (so the principal can see its basis), or, for
  a standing policy, when the intent matches its filters. Claims never
  produce constraints, so an agent cannot instruct other agents by writing to
  memory.
* ``watching`` lists every fact, precedent and constraint id in the dossier.

Listening: every change (a fact added, updated, retired, superseded or
confirmed; a reader set changed; claims recorded) tells the change listeners
which ``awaiting-commit`` tasks may be affected: those whose intent entities
intersect the change, or whose dossier watches a changed id. The server then
calls `refresh` for each at delivery time, which recomputes the dossier with
current permissions and returns an update only if the content changed.

Versions are opaque strings. Dossier versions come from one memory-wide
counter; a fact's version counts its revisions.

The engine is not thread-safe: use it from the server's event loop.
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypeVar

from mem2a import models
from mem2a.auth import Identity
from mem2a.constants import MEDIA_TYPES, ErrorCode, Phase
from mem2a.models import Level, SourceKind
from mem2a.validation import SchemaValidationError


logger = logging.getLogger(__name__)

#: The Mem2A parts of one incoming agent message, as (mediaType, data) pairs.
Payloads = Sequence[tuple[str, Any]]
#: Called with the ids of open tasks whose dossier may have changed.
ChangeListener = Callable[[list[str]], None]
#: Writes the dossier's one-line brief from what the principal may see.
Summarizer = Callable[
    [Sequence[models.Fact], Sequence[models.Precedent], Sequence[models.Constraint]], str
]

P = TypeVar('P', bound=models.Payload)


def utcnow() -> datetime:
    """The current time in UTC, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ordered(values: Iterable[str] | None) -> tuple[str, ...]:
    """Ordered, de-duplicated tuple."""
    return tuple(dict.fromkeys(values or ()))


def _intersection(sets: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    """Members of every set, in the first set's order."""
    if not sets:
        return ()
    return tuple(ref for ref in sets[0] if all(ref in other for other in sets[1:]))


def _normalize_answer(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _plural(kind: str, n: int) -> str:
    return kind if n == 1 or kind == 'precedent' else f'{kind}s'


def _entity_type(entity: str) -> str:
    """``account:acme`` -> ``account``."""
    return entity.partition(':')[0] if ':' in entity else ''


# ================================================================ records
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
    #: Every source the fact derives from; the first one is shown as its source.
    sources: tuple[str, ...]
    entities: tuple[str, ...]
    confirmed_by: str | None = None
    claimed_by: models.Attribution | None = None
    #: For facts recorded from a commit: the committing principal.
    claimant: str | None = None
    observed_at: datetime | None = None
    supersedes: tuple[str, ...] = ()
    evidence: tuple[models.Evidence, ...] = ()
    revision: int = 1
    retired: bool = False
    superseded_by: str | None = None


@dataclass(frozen=True)
class Relevance:
    """When a precedent bears on an intent, and why.

    Every filter that is set must match: the intent's action, one of its
    entities, a *confirmed* fact in the same dossier, and all answer ``tags``
    the task collected. ``text`` becomes the precedent's ``relevance``.
    """

    text: str
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    facts: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # Accept any iterable of strings for the filters.
        for name in ('actions', 'entities', 'facts', 'tags'):
            object.__setattr__(self, name, frozenset(getattr(self, name)))


@dataclass
class PrecedentRecord:
    id: str
    statement: str
    sources: tuple[str, ...]
    relevance: tuple[Relevance, ...]
    decided_by: str | None = None
    decided_at: datetime | None = None
    entities: tuple[str, ...] = ()
    retired: bool = False


@dataclass
class PolicyRecord:
    """A rule people configured. It produces the constraint with the same id."""

    id: str
    statement: str
    level: Level
    #: Fact or precedent ids; the constraint applies while any one of them is
    #: a confirmed fact or a precedent in the dossier.
    basis: tuple[str, ...] = ()
    #: A standing policy reference, used as the basis when `basis` is empty.
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
    #: Text part for people; defaults to the question text.
    prompt: str | None = None

    def model(self) -> models.Question:
        return models.Question(id=self.id, text=self.text, options=list(self.options) or None)


@dataclass
class RefusalRule:
    """Intents memory declines to prepare a dossier for (code ``refused``)."""

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
    """Memory's view of one Mem2A task."""

    id: str
    context_id: str
    identity: Identity
    created_at: datetime
    phase: Phase = 'refused'
    intent: models.Intent | None = None
    expires_at: datetime | None = None
    #: The open question while the task is in phase ``question``.
    question: QuestionRule | None = None
    answers: dict[str, str] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)
    #: The dossier as last produced (what the agent sees, or is about to).
    dossier: models.Dossier | None = None

    @property
    def open(self) -> bool:
        return self.phase in ('question', 'awaiting-commit')


@dataclass(frozen=True)
class Reply:
    """What memory says on a task.

    The server emits `dossier` and `receipt` as artifacts first, then one
    status message: `text`, then whichever of `question`, `update` and
    `error` are set, marked with `phase`.
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
        more = f' ({len(musts) - 1} more rule(s) apply.)' if len(musts) > 1 else ''
        text = f'Hold: {musts[0].statement}{more}'
    elif shoulds:
        text = 'Go ahead, with care: ' + ' '.join(c.statement for c in shoulds)
    elif facts:
        latest = facts[0]
        label = 'Latest (unconfirmed claim)' if latest.status == 'claim' else 'Latest'
        text = f'Nothing here blocks this. {label}: {latest.statement}'
    elif precedent:
        text = f'Nothing here blocks this. See precedent: {precedent[0].statement}'
    else:
        text = 'Memory has nothing on this yet.'
    return _clip(text, 2048)


# ================================================================== engine
class MemoryEngine:
    """An in-memory company memory that speaks Mem2A v0.1.

    Args:
        watch_timeout: How long a task may stay open without a commit
            (``None``: forever). Advertised as ``watchTimeoutSeconds``.
        clock: Returns the current time (inject a fake one in tests).
        summarize: Writes each dossier's ``summary``.
        first_version: The first dossier version the memory-wide counter hands
            out.
    """

    def __init__(
        self,
        *,
        watch_timeout: timedelta | None = timedelta(days=7),
        clock: Callable[[], datetime] = utcnow,
        summarize: Summarizer = default_summary,
        first_version: int = 1,
    ) -> None:
        self.watch_timeout = watch_timeout
        self.clock = clock
        self.summarize = summarize
        #: Conflicts and supersede proposals from commits, for people to review.
        self.review_queue: list[ReviewItem] = []
        self._sources: dict[str, SourceRecord] = {}
        self._facts: dict[str, FactRecord] = {}
        self._precedent: dict[str, PrecedentRecord] = {}
        self._policies: dict[str, PolicyRecord] = {}
        self._questions: dict[str, QuestionRule] = {}
        self._refusals: dict[str, RefusalRule] = {}
        self._action_readers: dict[str, tuple[str, ...]] = {}
        self._entity_readers: dict[str, tuple[str, ...]] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._notes: dict[str, str] = {}
        self._listeners: list[ChangeListener] = []
        self._versions = itertools.count(first_version)
        self._commits = itertools.count(1)
        self._next_fact = 1

    # ------------------------------------------------------------ listeners
    def on_change(self, listener: ChangeListener) -> Callable[[], None]:
        """Call `listener` with affected task ids after every change.

        Returns a function that removes the listener.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # ------------------------------------------------ content: permissions
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
        """Register a source. `readers` None means everyone may read it."""
        record = SourceRecord(
            ref, kind, title, url, at, None if readers is None else _ordered(readers)
        )
        self._sources[ref] = record
        return record

    def set_source_readers(self, ref: str, readers: Iterable[str] | None) -> None:
        """Change who may read a source. Affected dossiers are re-checked."""
        self._sources[ref].readers = None if readers is None else _ordered(readers)
        items: list[FactRecord | PrecedentRecord] = [
            *(f for f in self._facts.values() if ref in f.sources),
            *(p for p in self._precedent.values() if ref in p.sources),
        ]
        for item in items:
            self._notes.pop(item.id, None)  # never explain an access change
        self._changed({item.id for item in items}, {e for item in items for e in item.entities})

    def set_entity_readers(self, entity: str, readers: Iterable[str] | None) -> None:
        """Who, besides the claimant, may see claims naming `entity`.

        Applies to existing claims too; affected dossiers are re-checked.
        """
        if readers is None:
            self._entity_readers.pop(entity, None)
        else:
            self._entity_readers[entity] = _ordered(readers)
        ids = {f.id for f in self._facts.values() if f.claimant and entity in f.entities}
        for item_id in ids:
            self._notes.pop(item_id, None)
        self._changed(ids, {entity})

    def restrict_action(self, action: str, to: Iterable[str] | None) -> None:
        """Only principals in `to` (principals or groups) may negotiate `action`.

        Others are refused with ``not-authorized``. None lifts the restriction.
        """
        if to is None:
            self._action_readers.pop(action, None)
        else:
            self._action_readers[action] = _ordered(to)

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
        """Add a confirmed fact, retiring the facts it `supersedes`.

        Register its sources (`source` and `derived_from`) first. `note` is a
        plain sentence used as the update summary for principals who can see
        the new fact, for example ``'Legal cleared Acme pricing.'``.
        """
        if fact_id in self._facts:
            raise ValueError(f'Fact {fact_id} already exists')
        record = FactRecord(
            id=fact_id,
            statement=statement,
            status='confirmed',
            sources=_ordered([source, *derived_from]),
            entities=_ordered(entities),
            confirmed_by=confirmed_by,
            observed_at=observed_at,
            supersedes=_ordered(supersedes),
        )
        self._require_sources(record.sources)
        changed_entities = set(record.entities)
        for old_id in record.supersedes:
            old = self._facts[old_id]
            old.retired = True
            old.superseded_by = fact_id
            self._notes.pop(old_id, None)
            changed_entities |= set(old.entities)
        self._store_fact(record)
        self._changed({fact_id, *record.supersedes}, changed_entities, notes={fact_id: note})
        return record

    def update_fact(
        self,
        fact_id: str,
        *,
        statement: str | None = None,
        entities: Iterable[str] | None = None,
        note: str | None = None,
    ) -> FactRecord:
        """Revise a fact; its version goes up by one."""
        record = self._facts[fact_id]
        before = set(record.entities)
        if statement is not None:
            record.statement = statement
        if entities is not None:
            record.entities = _ordered(entities)
        record.revision += 1
        self._changed({fact_id}, before | set(record.entities), notes={fact_id: note})
        return record

    def retire_fact(self, fact_id: str) -> None:
        """The fact no longer holds. Dossiers show it as removed."""
        record = self._facts[fact_id]
        record.retired = True
        self._notes.pop(fact_id, None)
        self._changed({fact_id}, set(record.entities))

    def confirm_fact(self, fact_id: str, *, by: str, note: str | None = None) -> FactRecord:
        """A person or system of record stands behind a claim.

        Confirmation is outside the protocol (spec 9.3). The schema forbids
        ``claimedBy`` on a confirmed fact, so the attribution is dropped; the
        source (``commit:<id>``) still says where the fact came from, and its
        visibility does not change.
        """
        record = self._facts[fact_id]
        if record.status == 'confirmed':
            return record
        record.status = 'confirmed'
        record.confirmed_by = by
        record.claimed_by = None
        record.revision += 1
        note = note or f'{by} confirmed: {record.statement}'
        self._changed({fact_id}, set(record.entities), notes={fact_id: note})
        return record

    # -------------------------------------- content: precedent and rules
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
        """Add a precedent, cited when one of its `relevance` rules matches."""
        record = PrecedentRecord(
            id=precedent_id,
            statement=statement,
            sources=_ordered([source, *derived_from]),
            relevance=tuple(relevance),
            decided_by=decided_by,
            decided_at=decided_at,
            entities=_ordered(entities),
        )
        if not record.relevance:
            raise ValueError('A precedent needs at least one Relevance rule')
        self._require_sources(record.sources)
        self._precedent[precedent_id] = record
        self._changed({precedent_id}, set(record.entities), everyone=True)
        return record

    def retire_precedent(self, precedent_id: str) -> None:
        self._precedent[precedent_id].retired = True
        self._changed({precedent_id}, set(), everyone=True)

    def add_policy(
        self,
        constraint_id: str,
        statement: str,
        *,
        level: Level,
        basis: Iterable[str] = (),
        policy: str | None = None,
        actions: Iterable[str] = (),
        entities: Iterable[str] = (),
        until: str | None = None,
    ) -> PolicyRecord:
        """Configure a constraint. Give exactly one of `basis` or `policy`.

        * `basis`: fact or precedent ids. The constraint applies while one of
          them is a confirmed fact or a precedent in the dossier.
        * `policy`: a standing policy reference, such as
          ``policy:leadership-update-format``, that applies whenever the
          `actions` / `entities` filters match the intent.
        """
        record = PolicyRecord(
            id=constraint_id,
            statement=statement,
            level=level,
            basis=_ordered(basis),
            policy=policy,
            actions=frozenset(actions),
            entities=frozenset(entities),
            until=until,
        )
        if bool(record.basis) == bool(record.policy):
            raise ValueError('Give exactly one of basis= or policy=')
        self._policies[constraint_id] = record
        self._changed({constraint_id, *record.basis}, set(record.entities), everyone=True)
        return record

    def retire_policy(self, constraint_id: str) -> None:
        self._policies[constraint_id].retired = True
        self._changed({constraint_id}, set(), everyone=True)

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
        """Ask `text` before preparing a dossier for matching intents.

        `tags` maps answers (compared ignoring case and punctuation) to tags
        that `Relevance` rules can require.
        """
        rule = QuestionRule(
            id=question_id,
            text=text,
            options=_ordered(options),
            actions=frozenset(actions),
            entities=frozenset(entities),
            entity_types=frozenset(entity_types),
            tags={_normalize_answer(k): frozenset(v) for k, v in (tags or {}).items()},
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
        """Refuse matching intents (code ``refused``). `message` is shown as is."""
        rule = RefusalRule(
            rule_id, message, frozenset(actions), frozenset(entities), frozenset(entity_types)
        )
        self._refusals[rule_id] = rule
        return rule

    # ------------------------------------------------------------ inspection
    def task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def fact(self, fact_id: str) -> FactRecord | None:
        return self._facts.get(fact_id)

    def claims(self) -> list[FactRecord]:
        """Every fact recorded from a commit, confirmed since or not."""
        return [f for f in self._facts.values() if f.claimant is not None]

    def open_tasks(self) -> list[TaskRecord]:
        return [t for t in self._tasks.values() if t.open]

    # ========================================================= the protocol
    def negotiate(
        self, task_id: str, context_id: str, identity: Identity, payloads: Payloads
    ) -> Reply:
        """The first message of a task: an intent (spec 8.1)."""
        now = self.clock()
        task = TaskRecord(id=task_id, context_id=context_id, identity=identity, created_at=now)
        self._tasks[task_id] = task

        intent, problem = _read_payload(payloads, models.Intent)
        if intent is None:
            return self._refuse(
                task,
                'invalid-intent',
                f'The first message must carry one valid intent: {problem}',
                'Refused: the intent is not valid.',
            )
        if intent.on_behalf_of != identity.principal:
            return self._refuse(
                task,
                'principal-mismatch',
                "The intent's onBehalfOf does not match the principal in your credentials.",
                f'Refused: this agent is signed in for {identity.principal}, '
                f'not {intent.on_behalf_of}.',
            )
        allowed = self._action_readers.get(intent.action)
        if allowed is not None and not identity.can_read(allowed):
            return self._refuse(
                task,
                'not-authorized',
                f'{identity.principal} may not use memory for {intent.action!r}.',
                f'Refused: {identity.principal} may not use memory for this kind of action.',
            )
        for rule in self._refusals.values():
            if _matches(intent, rule.actions, rule.entities, rule.entity_types):
                return self._refuse(task, 'refused', rule.message, f'Refused: {rule.message}')

        task.intent = intent
        if self.watch_timeout is not None:
            task.expires_at = now + self.watch_timeout
        return self._advance(task)

    def respond(self, task_id: str, identity: Identity, payloads: Payloads) -> Reply:
        """A follow-up message on an open task: an answer or a commit."""
        task = self._tasks[task_id]
        if (task.identity.agent, task.identity.principal) != (identity.agent, identity.principal):
            # The A2A layer scopes tasks by owner, so reaching this is a bug.
            raise PermissionError(f'Task {task_id} belongs to another caller')
        if task.phase == 'question':
            return self._answer(task, payloads)
        if task.phase == 'awaiting-commit':
            return self._commit(task, payloads)
        raise ValueError(f'Task {task_id} is {task.phase}; it takes no more messages')

    def refresh(self, task_id: str) -> Reply | None:
        """Listen (spec 8.3): recompute an ``awaiting-commit`` dossier now.

        Returns the new dossier and an update if its content changed, else
        None. Access is checked at this moment, so an item the principal can
        no longer see shows up as ``removed``, exactly like a retired one.
        """
        task = self._tasks.get(task_id)
        if task is None or task.phase != 'awaiting-commit':
            return None
        update = self._revise(task)
        if update is None:
            return None
        return Reply(
            phase='awaiting-commit',
            text=f'{update.summary} Dossier is now version {update.dossier_version}.',
            dossier=task.dossier,
            update=update,
        )

    def cancel(self, task_id: str) -> Reply | None:
        """The agent canceled the task: stop watching, record nothing."""
        task = self._tasks.get(task_id)
        if task is None or not task.open:
            return None
        task.phase = 'canceled'
        return Reply(phase='canceled', text='Canceled: memory stopped watching this task.')

    def due_for_expiry(self) -> list[str]:
        """Open tasks whose watch ran past ``expiresAt``."""
        now = self.clock()
        return [t.id for t in self._tasks.values() if _overdue(t, now)]

    def expire(self, task_id: str) -> Reply | None:
        """End a watch that ran past ``expiresAt`` without a commit."""
        task = self._tasks.get(task_id)
        if task is None or not _overdue(task, self.clock()):
            return None
        task.phase = 'expired'
        return Reply(
            phase='expired',
            text='Expired: no commit arrived in time, so memory stopped watching.',
            error=models.Error(
                code='watch-expired',
                message='The watch expired before the agent committed. Negotiate again '
                'before acting.',
            ),
        )

    # ------------------------------------------------------ protocol steps
    def _advance(self, task: TaskRecord) -> Reply:
        """Ask the next question, or deliver the first dossier."""
        rule = self._next_question(task)
        if rule is not None:
            task.phase = 'question'
            task.question = rule
            return Reply(phase='question', text=rule.prompt or rule.text, question=rule.model())
        task.phase = 'awaiting-commit'
        task.question = None
        if self.watch_timeout is not None:
            # Spec 8.3.9: the watch runs from the first dossier.
            task.expires_at = self.clock() + self.watch_timeout
        facts, precedent, constraints = self._compose(task)
        task.dossier = self._new_dossier(task, facts, precedent, constraints)
        return Reply(phase='awaiting-commit', text=task.dossier.summary, dossier=task.dossier)

    def _answer(self, task: TaskRecord, payloads: Payloads) -> Reply:
        question = task.question
        assert question is not None
        answer, problem = _read_payload(payloads, models.Answer)
        if answer is None:
            return self._still_asking(
                task, 'invalid-answer', f'Expected an answer to {question.id}: {problem}'
            )
        if answer.question_id != question.id:
            return self._still_asking(
                task,
                'unknown-question',
                f'{answer.question_id!r} is not the open question. '
                f'Answer {question.id}: {question.text}',
            )
        task.answers[question.id] = answer.text
        task.tags |= question.tags.get(_normalize_answer(answer.text), frozenset())
        return self._advance(task)

    def _commit(self, task: TaskRecord, payloads: Payloads) -> Reply:
        assert task.dossier is not None
        commit, problem = _read_payload(payloads, models.Commit)
        if commit is None:
            message = f'Not a valid commit: {problem}'
            return Reply(
                phase='awaiting-commit',
                text=_clip(f'Not recorded. {message}', 4096),
                error=models.Error(code='invalid-commit', message=_clip(message, 2048)),
            )
        # Compare against memory's view now, not only the last dossier sent:
        # an update may still be on its way to the agent.
        update = self._revise(task)
        current = task.dossier.version
        if update is not None or commit.based_on != current:
            text = (
                f'Not recorded: your commit is based on dossier {commit.based_on}, '
                f'but the current version is {current}.'
            )
            if update is not None:
                text += f' {update.summary}'
            return Reply(
                phase='awaiting-commit',
                text=text,
                dossier=task.dossier if update is not None else None,
                update=update,
                error=models.Error(
                    code='stale-dossier',
                    message=f'Commit is based on dossier {commit.based_on}; the current '
                    f'version is {current}. Read the current dossier and commit again.',
                    current_version=current,
                ),
            )
        return self._record(task, commit)

    def _record(self, task: TaskRecord, commit: models.Commit) -> Reply:
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
            record = FactRecord(
                id=self._new_fact_id(),
                statement=claim.statement,
                status='claim',
                sources=(source,),
                entities=_ordered(claim.entities or task.intent.entities),
                claimed_by=models.Attribution(
                    agent=identity.agent,
                    on_behalf_of=identity.principal,
                    at=now,
                    commit_id=commit_id,
                ),
                claimant=identity.principal,
                evidence=tuple(claim.evidence or ()),
            )
            self._store_fact(record)
            recorded.append(models.RecordedFact(fact_id=record.id, version=str(record.revision)))
            notes[record.id] = f'{identity.principal} reported: {claim.statement}'
            entities_changed |= set(record.entities)
            for old_id in claim.supersedes or ():
                self.review_queue.append(
                    ReviewItem(
                        kind='supersede-proposal',
                        task_id=task.id,
                        commit_id=commit_id,
                        item_id=old_id,
                        explanation=f'Claim {record.id} says it replaces {old_id}.',
                        agent=identity.agent,
                        principal=identity.principal,
                        at=now,
                    )
                )
        for conflict in commit.conflicts or ():
            self.review_queue.append(
                ReviewItem(
                    kind='conflict',
                    task_id=task.id,
                    commit_id=commit_id,
                    item_id=conflict.id,
                    explanation=conflict.explanation,
                    agent=identity.agent,
                    principal=identity.principal,
                    at=now,
                )
            )

        task.phase = 'committed'  # before notifying: this task stops watching
        self._changed({r.fact_id for r in recorded}, entities_changed, notes=notes)

        conflicts = len(commit.conflicts or ())
        text = (
            'Recorded as a claim until a person or a system of record confirms it.'
            if len(recorded) == 1
            else f'Recorded as {len(recorded)} claims until a person or a system of '
            'record confirms them.'
        )
        if conflicts:
            text += f' {conflicts} conflict(s) sent to a person for review.'
        receipt = models.Receipt(
            commit_id=commit_id,
            based_on=commit.based_on,
            recorded=recorded,
            conflicts_recorded=conflicts,
            at=now,
        )
        return Reply(phase='committed', text=text, receipt=receipt)

    def _refuse(self, task: TaskRecord, code: ErrorCode, message: str, text: str) -> Reply:
        task.phase = 'refused'
        return Reply(
            phase='refused',
            text=_clip(text, 4096),
            error=models.Error(code=code, message=_clip(message, 2048)),
        )

    def _still_asking(self, task: TaskRecord, code: ErrorCode, message: str) -> Reply:
        """Reject an answer; the task stays in phase ``question``, asking again."""
        assert task.question is not None
        return Reply(
            phase='question',
            text=_clip(f'Not accepted: {message}', 4096),
            question=task.question.model(),
            error=models.Error(code=code, message=_clip(message, 2048)),
        )

    # --------------------------------------------------- dossier building
    def _revise(self, task: TaskRecord) -> models.Update | None:
        """Recompute the task's dossier. If its items changed, store a new
        version and return the update describing the change."""
        previous = task.dossier
        assert previous is not None
        facts, precedent, constraints = self._compose(task)
        if (facts, precedent, constraints) == (
            previous.facts,
            previous.precedent,
            previous.constraints,
        ):
            return None
        task.dossier = self._new_dossier(task, facts, precedent, constraints)
        changes = _diff(previous, task.dossier)
        return models.Update(
            dossier_version=task.dossier.version,
            previous_version=previous.version,
            summary=self._update_summary(changes),
            changes=changes,
        )

    def _new_dossier(
        self,
        task: TaskRecord,
        facts: list[models.Fact],
        precedent: list[models.Precedent],
        constraints: list[models.Constraint],
    ) -> models.Dossier:
        return models.Dossier(
            version=str(next(self._versions)),
            summary=self.summarize(facts, precedent, constraints),
            facts=facts,
            precedent=precedent,
            constraints=constraints,
            watching=[
                *(f.id for f in facts),
                *(p.id for p in precedent),
                *(c.id for c in constraints),
            ],
            expires_at=task.expires_at,
        )

    def _compose(
        self, task: TaskRecord
    ) -> tuple[list[models.Fact], list[models.Precedent], list[models.Constraint]]:
        """Select what the task's principal may see and needs to know now."""
        intent, identity = task.intent, task.identity
        assert intent is not None
        wanted = set(intent.entities)

        facts = [
            f
            for f in self._facts.values()
            if not f.retired and not wanted.isdisjoint(f.entities) and self._visible(identity, f)
        ]
        facts.sort(key=lambda f: (-self._when(f), f.id))  # newest first, undated last
        # Relevance and constraints use only what this principal can see
        # (spec 11.5), and never claims (spec 9.4).
        confirmed = {f.id for f in facts if f.status == 'confirmed'}

        precedent: list[tuple[PrecedentRecord, Relevance]] = []
        for p in self._precedent.values():
            if p.retired or not self._visible(identity, p):
                continue
            rule = next((r for r in p.relevance if _relevant(r, task, confirmed)), None)
            if rule is not None:
                precedent.append((p, rule))
        precedent.sort(key=lambda pr: (-_timestamp(pr[0].decided_at), pr[0].id))

        bases = confirmed | {p.id for p, _ in precedent}
        constraints: list[models.Constraint] = []
        for policy in self._policies.values():
            if policy.retired or not _matches(intent, policy.actions, policy.entities):
                continue
            if policy.basis:
                basis = [b for b in policy.basis if b in bases]
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
        return models.Fact(
            id=f.id,
            version=str(f.revision),
            statement=f.statement,
            status=f.status,
            source=self._sources[f.sources[0]].model(),
            confirmed_by=f.confirmed_by,
            claimed_by=f.claimed_by,
            entities=list(f.entities) or None,
            observed_at=f.observed_at,
            supersedes=list(f.supersedes) or None,
            visibility=self._visibility(f),
            metadata={'evidence': [e.dump() for e in f.evidence]} if f.evidence else None,
        )

    def _precedent_model(self, p: PrecedentRecord, rule: Relevance) -> models.Precedent:
        return models.Precedent(
            id=p.id,
            statement=p.statement,
            relevance=rule.text,
            source=self._sources[p.sources[0]].model(),
            decided_by=p.decided_by,
            decided_at=p.decided_at,
            entities=list(p.entities) or None,
        )

    def _when(self, f: FactRecord) -> float:
        claimed_at = f.claimed_by.at if f.claimed_by else None
        return _timestamp(f.observed_at or claimed_at or self._sources[f.sources[0]].at)

    # ---------------------------------------------------------- permissions
    def _visible(self, identity: Identity, item: FactRecord | PrecedentRecord) -> bool:
        """Spec 10.3 and 10.5: the principal may read every source, and, for
        a claim, is its claimant or may read every entity it names."""
        if not all(identity.can_read(self._sources[s].readers) for s in item.sources):
            return False
        if isinstance(item, FactRecord) and item.claimant not in (None, identity.principal):
            return all(identity.can_read(self._entity_readers.get(e, ())) for e in item.entities)
        return True

    def _visibility(self, f: FactRecord) -> list[str] | None:
        """Informational: refs in every restricted reader set (None if unrestricted)."""
        restricted = [
            readers for s in f.sources if (readers := self._sources[s].readers) is not None
        ]
        if f.claimant is not None:
            entity_sets = [self._entity_readers.get(e, ()) for e in f.entities]
            restricted.append((f.claimant, *_intersection(entity_sets)))
        if not restricted:
            return None
        return list(_intersection(restricted)) or None

    # --------------------------------------------------------------- rules
    def _next_question(self, task: TaskRecord) -> QuestionRule | None:
        assert task.intent is not None
        for rule in self._questions.values():
            if rule.id not in task.answers and _matches(
                task.intent, rule.actions, rule.entities, rule.entity_types
            ):
                return rule
        return None

    # -------------------------------------------------------------- helpers
    def _require_sources(self, refs: Iterable[str]) -> None:
        missing = [r for r in refs if r not in self._sources]
        if missing:
            raise KeyError(f'Unknown source(s): {", ".join(missing)}; call add_source first')

    def _store_fact(self, record: FactRecord) -> None:
        self._facts[record.id] = record
        match = re.fullmatch(r'f-(\d+)', record.id)
        if match:
            self._next_fact = max(self._next_fact, int(match.group(1)) + 1)

    def _new_fact_id(self) -> str:
        while f'f-{self._next_fact}' in self._facts:
            self._next_fact += 1
        return f'f-{self._next_fact}'

    def _update_summary(self, changes: Sequence[models.Change]) -> str:
        """Notes attached to items the principal can now see; otherwise a
        neutral count that does not say why anything was removed."""
        notes = [
            self._notes[c.id] for c in changes if c.change != 'removed' and c.id in self._notes
        ]
        if notes:
            return _clip(' '.join(dict.fromkeys(notes)), 2048)
        counts: dict[tuple[str, str], int] = {}
        for c in changes:
            counts[(c.kind, c.change)] = counts.get((c.kind, c.change), 0) + 1
        parts = [f'{n} {_plural(kind, n)} {change}' for (kind, change), n in counts.items()]
        return 'Dossier changed: ' + ', '.join(parts) + '.'

    def _changed(
        self,
        ids: set[str],
        entities: set[str],
        *,
        notes: Mapping[str, str | None] | None = None,
        everyone: bool = False,
    ) -> list[str]:
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
                everyone
                or not ids.isdisjoint(t.dossier.watching)
                or not entities.isdisjoint(t.intent.entities)
            )
        ]
        if affected:
            for listener in list(self._listeners):
                try:
                    listener(affected)
                except Exception:
                    logger.exception('Change listener failed')
        return affected


# ============================================================== functions
def _read_payload(payloads: Payloads, model: type[P]) -> tuple[P | None, str | None]:
    """The single Mem2A payload of an agent message, parsed as `model`.

    Returns (payload, None) or (None, what is wrong). Spec 7.1.1: exactly
    one Mem2A payload per agent message.
    """
    if len(payloads) != 1:
        return None, f'expected exactly one Mem2A payload, got {len(payloads)}'
    media_type, data = payloads[0]
    kind = MEDIA_TYPES.get(media_type)
    if kind != model.schema_name:
        return None, f'expected a {model.schema_name}, got {kind or media_type}'
    try:
        return model.parse(data), None
    except SchemaValidationError as error:
        return None, '; '.join(error.errors)


def _matches(
    intent: models.Intent,
    actions: frozenset[str],
    entities: frozenset[str],
    entity_types: frozenset[str] = frozenset(),
) -> bool:
    """Every filter that is set matches the intent."""
    if actions and intent.action not in actions:
        return False
    if entities and entities.isdisjoint(intent.entities):
        return False
    return not entity_types or any(_entity_type(e) in entity_types for e in intent.entities)


def _relevant(rule: Relevance, task: TaskRecord, confirmed: set[str]) -> bool:
    assert task.intent is not None
    return (
        _matches(task.intent, rule.actions, rule.entities)
        and (not rule.facts or not rule.facts.isdisjoint(confirmed))
        and rule.tags <= task.tags
    )


def _overdue(task: TaskRecord, now: datetime) -> bool:
    return task.open and task.expires_at is not None and task.expires_at <= now


def _timestamp(when: datetime | None) -> float:
    return when.timestamp() if when else 0.0


def _diff(old: models.Dossier, new: models.Dossier) -> list[models.Change]:
    """Changes by kind (facts, constraints, precedent); within each kind:
    added, updated, removed, in dossier order."""
    return [
        *_diff_items('fact', old.facts, new.facts),
        *_diff_items('constraint', old.constraints, new.constraints),
        *_diff_items('precedent', old.precedent, new.precedent),
    ]


def _diff_items(
    kind: models.ItemKind,
    old: Sequence[models.Fact | models.Precedent | models.Constraint],
    new: Sequence[models.Fact | models.Precedent | models.Constraint],
) -> list[models.Change]:
    before = {item.id: item for item in old}
    after = {item.id: item for item in new}
    added = [i for i in after if i not in before]
    updated = [i for i, item in after.items() if i in before and before[i] != item]
    removed = [i for i in before if i not in after]
    return [
        *(models.Change(id=i, kind=kind, change='added') for i in added),
        *(models.Change(id=i, kind=kind, change='updated') for i in updated),
        *(models.Change(id=i, kind=kind, change='removed') for i in removed),
    ]
