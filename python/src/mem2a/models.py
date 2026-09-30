# SPDX-License-Identifier: Apache-2.0
"""Typed models of the Mem2A v0.1 payloads (pydantic v2, camelCase on the wire).

The JSON Schemas in ``mem2a/schemas`` are the contract. These models are a
convenience for building and reading payloads:

* ``Intent.parse(data)`` validates `data` against the intent schema, then
  returns the model. Every top-level payload has ``parse``.
* ``payload.dump()`` returns the JSON-ready dict that goes on the wire.
"""

from __future__ import annotations

from datetime import datetime
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
    observed_at: datetime | None = None
    supersedes: list[str] | None = None
    visibility: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Precedent(Payload):
    id: str
    statement: str
    relevance: str
    source: Source
    decided_by: str | None = None
    decided_at: datetime | None = None
    entities: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Constraint(Payload):
    id: str
    statement: str
    level: Level
    basis: list[str]
    until: str | None = None
    metadata: dict[str, Any] | None = None


class Claim(Payload):
    statement: str
    entities: list[str] | None = None
    supersedes: list[str] | None = None
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
    at: datetime
    conflicts_recorded: int | None = None
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
    watch_timeout_seconds: int | None = None
    entity_types: list[str] | None = None
