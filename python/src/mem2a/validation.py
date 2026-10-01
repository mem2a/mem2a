# SPDX-License-Identifier: Apache-2.0
"""JSON Schema validation against the Mem2A v0.1 schemas shipped in this package.

The schemas in ``mem2a/schemas`` are byte-identical copies of the normative
schemas in the specification. What memory receives is checked the way spec
2.5 asks: members this version doesn't define are dropped (`known_members`),
then the rest is validated. What memory sends must validate as is, unknown
members included (see `check_outgoing`).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


if TYPE_CHECKING:
    if sys.version_info >= (3, 11):
        from importlib.resources.abc import Traversable
    else:
        from importlib.abc import Traversable

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

#: What to do when a payload memory is about to send breaks its schema:
#: ``raise`` (tests, development), ``log`` (send it anyway, log an error),
#: or ``off``.
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
        schema = json.loads((schema_dir() / f'{name}.schema.json').read_text(encoding='utf-8'))
        registry = registry.with_resource(schema['$id'], Resource.from_contents(schema))
        schemas[name] = schema
    return registry, schemas


def schema(kind: str) -> dict[str, Any]:
    """The parsed JSON Schema for a payload kind, e.g. ``'dossier'``."""
    return _load()[1][kind]


@cache
def validator(kind: str) -> Draft202012Validator:
    """A cached draft 2020-12 validator (with format checks) for `kind`."""
    registry, schemas = _load()
    return Draft202012Validator(schemas[kind], registry=registry, format_checker=FormatChecker())


def errors(kind: str, payload: Any) -> list[str]:
    """Readable schema violations; empty if the payload is valid."""
    found = sorted(validator(kind).iter_errors(payload), key=lambda e: e.json_path)
    return [f'{e.message} (at {e.json_path})' for e in found]


def validate(kind: str, payload: Any) -> None:
    """Raise `SchemaValidationError` unless `payload` matches its schema."""
    problems = errors(kind, payload)
    if problems:
        raise SchemaValidationError(kind, problems)


def check_outgoing(kind: str, payload: Any, mode: ValidationMode) -> None:
    """Apply the outgoing-validation policy to a payload memory is about to send.

    Strict: a payload memory sends must validate as is, unknown members
    included (spec 2.5 lets receivers ignore them; senders must not add them).
    """
    if mode == 'off':
        return
    problems = errors(kind, payload)
    if not problems:
        return
    if mode == 'raise':
        raise SchemaValidationError(kind, problems)
    logger.error('Outgoing %s payload violates its schema: %s', kind, problems)


def known_members(kind: str, payload: Any) -> Any:
    """`payload` without the object members its schema doesn't define (spec 2.5).

    A receiver ignores what a later version may add, then validates the rest.
    Wherever the schema lists an object's ``properties`` and allows no others,
    other members are dropped, in nested objects and arrays too. Free-form
    objects (``metadata``) are kept whole. Returns a copy; `payload` is not
    changed.
    """
    root = schema(kind)
    return _strip(payload, root, root['$id'])


def _strip(value: Any, node: Any, base: str) -> Any:
    parts = list(_parts(node, base))
    if isinstance(value, dict):
        members: dict[str, tuple[Any, str]] = {}
        closed = False
        for part, part_base in parts:
            closed = closed or part.get('additionalProperties') is False
            for name, subschema in part.get('properties', {}).items():
                members.setdefault(name, (subschema, part_base))
        return {
            name: _strip(member, *members[name]) if name in members else member
            for name, member in value.items()
            if name in members or not closed
        }
    if isinstance(value, list):
        items = next(((p['items'], b) for p, b in parts if isinstance(p.get('items'), dict)), None)
        return [_strip(item, *items) for item in value] if items else list(value)
    return value


def _parts(node: Any, base: str) -> Iterator[tuple[dict[str, Any], str]]:
    """`node` and every schema it applies through ``$ref`` and ``allOf``, each
    with the URI of the schema document it sits in, which its ``$ref`` is
    relative to."""
    if not isinstance(node, dict):
        return
    yield node, base
    if isinstance(node.get('$ref'), str):
        target = urljoin(base, node['$ref'])
        resolved = _load()[0].resolver().lookup(target)
        yield from _parts(resolved.contents, target.partition('#')[0])
    for branch in node.get('allOf', ()):
        yield from _parts(branch, base)
