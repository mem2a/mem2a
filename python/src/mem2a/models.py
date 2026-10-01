# SPDX-License-Identifier: Apache-2.0
"""Typed models of the Mem2A v0.1 payloads (pydantic v2, camelCase on the wire).

The JSON Schemas in ``mem2a/schemas`` are the contract. These models are a
convenience for building and reading payloads:

* ``Intent.parse(data)`` validates `data` against the intent schema, then
  returns the model. Every top-level payload has ``parse``.
* ``payload.dump()`` returns the JSON-ready dict that goes on the wire.

Mem2A payloads contain no JSON numbers: versions are strings, and so is the
card's ``watchTimeout`` (an ISO 8601 duration; see `parse_duration`).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from typing_extensions import Self

from mem2a import validation
from mem2a.constants import ErrorCode


SourceKind = Literal[
    'meeting',
    'email',
    'message',
    'document',
    'ticket',
    'record',
    'decision',
    'policy',
    'agent-commit',
    'other',
]
EvidenceKind = Literal['email', 'message', 'document', 'record', 'url', 'other']
ItemKind = Literal['fact', 'precedent', 'constraint']
ChangeType = Literal['added', 'updated', 'removed']
Level = Literal['must', 'should']


class Payload(BaseModel):
    """Base class: camelCase aliases, unknown fields rejected (as in the schemas)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra='forbid')

    #: Schema file stem for top-level payloads (``'intent'``, ...); None for parts.
    schema_name: ClassVar[str | None] = None

    @classmethod
    def parse(cls, data: Any) -> Self:
        """Validate `data` against the payload's JSON Schema and return the model.

        Raises:
            mem2a.validation.SchemaValidationError: `data` breaks the schema.
        """
        if cls.schema_name is None:
            raise TypeError(f'{cls.__name__} is not a top-level Mem2A payload')
        validation.validate(cls.schema_name, data)
        return cls.model_validate(data)

    def dump(self) -> dict[str, Any]:
        """The JSON-ready dict, as sent on the wire."""
        return self.model_dump(mode='json', by_alias=True, exclude_none=True)


# ------------------------------------------------------------ shared pieces
class Source(Payload):
    ref: str
    kind: SourceKind
    title: str | None = None
    url: str | None = None
    at: datetime | None = None


class Attribution(Payload):
    agent: str
    on_behalf_of: str
    at: datetime
    commit_id: str | None = None


class Evidence(Payload):
    kind: EvidenceKind
    ref: str
    url: str | None = None
    at: datetime | None = None


class Fact(Payload):
    id: str
    version: str
    statement: str
    status: Literal['confirmed', 'claim']
    source: Source
    confirmed_by: str | None = None
    claimed_by: Attribution | None = None
    entities: list[str] | None = None
    evidence: list[Evidence] | None = None
    observed_at: datetime | None = None
    supersedes: list[str] | None = None
    visibility: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Precedent(Payload):
    id: str
    version: str
    statement: str
    relevance: str
    source: Source
    decided_by: str | None = None
    decided_at: datetime | None = None
    entities: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Constraint(Payload):
    id: str
    version: str
    statement: str
    level: Level
    basis: list[str]
    until: str | None = None
    metadata: dict[str, Any] | None = None


class Claim(Payload):
    statement: str
    entities: list[str] | None = None
    replaces: list[str] | None = None
    evidence: list[Evidence] | None = None
    metadata: dict[str, Any] | None = None


class Conflict(Payload):
    id: str
    explanation: str


class Change(Payload):
    id: str
    kind: ItemKind
    change: ChangeType


class RecordedFact(Payload):
    fact_id: str
    version: str
    status: Literal['claim'] = 'claim'


# ------------------------------------------------------- agent -> memory
class Intent(Payload):
    schema_name = 'intent'

    action: str
    summary: str
    entities: list[str]
    on_behalf_of: str
    draft: str | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] | None = None


class Answer(Payload):
    schema_name = 'answer'

    question_id: str
    text: str
    metadata: dict[str, Any] | None = None


class Commit(Payload):
    schema_name = 'commit'

    based_on: str
    action: str
    outcome: Literal['done', 'partial', 'failed']
    summary: str
    claims: list[Claim]
    conflicts: list[Conflict] | None = None
    metadata: dict[str, Any] | None = None


# ------------------------------------------------------- memory -> agent
class Dossier(Payload):
    schema_name = 'dossier'

    version: str
    summary: str
    facts: list[Fact]
    precedent: list[Precedent]
    constraints: list[Constraint]
    watching: list[str]
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class Question(Payload):
    schema_name = 'question'

    id: str
    text: str
    options: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Update(Payload):
    schema_name = 'update'

    dossier_version: str
    previous_version: str
    summary: str
    changes: list[Change]
    metadata: dict[str, Any] | None = None


class Receipt(Payload):
    schema_name = 'receipt'

    commit_id: str
    based_on: str
    recorded: list[RecordedFact]
    #: Ids from the commit's ``conflicts``, as recorded for review.
    conflicts: list[str]
    at: datetime
    metadata: dict[str, Any] | None = None


class Error(Payload):
    schema_name = 'error'

    code: ErrorCode
    message: str
    current_version: str | None = None
    metadata: dict[str, Any] | None = None


class ExtensionParams(Payload):
    """The ``params`` of the Mem2A entry in a memory's Agent Card."""

    schema_name = 'extension-params'

    spec_version: Literal['0.1'] = '0.1'
    listen: list[Literal['push', 'subscribe']]
    #: ISO 8601 duration, for example ``P7D``; see `watch_timeout_delta`.
    watch_timeout: str | None = None
    entity_types: list[str] | None = None

    def watch_timeout_delta(self) -> timedelta | None:
        """``watchTimeout`` as a timedelta, or None if memory doesn't expire watches."""
        return None if self.watch_timeout is None else parse_duration(self.watch_timeout)


# ------------------------------------------------------ ISO 8601 durations
#: Days, hours, minutes and seconds only, as extension-params.schema.json allows.
_DURATION = re.compile(r'^P(?!$)(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$')


def parse_duration(text: str) -> timedelta:
    """``'P7D'`` -> ``timedelta(days=7)``; ``'PT12H'`` -> 12 hours.

    Raises:
        ValueError: `text` is not a duration in days, hours, minutes and seconds.
    """
    match = _DURATION.match(text)
    if match is None:
        raise ValueError(f'Not an ISO 8601 duration in days, hours, minutes and seconds: {text!r}')
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def format_duration(duration: timedelta) -> str:
    """``timedelta(days=7)`` -> ``'P7D'``; 90 minutes -> ``'PT1H30M'``.

    Raises:
        ValueError: the duration is negative or not a whole number of seconds.
    """
    if duration < timedelta(0) or duration.microseconds:
        raise ValueError(f'Durations must be a whole, non-negative number of seconds: {duration}')
    hours, rest = divmod(duration.seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    date = f'{duration.days}D' if duration.days else ''
    time = ''.join(
        f'{value}{unit}' for value, unit in ((hours, 'H'), (minutes, 'M'), (seconds, 'S')) if value
    )
    if not date and not time:
        return 'PT0S'
    return f'P{date}' + (f'T{time}' if time else '')
