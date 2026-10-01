"""Check that every example in spec/v0.1/examples follows the Mem2A v0.1 spec.

Run from the repository root:

    pip install -r conformance/requirements.txt
    pytest conformance

What this checks:
  * every Mem2A payload (a part whose mediaType is application/vnd.mem2a.*)
    validates against its JSON Schema, with formats enforced;
  * the Agent Card's Mem2A entry and params;
  * the message rules in spec section 7: agent messages carry exactly one
    Mem2A payload and list the extension URI, memory status messages carry a
    phase the task state allows, replies name the message they answer, and
    Mem2A artifacts use their reserved ids;
  * the dossier rules in sections 8.1.5 and 9: `watching` lists exactly the
    dossier's items, and no constraint rests on a claim;
  * a dossier version always means the same content, across examples.

It checks the examples in this repository. To check a running memory, use
`mem2a-conform` from the reference implementation (see python/README.md).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "v0.1"
URI = "https://w3id.org/mem2a/v0.1"
PHASE_KEY = f"{URI}/phase"
IN_REPLY_TO_KEY = f"{URI}/inReplyTo"

MEDIA_TYPES = {
    "application/vnd.mem2a.intent+json": "intent",
    "application/vnd.mem2a.answer+json": "answer",
    "application/vnd.mem2a.commit+json": "commit",
    "application/vnd.mem2a.dossier+json": "dossier",
    "application/vnd.mem2a.question+json": "question",
    "application/vnd.mem2a.update+json": "update",
    "application/vnd.mem2a.receipt+json": "receipt",
    "application/vnd.mem2a.error+json": "error",
}
AGENT_PAYLOADS = {"intent", "answer", "commit"}

# Spec section 7.2: which phases may accompany which task states.
PHASES_BY_STATE = {
    "TASK_STATE_SUBMITTED": {"working"},
    "TASK_STATE_WORKING": {"working"},
    "TASK_STATE_INPUT_REQUIRED": {"question", "awaiting-commit"},
    "TASK_STATE_AUTH_REQUIRED": {"reauth"},
    "TASK_STATE_COMPLETED": {"committed"},
    "TASK_STATE_REJECTED": {"refused"},
    "TASK_STATE_CANCELED": {"expired", "canceled"},
    "TASK_STATE_FAILED": {"failed"},
}


def _load_registry() -> tuple[Registry, dict[str, dict[str, Any]]]:
    schemas: dict[str, dict[str, Any]] = {}
    registry = Registry()
    for path in sorted((SPEC / "schemas").glob("*.schema.json")):
        schema = json.loads(path.read_text())
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        schemas[path.name.removesuffix(".schema.json")] = schema
    return registry, schemas


REGISTRY, SCHEMAS = _load_registry()
EXAMPLES = sorted((SPEC / "examples").glob("*.json"))


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(SCHEMAS[name], registry=REGISTRY, format_checker=FormatChecker())


def load(path: Path) -> Any:
    return json.loads(path.read_text())


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
        if isinstance(item, dict) and str(item.get("mediaType", "")).startswith("application/vnd.mem2a."):
            yield item["mediaType"], item.get("data")


def dossiers(node: Any) -> Iterator[dict[str, Any]]:
    for media_type, data in mem2a_parts(node):
        if MEDIA_TYPES.get(media_type) == "dossier":
            yield data


def messages(node: Any) -> Iterator[dict[str, Any]]:
    for item in walk(node):
        if isinstance(item, dict) and "messageId" in item and "role" in item:
            yield item


def statuses(node: Any) -> Iterator[dict[str, Any]]:
    for item in walk(node):
        if isinstance(item, dict) and str(item.get("state", "")).startswith("TASK_STATE_"):
            yield item


def http_exchanges(node: Any) -> Iterator[dict[str, Any]]:
    for item in walk(node):
        if isinstance(item, dict) and isinstance(item.get("http"), dict):
            yield item["http"]


def test_schemas_are_valid_json_schema() -> None:
    for name, schema in SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith(f"{URI}/schemas/"), name


def test_every_media_type_has_a_schema() -> None:
    assert set(MEDIA_TYPES.values()) <= set(SCHEMAS)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_payloads_validate(path: Path) -> None:
    example = load(path)
    found = 0
    for media_type, data in mem2a_parts(example):
        assert media_type in MEDIA_TYPES, f"unknown Mem2A media type {media_type}"
        errors = sorted(validator(MEDIA_TYPES[media_type]).iter_errors(data), key=str)
        assert not errors, "\n".join(f"{media_type}: {e.message} at {list(e.path)}" for e in errors)
        found += 1
    if path.name != "01-agent-card.json":
        assert found, "example carries no Mem2A payload"


def test_agent_card_declares_extension() -> None:
    card = load(SPEC / "examples" / "01-agent-card.json")["agentCard"]
    entries = [e for e in card["capabilities"]["extensions"] if e["uri"] == URI]
    assert len(entries) == 1
    assert entries[0]["required"] is True
    params = entries[0]["params"]
    errors = list(validator("extension-params").iter_errors(params))
    assert not errors, errors
    if "push" in params["listen"]:
        assert card["capabilities"].get("pushNotifications") is True
    if "subscribe" in params["listen"]:
        assert card["capabilities"].get("streaming") is True
    assert card.get("securitySchemes") and card.get("securityRequirements")
    for media_type in ("intent", "answer", "commit"):
        assert f"application/vnd.mem2a.{media_type}+json" in card["defaultInputModes"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_message_rules(path: Path) -> None:
    for message in messages(load(path)):
        payloads = [MEDIA_TYPES.get(mt) for mt, _ in mem2a_parts(message)]
        assert URI in message.get("extensions", []), f"{message['messageId']} does not list the extension"
        if message["role"] == "ROLE_USER":
            assert len(payloads) == 1 and payloads[0] in AGENT_PAYLOADS, message["messageId"]
        else:
            assert not set(payloads) & AGENT_PAYLOADS, message["messageId"]
            assert any("text" in part for part in message["parts"]), f"{message['messageId']} has no text part"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_phase_matches_state(path: Path) -> None:
    for status in statuses(load(path)):
        message = status.get("message")
        if message is None:
            continue
        phase = message.get("metadata", {}).get(PHASE_KEY)
        assert phase in PHASES_BY_STATE.get(status["state"], set()), (status["state"], phase)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_replies_name_the_message_they_answer(path: Path) -> None:
    for http in http_exchanges(load(path)):
        body = http["request"]["body"]
        if body.get("method") != "SendMessage":
            continue
        asked = body["params"]["message"]["messageId"]
        reply = http["response"]["body"]["result"]["task"]["status"]["message"]
        assert reply["metadata"].get(IN_REPLY_TO_KEY) == asked, reply["messageId"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_mem2a_artifacts(path: Path) -> None:
    for item in walk(load(path)):
        if isinstance(item, dict) and "artifactId" in item:
            kinds = [MEDIA_TYPES.get(mt) for mt, _ in mem2a_parts(item)]
            if not kinds:
                continue  # not a Mem2A artifact; the spec allows others
            assert len(kinds) == 1, item["artifactId"]
            assert kinds[0] in {"dossier", "receipt"}, item["artifactId"]
            assert item["artifactId"] == kinds[0] and item["name"] == kinds[0]
            assert URI in item.get("extensions", [])


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_dossier_watches_exactly_its_items(path: Path) -> None:
    for dossier in dossiers(load(path)):
        items = [item["id"] for key in ("facts", "precedent", "constraints") for item in dossier[key]]
        assert len(items) == len(set(items)), f"dossier {dossier['version']} repeats an id"
        assert set(dossier["watching"]) == set(items), f"dossier {dossier['version']}"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_no_constraint_rests_on_a_claim(path: Path) -> None:
    for dossier in dossiers(load(path)):
        claims = {fact["id"] for fact in dossier["facts"] if fact["status"] == "claim"}
        for constraint in dossier["constraints"]:
            assert not claims & set(constraint["basis"]), constraint["id"]


def test_same_dossier_version_same_content() -> None:
    seen: dict[str, Any] = {}
    for path in EXAMPLES:
        for dossier in dossiers(load(path)):
            key = dossier["version"]
            assert seen.setdefault(key, dossier) == dossier, f"dossier {key} differs in {path.name}"
