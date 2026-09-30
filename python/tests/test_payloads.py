# SPDX-License-Identifier: Apache-2.0
"""The packaged schemas, the models and the Agent Card helper."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict

from mem2a import build_agent_card, mem2a_params, models, validation
from mem2a.constants import EXTENSION_URI, MEDIA_TYPES
from mem2a.validation import SchemaValidationError

from wire import mem2a_parts


SPEC = Path(__file__).resolve().parents[2] / 'spec' / 'v0.1'
needs_spec = pytest.mark.skipif(not SPEC.is_dir(), reason='needs the spec directory')

MODELS: dict[str, type[models.Payload]] = {
    'intent': models.Intent,
    'answer': models.Answer,
    'commit': models.Commit,
    'dossier': models.Dossier,
    'question': models.Question,
    'update': models.Update,
    'receipt': models.Receipt,
    'error': models.Error,
}


def example_payloads() -> list[tuple[str, str, Any]]:
    found = []
    for path in sorted((SPEC / 'examples').glob('*.json')):
        for media_type, data in mem2a_parts(json.loads(path.read_text())):
            found.append((path.name, MEDIA_TYPES[media_type], data))
    return found


@needs_spec
def test_packaged_schemas_are_byte_identical_to_the_spec() -> None:
    spec_schemas = sorted((SPEC / 'schemas').glob('*.schema.json'))
    packaged = validation.schema_dir()
    assert sorted(p.name for p in spec_schemas) == sorted(
        entry.name for entry in packaged.iterdir() if entry.name.endswith('.schema.json')
    )
    for path in spec_schemas:
        assert (packaged / path.name).read_bytes() == path.read_bytes(), path.name


@needs_spec
@pytest.mark.parametrize(('example', 'kind', 'data'), example_payloads() if SPEC.is_dir() else [])
def test_models_round_trip_the_spec_examples(example: str, kind: str, data: Any) -> None:
    assert MODELS[kind].parse(data).dump() == data


def test_parse_rejects_what_the_schema_rejects() -> None:
    with pytest.raises(SchemaValidationError) as caught:
        models.Error.parse({'code': 'stale-dossier', 'message': 'Stale.'})
    assert 'currentVersion' in str(caught.value)
    with pytest.raises(SchemaValidationError):
        models.Intent.parse(
            {
                'action': 'x',
                'summary': 'x',
                'entities': ['a:b'],
                'onBehalfOf': 'u',
                'deadline': 'soon',
            }
        )


def test_agent_card() -> None:
    card = build_agent_card(
        'https://memory.example.com/a2a/jsonrpc',
        watch_timeout=timedelta(days=7),
        entity_types=['account', 'project'],
    )
    data = MessageToDict(card)
    assert data['capabilities']['streaming'] and data['capabilities']['pushNotifications']
    [entry] = data['capabilities']['extensions']
    assert (entry['uri'], entry['required']) == (EXTENSION_URI, True)
    validation.validate('extension-params', entry['params'])
    assert [s['id'] for s in data['skills']] == ['negotiate', 'commit']
    assert data['securitySchemes'] and data['securityRequirements']

    params = mem2a_params(card)
    assert params is not None
    assert (params.watch_timeout_seconds, params.entity_types) == (604800, ['account', 'project'])
    assert params.listen == ['push', 'subscribe']


def test_watch_timeout_below_the_schema_minimum_is_rejected() -> None:
    with pytest.raises(SchemaValidationError):
        build_agent_card('https://memory.example.com', watch_timeout=timedelta(seconds=30))
