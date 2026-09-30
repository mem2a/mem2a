# SPDX-License-Identifier: Apache-2.0
"""Build the Agent Card that marks an A2A agent as a Mem2A memory (spec 5)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct

from mem2a import constants as C
from mem2a.models import ExtensionParams


#: The scheme `mem2a.auth.DevTokenAuthenticator` implements.
DEV_TOKEN_SCHEME = SecurityScheme(
    http_auth_security_scheme=HTTPAuthSecurityScheme(
        scheme='Bearer',
        bearer_format='dev:<agent>:<principal>[:<group>,...]',
        description='Development tokens that name the agent and the principal it acts '
        'for. Unsigned: not for production. Replace with your identity provider, for '
        'example OAuth 2.0 Token Exchange (RFC 8693).',
    )
)


def extension_params(
    *,
    listen: Sequence[str] = ('push', 'subscribe'),
    watch_timeout: timedelta | None = None,
    entity_types: Iterable[str] | None = None,
) -> ExtensionParams:
    """The Mem2A ``params``, validated against extension-params.schema.json."""
    data: dict[str, object] = {'specVersion': C.SPEC_VERSION, 'listen': list(listen)}
    if watch_timeout is not None:
        data['watchTimeoutSeconds'] = int(watch_timeout.total_seconds())
    if entity_types is not None:
        data['entityTypes'] = list(entity_types)
    return ExtensionParams.parse(data)


def build_agent_card(
    url: str,
    *,
    name: str = 'Mem2A memory',
    description: str = (
        'Shared company memory. Tell it what you are about to do and for whom; it '
        'tells you what you need to know first, and calls back if that changes.'
    ),
    version: str = '0.1.0',
    organization: str | None = None,
    organization_url: str = '',
    watch_timeout: timedelta | None = None,
    entity_types: Iterable[str] | None = None,
    security_schemes: Mapping[str, SecurityScheme] | None = None,
    security_requirements: Sequence[SecurityRequirement] | None = None,
) -> AgentCard:
    """An Agent Card for a Mem2A memory served over JSON-RPC at `url`.

    Declares streaming and push notifications (both ways to listen), the
    required Mem2A extension with its params, the ``negotiate`` and ``commit``
    skills, and a security scheme. The default scheme describes the dev tokens
    of the reference server; pass your own for production.
    """
    params = extension_params(watch_timeout=watch_timeout, entity_types=entity_types)
    if security_schemes is None:
        security_schemes = {'dev-token': DEV_TOKEN_SCHEME}
    if security_requirements is None:
        security_requirements = [
            SecurityRequirement(schemes={scheme: StringList() for scheme in security_schemes})
        ]
    return AgentCard(
        name=name,
        description=description,
        version=version,
        provider=(
            AgentProvider(organization=organization, url=organization_url) if organization else None
        ),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding='JSONRPC', protocol_version='1.0')
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
            extensions=[
                AgentExtension(
                    uri=C.EXTENSION_URI,
                    description='Mem2A: negotiate before acting, listen for changes, '
                    'commit after acting.',
                    required=True,
                    params=ParseDict(params.dump(), Struct()),
                )
            ],
        ),
        security_schemes=dict(security_schemes),
        security_requirements=list(security_requirements),
        default_input_modes=['application/json'],
        default_output_modes=['application/json', 'text/plain'],
        skills=[
            AgentSkill(
                id='negotiate',
                name='Negotiate',
                description='Before acting, send an intent (what, which things, for '
                'whom). Get back a dossier, a question, or a refusal.',
                tags=['mem2a', 'memory'],
                input_modes=[C.INTENT, C.ANSWER],
                output_modes=[C.DOSSIER, C.QUESTION, C.ERROR],
            ),
            AgentSkill(
                id='commit',
                name='Commit',
                description='After acting, report what you did against the dossier '
                'version you relied on.',
                tags=['mem2a', 'memory'],
                input_modes=[C.COMMIT],
                output_modes=[C.RECEIPT, C.ERROR],
            ),
        ],
    )


def mem2a_params(card: AgentCard) -> ExtensionParams | None:
    """The Mem2A params in an Agent Card, or None if it is not a Mem2A memory.

    Raises:
        mem2a.validation.SchemaValidationError: the params break the schema.
    """
    for extension in card.capabilities.extensions:
        if extension.uri == C.EXTENSION_URI:
            return ExtensionParams.parse(MessageToDict(extension.params))
    return None
