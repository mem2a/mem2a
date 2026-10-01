# SPDX-License-Identifier: Apache-2.0
"""The packaged schemas, the models, durations and the Agent Card helper."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict

from mem2a import (
    build_agent_card,
    format_duration,
    mem2a_params,
    models,
    parse_duration,
    validation,
)
from mem2a.constants import EXTENSION_URI, INPUT_MODES, MEDIA_TYPES, OUTPUT_MODES
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
    intent = {'action': 'x', 'summary': 'x', 'entities': ['a:b'], 'onBehalfOf': 'u'}
    with pytest.raises(SchemaValidationError):
        models.Intent.parse({**intent, 'deadline': 'soon'})
    with pytest.raises(SchemaValidationError):  # Evidence URLs must be https
        models.Commit.parse(
            {
                'basedOn': '1',
                'action': 'x',
                'outcome': 'done',
                'summary': 'x',
                'claims': [
                    {
                        'statement': 'x',
                        'evidence': [{'kind': 'url', 'ref': 'r', 'url': 'http://a.b'}],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ('text', 'duration'),
    [
        ('P7D', timedelta(days=7)),
        ('PT12H', timedelta(hours=12)),
        ('PT1H30M', timedelta(minutes=90)),
        ('P1DT2H3M4S', timedelta(days=1, hours=2, minutes=3, seconds=4)),
        ('PT0S', timedelta(0)),
    ],
)
def test_durations(text: str, duration: timedelta) -> None:
    assert parse_duration(text) == duration
    assert format_duration(duration) == text
    assert not validation.errors(
        'extension-params', {'specVersion': '0.1', 'listen': ['push'], 'watchTimeout': text}
    )


@pytest.mark.parametrize('text', ['', 'P', 'PT', 'P1W', 'P1Y', 'P1M', '7D', 'PT1.5S', 'P1DT'])
def test_invalid_durations(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


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
    assert entry['params']['watchTimeout'] == 'P7D'
    assert (data['defaultInputModes'], data['defaultOutputModes']) == (
        list(INPUT_MODES),
        list(OUTPUT_MODES),
    )
    assert [s['id'] for s in data['skills']] == ['negotiate', 'commit']
    assert data['securitySchemes'] and data['securityRequirements']

    params = mem2a_params(card)
    assert params is not None
    assert params.watch_timeout_delta() == timedelta(days=7)
    assert (params.entity_types, params.listen) == (['account', 'project'], ['push', 'subscribe'])


def test_watch_timeouts_are_whole_seconds() -> None:
    with pytest.raises(ValueError):
        build_agent_card('https://memory.example.com', watch_timeout=timedelta(seconds=1.5))
