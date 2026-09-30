# SPDX-License-Identifier: Apache-2.0
"""JSON Schema validation against the Mem2A v0.1 schemas shipped in this package.

The JSON Schemas are the contract. Incoming payloads are validated here
before they are parsed into models, and outgoing payloads can be validated
before they are sent (see `check_outgoing`).
"""

from __future__ import annotations

import json
import logging

from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

logger = logging.getLogger(__name__)

#: Schema file stems. Payload kinds use the same names.
SCHEMA_NAMES = (
    'answer',
    'commit',
    'common',
    'dossier',
    'error',
    'extension-params',
    'intent',
    'question',
    'receipt',
    'update',
)

#: What to do when an outgoing payload does not match its schema.
#: ``raise`` for tests and development, ``log`` or ``off`` if you must.
ValidationMode = Literal['raise', 'log', 'off']


class SchemaValidationError(ValueError):
    """A payload does not match its Mem2A schema."""

    def __init__(self, kind: str, errors: list[str]) -> None:
        self.kind = kind
        self.errors = errors
        super().__init__(f'{kind}: ' + '; '.join(errors))


def schema_dir() -> Traversable:
    """The directory holding the packaged ``*.schema.json`` files."""
    return files('mem2a') / 'schemas'


@cache
def _load() -> tuple[Registry, dict[str, dict[str, Any]]]:
    registry: Registry = Registry()
    schemas: dict[str, dict[str, Any]] = {}
    for name in SCHEMA_NAMES:
        schema = json.loads(
            (schema_dir() / f'{name}.schema.json').read_text(encoding='utf-8')
        )
        registry = registry.with_resource(
            schema['$id'], Resource.from_contents(schema)
        )
        schemas[name] = schema
    return registry, schemas


def schema(kind: str) -> dict[str, Any]:
    """The parsed JSON Schema for a payload kind, e.g. ``'dossier'``."""
    return _load()[1][kind]


@cache
def validator(kind: str) -> Draft202012Validator:
    registry, schemas = _load()
    return Draft202012Validator(
        schemas[kind], registry=registry, format_checker=FormatChecker()
    )


def errors(kind: str, payload: Any) -> list[str]:
    """Readable schema violations, empty if the payload is valid."""
    found = sorted(
        validator(kind).iter_errors(payload), key=lambda e: e.json_path
    )
    return [f'{e.message} (at {e.json_path})' for e in found]


def validate(kind: str, payload: Any) -> None:
    """Raise `SchemaValidationError` unless `payload` matches its schema."""
    problems = errors(kind, payload)
    if problems:
        raise SchemaValidationError(kind, problems)


def is_valid(kind: str, payload: Any) -> bool:
    return not errors(kind, payload)


def check_outgoing(kind: str, payload: Any, mode: ValidationMode) -> None:
    """Apply the outgoing-validation policy to a payload memory is about to send."""
    if mode == 'off':
        return
    problems = errors(kind, payload)
    if not problems:
        return
    if mode == 'raise':
        raise SchemaValidationError(kind, problems)
    logger.error('Outgoing %s payload violates its schema: %s', kind, problems)
