# SPDX-License-Identifier: Apache-2.0
"""The memory itself: a transport-agnostic, in-memory Mem2A engine.

The engine knows nothing about A2A. The server (`mem2a.server`) calls
`MemoryEngine.negotiate`, `respond`, `refresh`, `cancel`, `expire` and `fail`,
and turns each returned `Reply` into A2A artifacts and one status message.

How this reference memory decides what goes into a dossier:

* **Facts** are included when they are not retired, their entities intersect
  the intent's entities, and the principal may read *every* source the fact
  derives from ("permissions follow the source"). Confirmed facts come
  first, newest first; claims follow.
* **Claims** are facts recorded from commits. Besides the claimant, a
  principal sees a claim only if it may read every entity the claim names
  (`set_entity_readers`) *and* see every item of the dossier the commit was
  based on. So an agent can't launder what it read into a claim that more
  people can see.
* **Precedent** is included when it is not retired, the principal may read
  every source, and one of its `When` conditions matches the intent (action,
  entities, answers given, or a confirmed fact in the same dossier).
* **Constraints** come only from policies people configured (`add_policy`).
  A policy applies when *all* of its basis items are confirmed facts or
  precedent in the same dossier, or, for a standing policy, when the intent
  matches its filters. Claims never produce constraints, so an agent cannot
  instruct other agents by writing to memory.
* ``watching`` lists exactly the ids of the dossier's items.

Every fact, precedent and constraint has a version, bumped whenever it
changes. A dossier gets a new version only when its set of items or any
item's version changes; wording alone never produces an update.

Listening: every change (a fact added, updated, retired, superseded or
confirmed; a reader set changed; claims recorded) tells the change listeners
which ``awaiting-commit`` tasks may be affected. The server then calls
`refresh` for each at delivery time, which recomputes the dossier with
current permissions and returns an update only if the content changed.

Versions are opaque strings. Dossier versions come from one memory-wide
counter. The engine is not thread-safe: use it from one event loop.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mem2a import models
from mem2a.auth import Identity
from mem2a.constants import MEDIA_TYPES, OPEN_PHASES, ErrorCode, PayloadKind, Phase
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
#: Decides whether to record a valid, current commit: returns None to record
#: it, or a reason to refuse it (error ``commit-refused``). The agent sees the
#: reason, so it must not reveal anything the principal can't see.
CommitPolicy = Callable[['TaskRecord', models.Commit], str | None]

# Limits from the schemas (maxItems, maxLength).
MAX_ID = 256
MAX_STATEMENT = 4096
MAX_RULE = 2048
MAX_ENTITIES = 64
MAX_SUPERSEDES = 32
MAX_BASIS = 32
MAX_UNTIL = 1024


def utcnow() -> datetime:
    """The current time in UTC, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ordered(values: Iterable[str] | None) -> tuple[str, ...]:
    """Ordered, de-duplicated tuple."""
    return tuple(dict.fromkeys(values or ()))


def _readers(readers: Iterable[str] | None) -> tuple[str, ...] | None:
    return None if readers is None else _ordered(readers)


def _normalize_answer(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _plural(kind: str, n: int) -> str:
    return kind if n == 1 or kind == 'precedent' else f'{kind}s'


def _entity_type(entity: str) -> str:
    """``account:acme`` -> ``account``."""
    return entity.partition(':')[0] if ':' in entity else ''


def _timestamp(when: datetime | None) -> float:
    return when.timestamp() if when else 0.0


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _check_text(name: str, text: str, limit: int) -> None:
    _check(0 < len(text) <= limit, f'{name} must have 1 to {limit} characters')


def _check_items(name: str, items: Sequence[str], limit: int) -> None:
    _check(len(items) <= limit, f'At most {limit} {name}, got {len(items)}')


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


@dataclass(frozen=True)
class CommitOrigin:
    """How a fact came from a commit: who claimed it, and on what basis."""

    commit_id: str
    agent: str
    principal: str
    at: datetime
    #: Ids of the facts and precedent in the dossier the commit was based on.
    context: tuple[str, ...]


@dataclass
class FactRecord:
    id: str
    statement: str
    status: Literal['confirmed', 'claim']
    #: Every source the fact derives from; the first is shown as its source.
    sources: tuple[str, ...]
    entities: tuple[str, ...]
    confirmed_by: str | None = None
    #: Set for facts recorded from a commit, and kept once they are confirmed.
    origin: CommitOrigin | None = None
    observed_at: datetime | None = None
    supersedes: tuple[str, ...] = ()
    evidence: tuple[models.Evidence, ...] = ()
    revision: int = 1
    retired: bool = False
    retired_note: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, init=False)
