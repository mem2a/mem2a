# SPDX-License-Identifier: Apache-2.0
"""The spec's wire rules (sections 6 and 7), checked on everything the tests see.

Mirrors conformance/test_spec_examples.py, so what this server emits is held
to the same rules as the spec's examples, plus a few more that the examples
cannot show (a text part on every memory status message).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypeVar

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtoMessage

from mem2a import validation
from mem2a.constants import AGENT_PAYLOADS, EXTENSION_URI, MEDIA_TYPES, PHASE_KEY


T = TypeVar('T')

PHASES_BY_STATE = {
    'TASK_STATE_INPUT_REQUIRED': {'question', 'awaiting-commit'},
    'TASK_STATE_COMPLETED': {'committed'},
    'TASK_STATE_REJECTED': {'refused'},
    'TASK_STATE_CANCELED': {'expired', 'canceled'},
}


def walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def mem2a_parts(node: Any) -> Iterator[tuple[str, Any]]:
    for item in walk(node):
        if isinstance(item, dict) and str(item.get('mediaType', '')).startswith(
            'application/vnd.mem2a.'
        ):
            yield item['mediaType'], item.get('data')


def check(obj: T) -> T:
    """Assert the Mem2A wire rules on a Task, StreamResponse, push body or
    JSON-RPC response, and return it unchanged."""
    data = MessageToDict(obj) if isinstance(obj, ProtoMessage) else obj
    for media_type, payload in mem2a_parts(data):
        assert media_type in MEDIA_TYPES, media_type
        problems = validation.errors(MEDIA_TYPES[media_type], payload)
        assert not problems, (media_type, problems)
    for item in walk(data):
        if not isinstance(item, dict):
            continue
        if 'messageId' in item and 'role' in item:
            kinds = [MEDIA_TYPES[mt] for mt, _ in mem2a_parts(item)]
            assert EXTENSION_URI in item.get('extensions', []), item['messageId']
            if item['role'] == 'ROLE_USER':
                assert len(kinds) == 1 and kinds[0] in AGENT_PAYLOADS, item
            else:
                assert not set(kinds) & AGENT_PAYLOADS, item
                assert any('text' in part for part in item['parts']), item
                assert item.get('metadata', {}).get(PHASE_KEY), item
        if str(item.get('state', '')).startswith('TASK_STATE_') and 'message' in item:
            phase = item['message'].get('metadata', {}).get(PHASE_KEY)
            assert phase in PHASES_BY_STATE.get(item['state'], set()), (item['state'], phase)
        if 'artifactId' in item:
            kinds = [MEDIA_TYPES[mt] for mt, _ in mem2a_parts(item)]
            assert len(kinds) == 1 and item['artifactId'] == kinds[0], item
            assert item.get('name') == kinds[0], item
            assert EXTENSION_URI in item.get('extensions', []), item
    return obj
