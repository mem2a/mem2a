# SPDX-License-Identifier: Apache-2.0
"""Typed models of the Mem2A v0.1 payloads (pydantic v2, camelCase on the wire).

The JSON Schemas in ``mem2a/schemas`` are the contract; these models are a
convenience for building and reading payloads. Use `parse` to validate
against the schema and get a model back, and `Payload.dump` to get the
JSON-ready dict that goes on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from mem2a import validation
from mem2a.constants import ErrorCode, PayloadKind


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
ChangeKind = Literal['fact', 'precedent', 'constraint']
ChangeType = Literal['added', 'updated', 'removed']


class Payload(BaseModel):
    """Base class: camelCase aliases, unknown fields rejected (as in the schemas)."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra='forbid'
    )

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
    level: Literal['must', 'should']
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


# ------------------------------------------------------- agent -> memory
class Intent(Payload):
    action: str
    summary: str
    entities: list[str]
    on_behalf_of: str
    draft: str | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] | None = None


class Answer(Payload):
    question_id: str
    text: str
    metadata: dict[str, Any] | None = None


class Commit(Payload):
    based_on: str
    action: str
    outcome: Literal['done', 'partial', 'failed']
    summary: str
    claims: list[Claim]
    conflicts: list[Conflict] | None = None
    metadata: dict[str, Any] | None = None


# ------------------------------------------------------- memory -> agent
class Dossier(Payload):
    version: str
    summary: str
    facts: list[Fact]
    precedent: list[Precedent]
    constraints: list[Constraint]
    watching: list[str]
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class Question(Payload):
    id: str
    text: str
    options: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Change(Payload):
    id: str
    kind: ChangeKind
    change: ChangeType


class Update(Payload):
    dossier_version: str
    previous_version: str
    summary: str
    changes: list[Change]
    metadata: dict[str, Any] | None = None


class RecordedFact(Payload):
    fact_id: str
    version: str
    status: Literal['claim'] = 'claim'


class Receipt(Payload):
    commit_id: str
    based_on: str
    recorded: list[RecordedFact]
    at: datetime
    conflicts_recorded: int | None = None
    metadata: dict[str, Any] | None = None


class Error(Payload):
    code: ErrorCode
    message: str
    current_version: str | None = None
    metadata: dict[str, Any] | None = None


class ExtensionParams(Payload):
    """The ``params`` of the Mem2A entry in a memory's Agent Card."""

    spec_version: Literal['0.1'] = '0.1'
    listen: list[Literal['push', 'subscribe']]
    watch_timeout_seconds: int | None = None
    entity_types: list[str] | None = None


MODELS: dict[str, type[Payload]] = {
    'intent': Intent,
    'answer': Answer,
    'commit': Commit,
    'dossier': Dossier,
    'question': Question,
    'update': Update,
    'receipt': Receipt,
    'error': Error,
    'extension-params': ExtensionParams,
}

P = TypeVar('P', bound=Payload)


def parse(kind: PayloadKind | Literal['extension-params'], data: Any) -> Payload:
    """Validate `data` against its schema, then return the typed model.

    Raises `mem2a.validation.SchemaValidationError` if the data is invalid.
    """
    validation.validate(kind, data)
    return MODELS[kind].model_validate(data)


def parse_as(model: type[P], kind: PayloadKind, data: Any) -> P:
    """Like `parse`, typed for the caller: ``parse_as(Commit, 'commit', data)``."""
    validation.validate(kind, data)
    return model.model_validate(data)