class When:
    """A condition under which a precedent bears on an intent.

    Every filter that is set must match: the intent's action, one of its
    entities, a *confirmed* fact in the same dossier, and all answer ``tags``
    the task collected (see `MemoryEngine.add_question`). For example
    ``When(actions={'send_quote'}, facts={'f-311'})``.
    """

    actions: frozenset[str]
    entities: frozenset[str]
    facts: frozenset[str]
    tags: frozenset[str]

    def __init__(
        self,
        *,
        actions: Iterable[str] = (),
        entities: Iterable[str] = (),
        facts: Iterable[str] = (),
        tags: Iterable[str] = (),
    ) -> None:
        object.__setattr__(self, 'actions', frozenset(actions))
        object.__setattr__(self, 'entities', frozenset(entities))
        object.__setattr__(self, 'facts', frozenset(facts))
        object.__setattr__(self, 'tags', frozenset(tags))


@dataclass
class PrecedentRecord:
    id: str
    statement: str
    relevance: str
    sources: tuple[str, ...]
    when: tuple[When, ...]
    decided_by: str | None = None
    decided_at: datetime | None = None
    entities: tuple[str, ...] = ()
    revision: int = 1
    retired: bool = False


@dataclass
class PolicyRecord:
    """A rule people configured. It produces the constraint with the same id."""

    id: str
    statement: str
    level: Level
    #: Fact or precedent ids; the constraint applies while all of them are
    #: confirmed facts or precedent in the dossier.
    basis: tuple[str, ...] = ()
    #: A standing policy reference, used as the basis when `basis` is empty.
    policy: str | None = None
    actions: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()
    until: str | None = None
    revision: int = 1
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

    kind: Literal['conflict', 'replace-proposal']
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
    phase: Phase = 'working'
    intent: models.Intent | None = None
    expires_at: datetime | None = None
    #: The open question while the task is in phase ``question``.
    question: QuestionRule | None = None
    answers: dict[str, str] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)
    #: The current dossier (what the agent has, or is about to get).
    dossier: models.Dossier | None = None
    #: Every dossier this task was given, by version.
    dossiers: dict[str, models.Dossier] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return self.phase in OPEN_PHASES


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
    claims = [f for f in facts if f.status == 'claim']
    if musts:
        more = f' ({len(musts) - 1} more rule(s) apply.)' if len(musts) > 1 else ''
        text = f'Hold: {musts[0].statement}{more}'
    elif shoulds:
        text = 'Go ahead, with care: ' + ' '.join(c.statement for c in shoulds)
    elif claims:
        text = f'Nothing here blocks this. Reported, not yet confirmed: {claims[0].statement}'
    elif facts:
        text = f'Nothing here blocks this. Latest: {facts[0].statement}'
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
            (``None``: forever). Advertised in the card as ``watchTimeout``.
        clock: Returns the current time (inject a fake one in tests).
        summarize: Writes each dossier's ``summary``.
        first_version: The first dossier version the memory-wide counter
            hands out.
        max_open_tasks: How many open tasks one principal may have; more are
            refused with ``limit-exceeded``. None: no limit.
        commit_policy: May refuse valid, current commits (``commit-refused``),
            for example to rate-limit an agent. By default every one is
            recorded.
    """

    def __init__(
        self,
        *,
        watch_timeout: timedelta | None = timedelta(days=7),
        clock: Callable[[], datetime] = utcnow,
        summarize: Summarizer = default_summary,
        first_version: int = 1,
        max_open_tasks: int | None = 100,
        commit_policy: CommitPolicy | None = None,
    ) -> None:
        self.watch_timeout = watch_timeout
        self.clock = clock
        self.summarize = summarize
        self.max_open_tasks = max_open_tasks
        self.commit_policy = commit_policy
        #: Conflicts and replace proposals from commits, for people to review.
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

    # ------------------------------------------------------------ listeners
    def on_change(self, listener: ChangeListener) -> Callable[[], None]:
        """Call `listener` with the affected task ids after every change.

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
        _check(ref not in self._sources, f'Source {ref} already exists')
        _check(
            kind != 'agent-commit' and not ref.startswith('commit:'),
            'agent-commit sources are created by commits, not added by hand',
        )
        _check(url is None or url.startswith('https://'), f'Source URLs must be https: {url}')
        record = SourceRecord(ref, kind, title, url, at, _readers(readers))
        self._sources[ref] = record
        return record

    def set_source_readers(self, ref: str, readers: Iterable[str] | None) -> list[str]:
        """Change who may read a source. Returns the tasks re-checked."""
        self._sources[ref].readers = _readers(readers)
        direct = {item.id for item in self._items() if ref in item.sources}
        return self._access_changed(direct | self._claims_resting_on(direct))

    def set_entity_readers(self, entity: str, readers: Iterable[str] | None) -> list[str]:
        """Who, besides the claimant, may see claims naming `entity`.

        None: nobody else. Applies to existing claims too. Returns the tasks
        re-checked.
        """
        if readers is None:
            self._entity_readers.pop(entity, None)
        else:
            self._entity_readers[entity] = _ordered(readers)
        naming = {f.id for f in self._facts.values() if f.origin and entity in f.entities}
        return self._access_changed(naming | self._claims_resting_on(naming))

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
        version: int = 1,
    ) -> FactRecord:
        """Add a confirmed fact, retiring the confirmed facts it `supersedes`.

        Register its sources (`source` and `derived_from`) first. `note` is a
        plain sentence used as the update summary for principals who can see
        the new fact, for example ``'Legal cleared Acme pricing.'``. `version`
        is the fact's first version (use it when importing existing facts).
        """
        record = FactRecord(
            id=fact_id,
            statement=statement,
            status='confirmed',
            sources=_ordered([source, *derived_from]),
            entities=_ordered(entities),
            confirmed_by=confirmed_by,
            observed_at=observed_at,
            supersedes=_ordered(supersedes),
            revision=version,
        )
        self._check_new_item(fact_id, statement, record.sources, version=version)
        _check_items('entities', record.entities, MAX_ENTITIES)
        _check_items('superseded facts', record.supersedes, MAX_SUPERSEDES)
        for old_id in record.supersedes:
            old = self._facts.get(old_id)
            _check(
                old is not None and old.status == 'confirmed',
                f'{old_id} is not a confirmed fact; only confirmed facts can be superseded',
            )

        changed = set(record.entities)
        for old_id in record.supersedes:
            old = self._facts[old_id]
            old.retired, old.superseded_by = True, fact_id
            self._notes.pop(old_id, None)
            changed |= set(old.entities)
        self._facts[fact_id] = record
        self._changed({fact_id, *record.supersedes}, changed, notes={fact_id: note})
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
        new_entities = record.entities if entities is None else _ordered(entities)
        if statement is not None:
            _check_text('statement', statement, MAX_STATEMENT)
        _check_items('entities', new_entities, MAX_ENTITIES)
        before = set(record.entities)
        record.statement = statement or record.statement
        record.entities = new_entities
        record.revision += 1
        self._changed({fact_id}, before | set(new_entities), notes={fact_id: note})
        return record

    def retire_fact(self, fact_id: str, *, note: str | None = None) -> FactRecord:
        """The fact no longer holds. Dossiers show it as removed.

        `note` is kept for people (and `/dev/state`), never sent to agents:
        an update must not say why an item was removed, so that retirement
        and lost access look the same.
        """
        record = self._facts[fact_id]
        record.retired, record.retired_note = True, note
        self._notes.pop(fact_id, None)
        self._changed({fact_id}, set(record.entities))
        return record

    def confirm_fact(self, fact_id: str, *, by: str, note: str | None = None) -> FactRecord:
        """A person or system of record stands behind a claim.

        Confirmation is outside the protocol (spec 9.3). The fact's version
        goes up, it loses ``claimedBy`` (the schema allows only one of
        ``claimedBy`` and ``confirmedBy``), and it keeps its commit source
        and who may see it.
        """
        record = self._facts[fact_id]
        if record.status == 'confirmed':
            return record
        record.status, record.confirmed_by = 'confirmed', by
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
        relevance: str,
        source: str,
        when: Iterable[When],
        decided_by: str | None = None,
        decided_at: datetime | None = None,
        entities: Iterable[str] = (),
        derived_from: Iterable[str] = (),
        version: int = 1,
    ) -> PrecedentRecord:
        """Add a precedent, cited (with `relevance` as the reason) when one of
        its `when` conditions matches an intent."""
        record = PrecedentRecord(
            id=precedent_id,
            statement=statement,
            relevance=relevance,
            sources=_ordered([source, *derived_from]),
            when=tuple(when),
            decided_by=decided_by,
            decided_at=decided_at,
            entities=_ordered(entities),
            revision=version,
        )
        self._check_new_item(precedent_id, statement, record.sources, version=version)
        _check_text('relevance', relevance, MAX_RULE)
        _check_items('entities', record.entities, MAX_ENTITIES)
        _check(bool(record.when), 'A precedent needs at least one When condition')
        self._precedent[precedent_id] = record
        self._changed({precedent_id}, set(record.entities), everyone=True)
        return record

    def update_precedent(
        self,
        precedent_id: str,
        *,
        statement: str | None = None,
        relevance: str | None = None,
        note: str | None = None,
    ) -> PrecedentRecord:
        """Revise a precedent; its version goes up by one."""
        record = self._precedent[precedent_id]
        if statement is not None:
            _check_text('statement', statement, MAX_STATEMENT)
        if relevance is not None:
            _check_text('relevance', relevance, MAX_RULE)
        record.statement = statement or record.statement
        record.relevance = relevance or record.relevance
        record.revision += 1
        self._changed({precedent_id}, set(record.entities), notes={precedent_id: note})
        return record

    def retire_precedent(self, precedent_id: str) -> None:
        self._precedent[precedent_id].retired = True
        self._notes.pop(precedent_id, None)
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
        version: int = 1,
    ) -> PolicyRecord:
        """Configure a constraint. Give exactly one of `basis` or `policy`.

        * `basis`: fact or precedent ids. The constraint applies while all of
          them are confirmed facts or precedent in the dossier.
        * `policy`: a standing policy reference, such as
          ``policy:leadership-update-format``, that applies whenever the
          `actions` and `entities` filters match the intent.
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
            revision=version,
        )
        self._check_new_item(constraint_id, statement, (), version=version, limit=MAX_RULE)
        _check(
            until is None or 0 < len(until) <= MAX_UNTIL,
            f'until must have 1 to {MAX_UNTIL} characters',
        )
        _check(bool(record.basis) != bool(record.policy), 'Give exactly one of basis= or policy=')
        _check_items('basis items', record.basis, MAX_BASIS)
        self._policies[constraint_id] = record
        self._changed({constraint_id, *record.basis}, set(record.entities), everyone=True)
        return record

    def update_policy(
        self,
        constraint_id: str,
        *,
        statement: str | None = None,
        level: Level | None = None,
        until: str | None = None,
        note: str | None = None,
    ) -> PolicyRecord:
        """Revise a policy; its constraint's version goes up by one."""
        record = self._policies[constraint_id]
        if statement is not None:
            _check_text('statement', statement, MAX_RULE)
        record.statement = statement or record.statement
        record.level = level or record.level
        record.until = until if until is not None else record.until
        record.revision += 1
        self._changed({constraint_id}, set(record.entities), notes={constraint_id: note})
        return record

    def retire_policy(self, constraint_id: str) -> None:
        self._policies[constraint_id].retired = True
        self._notes.pop(constraint_id, None)
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
        that `When` conditions can require.
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

    def facts(self) -> list[FactRecord]:
        """Every fact, claims included, retired or not."""
        return list(self._facts.values())

    def claims(self) -> list[FactRecord]:
        """Every fact recorded from a commit, confirmed since or not."""
        return [f for f in self._facts.values() if f.origin is not None]

    def precedents(self) -> list[PrecedentRecord]:
        return list(self._precedent.values())

    def policies(self) -> list[PolicyRecord]:
        return list(self._policies.values())

    def sources(self) -> list[SourceRecord]:
        return list(self._sources.values())

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

        kind, data = _single(payloads)
        if kind != 'intent':
            reason = _unexpected_count(payloads) or 'A task starts with an intent.'
            return self._refuse(task, 'unexpected-message', reason, f'Refused: {reason}')
        try:
            intent = models.Intent.parse(data)
        except SchemaValidationError as error:
            return self._refuse(
                task,
                'invalid-intent',
                'The intent is not valid: ' + '; '.join(error.errors),
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
        if self.max_open_tasks is not None:
            open_tasks = [
                t for t in self.open_tasks() if t.identity.principal == identity.principal
            ]
            if len(open_tasks) >= self.max_open_tasks:
                return self._refuse(
                    task,
                    'limit-exceeded',
                    f'{identity.principal} already has {len(open_tasks)} open tasks, the most '
                    'memory allows. Commit or cancel some first.',
                    'Refused: too many open tasks. Commit or cancel some first.',
                )
        for rule in self._refusals.values():
            if _matches(intent, rule.actions, rule.entities, rule.entity_types):
                return self._refuse(task, 'refused', rule.message, f'Refused: {rule.message}')

        task.intent = intent
        if self.watch_timeout is not None:
            task.expires_at = now + self.watch_timeout  # questions expire too
        return self._advance(task)

    def respond(self, task_id: str, identity: Identity, payloads: Payloads) -> Reply:
        """A follow-up message on an open task: an answer or a commit."""
        task = self._tasks[task_id]
        if (task.identity.agent, task.identity.principal) != (identity.agent, identity.principal):
            # The A2A layer scopes tasks by owner, so reaching this is a bug.
            raise PermissionError(f'Task {task_id} belongs to another caller')
        if not task.open:
            raise ValueError(f'Task {task_id} is {task.phase}; it takes no more messages')
        kind, data = _single(payloads)
        if task.phase == 'question' and kind == 'answer':
            return self._answer(task, data)
        if task.phase == 'awaiting-commit' and kind == 'commit':
            return self._commit(task, data)
        return self._unexpected(task, _unexpected_count(payloads) or _unexpected_kind(task, kind))

    def refresh(self, task_id: str) -> Reply | None:
        """Listen (spec 8.3): recompute an ``awaiting-commit`` dossier now.

        Returns the new dossier and an update if its items changed, else None.
        Access is checked at this moment, so an item the principal can no
        longer see shows up as ``removed``, exactly like a retired one.
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
        return Reply(
            phase='canceled',
            text='Canceled. Memory stopped watching this task and recorded nothing from it.',
        )

    def due_for_expiry(self) -> list[str]:
        """Open tasks, asking or watching, that ran past ``expiresAt``."""
        now = self.clock()
        return [t.id for t in self._tasks.values() if _overdue(t, now)]

    def expire(self, task_id: str) -> Reply | None:
        """End a task that ran past ``expiresAt`` without a commit."""
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

    def fail(self, task_id: str) -> Reply:
        """Memory hit an internal error on this task: end it (phase ``failed``).

        Says nothing about the error itself; log that on the server.
        """
        task = self._tasks.get(task_id)
        if task is not None:
            task.phase = 'failed'
        return Reply(
            phase='failed',
            text='Memory hit an internal error, so this task failed. Negotiate again.',
            error=models.Error(code='internal', message='Internal error.'),
        )

    def visible_dossier(
        self, task_id: str, dossier: models.Dossier, viewer: Identity | None = None
    ) -> models.Dossier:
        """A stored dossier as the task's principal may see it now.

        For reads (GetTask, ListTasks, stream snapshots) in any task state:
        drops items the task's principal, or `viewer` (the caller now), can no
        longer see, then what rested on them (precedent cited because of a
        dropped fact, constraints whose basis was dropped), and rewrites
        ``watching`` and the summary to match. Returns `dossier` itself if
        nothing was dropped. Otherwise the result gets its own version,
        ``<version>-redacted-<hash>``, derived from what is left, so that a
        version always names one content (spec 10.3).
        """
        task = self._tasks.get(task_id)
        if task is None or task.intent is None:
            return dossier
        viewers = [task.identity] if viewer in (None, task.identity) else [task.identity, viewer]
        caches: list[dict[str, bool]] = [{} for _ in viewers]

        def sees(item_id: str) -> bool:
            return all(self._sees(v, item_id, c) for v, c in zip(viewers, caches, strict=True))

        def still_relevant(item_id: str) -> bool:
            record = self._precedent.get(item_id)
            return record is not None and any(_relevant(w, task, confirmed) for w in record.when)

        facts = [f for f in dossier.facts if sees(f.id)]
        confirmed = {f.id for f in facts if f.status == 'confirmed'}
        precedent = [p for p in dossier.precedent if sees(p.id) and still_relevant(p.id)]
        kept = {f.id for f in facts} | {p.id for p in precedent}
        constraints = [
            c
            for c in dossier.constraints
            if all(b in kept or not self._is_item(b) for b in c.basis)
        ]
        if (len(facts), len(precedent), len(constraints)) == (
            len(dossier.facts),
            len(dossier.precedent),
            len(dossier.constraints),
        ):
            return dossier
        kept_items = sorted(
            [(f.id, f.version) for f in facts]
            + [(p.id, p.version) for p in precedent]
            + [(c.id, c.version) for c in constraints]
        )
        digest = hashlib.sha256(repr(kept_items).encode()).hexdigest()[:12]
        return dossier.model_copy(
            update={
                'version': f'{dossier.version}-redacted-{digest}',
                'facts': facts,
                'precedent': precedent,
                'constraints': constraints,
                'watching': [
                    *(f.id for f in facts),
                    *(p.id for p in precedent),
                    *(c.id for c in constraints),
                ],
                'summary': self.summarize(facts, precedent, constraints),
            }
        )

    # ------------------------------------------------------ protocol steps
    def _advance(self, task: TaskRecord) -> Reply:
        """Ask the next question, or deliver the first dossier."""
        rule = self._next_question(task)
        if rule is not None:
            task.phase, task.question = 'question', rule
            return Reply(phase='question', text=rule.prompt or rule.text, question=rule.model())
        task.phase, task.question = 'awaiting-commit', None
        if self.watch_timeout is not None:
            # Spec 8.3.9: the watch runs from the first dossier.
            task.expires_at = self.clock() + self.watch_timeout
        dossier = self._new_dossier(task, *self._compose(task))
        return Reply(phase='awaiting-commit', text=dossier.summary, dossier=dossier)

    def _answer(self, task: TaskRecord, data: Any) -> Reply:
        question = task.question
        assert question is not None
        try:
            answer = models.Answer.parse(data)
        except SchemaValidationError as error:
            return self._still_asking(
                task, 'invalid-answer', f'Not a valid answer: {"; ".join(error.errors)}'
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

    def _commit(self, task: TaskRecord, data: Any) -> Reply:
        try:
            commit = models.Commit.parse(data)
        except SchemaValidationError as error:
            return self._not_recorded(
                'invalid-commit', f'Not a valid commit: {"; ".join(error.errors)}'
            )
        # Compare against memory's view now, not only the last dossier sent:
        # an update may still be on its way to the agent.
        fresh = self._revise(task)
        current = task.dossier
        assert current is not None
        if commit.based_on != current.version:
            return self._stale(task, commit.based_on, fresh)
        unknown = [c.id for c in commit.conflicts or () if c.id not in current.watching]
        if unknown:
            return self._not_recorded(
                'invalid-commit',
                f'Conflicts must name items of dossier {current.version}; not in it: '
                + ', '.join(unknown),
            )
        if self.commit_policy is not None:
            reason = self.commit_policy(task, commit)
            if reason:
                return self._not_recorded('commit-refused', reason)
        return self._record(task, commit)

    def _stale(self, task: TaskRecord, based_on: str, fresh: models.Update | None) -> Reply:
        """Spec 8.4.3: record nothing; say what changed since `based_on`, if
        it is a version this task was given."""
        current = task.dossier
        assert current is not None
        previous = task.dossiers.get(based_on)
        update = self._update(previous, current) if previous is not None else fresh
        text = (
            f'Not recorded: your commit is based on dossier {based_on}, but the current '
            f'version is {current.version}.'
        )
        if update is not None:
            text += f' {update.summary}'
        return Reply(
            phase='awaiting-commit',
            text=text,
            dossier=current if fresh is not None else None,  # new since the agent's last event
            update=update,
            error=models.Error(
                code='stale-dossier',
                message=f'Commit is based on dossier {based_on}; the current version is '
                f'{current.version}. Read the current dossier and commit again.',
                current_version=current.version,
            ),
        )

    def _record(self, task: TaskRecord, commit: models.Commit) -> Reply:
        """Record claims (never confirmed), queue conflicts, tell other tasks."""
        assert task.intent is not None and task.dossier is not None
        now = self.clock()
        identity = task.identity
        commit_id = f'cm-{next(self._commits)}'
        source = f'commit:{commit_id}'
        self._sources[source] = SourceRecord(ref=source, kind='agent-commit', at=now)
        origin = CommitOrigin(
            commit_id=commit_id,
            agent=identity.agent,
            principal=identity.principal,
            at=now,
            context=(*(f.id for f in task.dossier.facts), *(p.id for p in task.dossier.precedent)),
        )

        def review(kind: Literal['conflict', 'replace-proposal'], item: str, why: str) -> None:
            self.review_queue.append(
                ReviewItem(
                    kind, task.id, commit_id, item, why, identity.agent, identity.principal, now
                )
            )

        recorded: list[models.RecordedFact] = []
        notes: dict[str, str | None] = {}
        entities: set[str] = set()
        for claim in commit.claims:
            record = FactRecord(
                id=self._new_fact_id(),
                statement=claim.statement,
                status='claim',
                sources=(source,),
                entities=_ordered(claim.entities or task.intent.entities),
                origin=origin,
                evidence=tuple(claim.evidence or ()),
            )
            self._facts[record.id] = record
            recorded.append(models.RecordedFact(fact_id=record.id, version=str(record.revision)))
            notes[record.id] = (
                f'Reported by {identity.principal}, not yet confirmed: {claim.statement}'
            )
            entities |= set(record.entities)
            for old_id in claim.replaces or ():
                # A hint for people only: memory never retires anything for a claim.
                review('replace-proposal', old_id, f'Claim {record.id} says it replaces {old_id}.')
        for conflict in commit.conflicts or ():
            review('conflict', conflict.id, conflict.explanation)

        task.phase = 'committed'  # before notifying: this task stops watching
        self._changed({r.fact_id for r in recorded}, entities, notes=notes)

        conflicts = list(dict.fromkeys(c.id for c in commit.conflicts or ()))
        text = (
            'Recorded as a claim until a person or a system of record confirms it.'
            if len(recorded) == 1
            else f'Recorded as {len(recorded)} claims until a person or a system of record '
            'confirms them.'
        )
        if conflicts:
            text += f' {len(conflicts)} conflict(s) sent to a person for review.'
        receipt = models.Receipt(
            commit_id=commit_id,
            based_on=commit.based_on,
            recorded=recorded,
            conflicts=conflicts,
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
        """Reject a message in phase ``question``, asking the question again."""
        assert task.question is not None
        return Reply(
            phase='question',
            text=_clip(f'Not accepted: {message}', 4096),
            question=task.question.model(),
            error=models.Error(code=code, message=_clip(message, 2048)),
        )

    def _not_recorded(self, code: ErrorCode, message: str) -> Reply:
        """Reject a commit; the task stays in phase ``awaiting-commit``."""
        return Reply(
            phase='awaiting-commit',
            text=_clip(f'Not recorded: {message}', 4096),
            error=models.Error(code=code, message=_clip(message, 2048)),
        )

    def _unexpected(self, task: TaskRecord, message: str) -> Reply:
        """A message that doesn't fit the task's phase; nothing changes."""
        if task.phase == 'question':
            return self._still_asking(task, 'unexpected-message', message)
        return Reply(
            phase=task.phase,
            text=_clip(f'Not accepted: {message}', 4096),
            error=models.Error(code='unexpected-message', message=_clip(message, 2048)),
        )

    # --------------------------------------------------- dossier building
    def _revise(self, task: TaskRecord) -> models.Update | None:
        """Recompute the task's dossier. If its items or their versions
        changed, store a new version and return the update."""
        previous = task.dossier
        assert previous is not None
        items = self._compose(task)
        if _signature(*items) == _signature(
            previous.facts, previous.precedent, previous.constraints
        ):
            return None
        return self._update(previous, self._new_dossier(task, *items))

    def _update(self, old: models.Dossier, new: models.Dossier) -> models.Update | None:
        changes = _diff(old, new)
        if not changes:
            return None
        return models.Update(
            dossier_version=new.version,
            previous_version=old.version,
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
        dossier = models.Dossier(
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
        task.dossier = task.dossiers[dossier.version] = dossier
        return dossier

    def _compose(
        self, task: TaskRecord
    ) -> tuple[list[models.Fact], list[models.Precedent], list[models.Constraint]]:
        """Select what the task's principal may see and needs to know now."""
        intent, identity = task.intent, task.identity
        assert intent is not None
        wanted = set(intent.entities)
        seen: dict[str, bool] = {}

        facts = [
            f
            for f in self._facts.values()
            if not f.retired
            and not wanted.isdisjoint(f.entities)
            and self._sees(identity, f.id, seen)
        ]
        # Confirmed facts first, then claims; each newest first, undated last.
        facts.sort(key=lambda f: (f.status == 'claim', -self._when(f), f.id))
        # Relevance and constraints use only what this principal can see
        # (spec 11.5), and never claims (spec 9.4).
        confirmed = {f.id for f in facts if f.status == 'confirmed'}
        precedent = [
            p
            for p in self._precedent.values()
            if not p.retired
            and self._sees(identity, p.id, seen)
            and any(_relevant(w, task, confirmed) for w in p.when)
        ]
        precedent.sort(key=lambda p: (-_timestamp(p.decided_at), p.id))
        bases = confirmed | {p.id for p in precedent}
        constraints = [
            self._constraint_model(p)
            for p in self._policies.values()
            if not p.retired
            and _matches(intent, p.actions, p.entities)
            and (p.policy is not None or set(p.basis) <= bases)
        ]
        constraints.sort(key=lambda c: (c.level != 'must', c.id))
        return (
            [self._fact_model(f) for f in facts],
            [self._precedent_model(p) for p in precedent],
            constraints,
        )

    def _fact_model(self, f: FactRecord) -> models.Fact:
        claimed_by = None
        if f.status == 'claim' and f.origin is not None:
            claimed_by = models.Attribution(
                agent=f.origin.agent,
                on_behalf_of=f.origin.principal,
                at=f.origin.at,
                commit_id=f.origin.commit_id,
            )
        return models.Fact(
            id=f.id,
            version=str(f.revision),
            statement=f.statement,
            status=f.status,
            source=self._sources[f.sources[0]].model(),
            confirmed_by=f.confirmed_by,
            claimed_by=claimed_by,
            entities=list(f.entities) or None,
            evidence=list(f.evidence) or None,
            observed_at=f.observed_at,
            supersedes=list(f.supersedes) or None,
        )

    def _precedent_model(self, p: PrecedentRecord) -> models.Precedent:
        return models.Precedent(
            id=p.id,
            version=str(p.revision),
            statement=p.statement,
            relevance=p.relevance,
            source=self._sources[p.sources[0]].model(),
            decided_by=p.decided_by,
            decided_at=p.decided_at,
            entities=list(p.entities) or None,
        )

    def _constraint_model(self, p: PolicyRecord) -> models.Constraint:
        return models.Constraint(
            id=p.id,
            version=str(p.revision),
            statement=p.statement,
            level=p.level,
            basis=list(p.basis) if p.basis else [str(p.policy)],
            until=p.until,
        )

    def _when(self, f: FactRecord) -> float:
        claimed_at = f.origin.at if f.origin else None
        return _timestamp(f.observed_at or claimed_at or self._sources[f.sources[0]].at)

    # ---------------------------------------------------------- permissions
    def _sees(self, identity: Identity, item_id: str, cache: dict[str, bool]) -> bool:
        """May `identity` see a fact or precedent now? (Spec 10.3 and 10.5.)

        It must be able to read every source the item derives from. A claim
        is also visible only to its claimant, or to principals who may read
        every entity it names and see every item of the dossier its commit
        was based on. Memoized in `cache` for one computation.
        """
        if item_id in cache:
            return cache[item_id]
        cache[item_id] = False  # fail closed if a claim ever rested on itself
        item: FactRecord | PrecedentRecord | None = self._facts.get(item_id)
        if item is None:
            item = self._precedent.get(item_id)
        visible = item is not None and all(
            identity.can_read(self._sources[s].readers) for s in item.sources
        )
        origin = item.origin if isinstance(item, FactRecord) else None
        if visible and origin is not None and origin.principal != identity.principal:
            assert item is not None
            visible = all(
                identity.can_read(self._entity_readers.get(e, ())) for e in item.entities
            ) and all(self._sees(identity, i, cache) for i in origin.context)
        cache[item_id] = visible
        return visible

    def _claims_resting_on(self, ids: set[str]) -> set[str]:
        """Claims whose visibility depends on any of `ids`, transitively."""
        found: set[str] = set()
        frontier = set(ids)
        while frontier:
            frontier = {
                f.id
                for f in self._facts.values()
                if f.origin and f.id not in found and not frontier.isdisjoint(f.origin.context)
            }
            found |= frontier
        return found

    def _access_changed(self, ids: set[str]) -> list[str]:
        for item_id in ids:
            self._notes.pop(item_id, None)  # never explain an access change
        entities = {e for item in self._items() if item.id in ids for e in item.entities}
        return self._changed(ids, entities)

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
    def _items(self) -> Iterable[FactRecord | PrecedentRecord]:
        yield from self._facts.values()
        yield from self._precedent.values()

    def _is_item(self, item_id: str) -> bool:
        return item_id in self._facts or item_id in self._precedent

    def _check_new_item(
        self,
        item_id: str,
        statement: str,
        sources: Sequence[str],
        *,
        version: int,
        limit: int = MAX_STATEMENT,
    ) -> None:
        _check(0 < len(item_id) <= MAX_ID, f'Ids must have 1 to {MAX_ID} characters')
        _check(version >= 1, 'Versions start at 1')
        _check(
            item_id not in self._facts
            and item_id not in self._precedent
            and item_id not in self._policies,
            f'{item_id} is already in use: facts, precedent and constraints share one set of ids',
        )
        _check_text('statement', statement, limit)
        missing = [ref for ref in sources if ref not in self._sources]
        _check(not missing, f'Unknown source(s): {", ".join(missing)}; call add_source first')
        _check(
            all(self._sources[ref].kind != 'agent-commit' for ref in sources),
            'Only commits create facts from agent-commit sources',
        )

    def _new_fact_id(self) -> str:
        numbers = [int(m.group(1)) for i in self._facts if (m := re.fullmatch(r'f-(\d+)', i))]
        n = max(numbers, default=0) + 1
        while f'f-{n}' in self._precedent or f'f-{n}' in self._policies:
            n += 1
        return f'f-{n}'

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
def _single(payloads: Payloads) -> tuple[PayloadKind | None, Any]:
    """The kind and data of a message's one Mem2A payload, or (None, None)."""
    if len(payloads) != 1:
        return None, None
    media_type, data = payloads[0]
    return MEDIA_TYPES.get(media_type), data


def _unexpected_count(payloads: Payloads) -> str | None:
    if len(payloads) == 1:
        return None
    return f'Send exactly one Mem2A payload per message; this one has {len(payloads)}.'


def _unexpected_kind(task: TaskRecord, kind: PayloadKind | None) -> str:
    if kind == 'intent':
        return 'This task already has an intent. Start a new task for a new action.'
    if task.phase == 'question' and task.question is not None:
        return f'Memory is waiting for an answer to question {task.question.id}.'
    return 'Memory is waiting for a commit on this task (or for the task to be canceled).'


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


def _relevant(when: When, task: TaskRecord, confirmed: set[str]) -> bool:
    assert task.intent is not None
    return (
        _matches(task.intent, when.actions, when.entities)
        and (not when.facts or not when.facts.isdisjoint(confirmed))
        and when.tags <= task.tags
    )


def _overdue(task: TaskRecord, now: datetime) -> bool:
    return task.open and task.expires_at is not None and task.expires_at <= now


Item = models.Fact | models.Precedent | models.Constraint


def _signature(
    facts: Sequence[Item], precedent: Sequence[Item], constraints: Sequence[Item]
) -> frozenset[tuple[str, str]]:
    """What a dossier version stands for: its items' ids and versions."""
    return frozenset((item.id, item.version) for item in (*facts, *precedent, *constraints))


def _diff(old: models.Dossier, new: models.Dossier) -> list[models.Change]:
    """Changes by kind (facts, constraints, precedent); within each kind:
    added, updated (new version), removed, in dossier order."""
    return [
        *_diff_items('fact', old.facts, new.facts),
        *_diff_items('constraint', old.constraints, new.constraints),
        *_diff_items('precedent', old.precedent, new.precedent),
    ]


def _diff_items(
    kind: models.ItemKind, old: Sequence[Item], new: Sequence[Item]
) -> list[models.Change]:
    before = {item.id: item.version for item in old}
    after = {item.id: item.version for item in new}
    return [
        *(models.Change(id=i, kind=kind, change='added') for i in after if i not in before),
        *(
            models.Change(id=i, kind=kind, change='updated')
            for i, version in after.items()
            if i in before and before[i] != version
        ),
        *(models.Change(id=i, kind=kind, change='removed') for i in before if i not in after),
    ]
