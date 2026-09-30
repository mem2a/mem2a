# SPDX-License-Identifier: Apache-2.0
"""Who is calling: the acting agent and the principal it acts for.

Memory needs both on every request: the principal decides what memory may
show (permissions follow the source), and the agent is recorded on claims.
Plug in your own `Authenticator` (for example one that validates an OAuth
2.0 token-exchange JWT naming both). `DevTokenAuthenticator` is for tests and
demos only.
"""

from __future__ import annotations

import logging
import re

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """An authenticated caller.

    Attributes:
        agent: The acting agent (AgentRef), e.g. ``agent:sales-assistant``.
        principal: The person or system it acts for, e.g. ``user:tom``.
        groups: Groups the principal belongs to, e.g. ``group:sales``.
    """

    agent: str
    principal: str
    groups: frozenset[str] = field(default_factory=frozenset)

    @property
    def refs(self) -> frozenset[str]:
        """Every principal reference that can appear in a reader set."""
        return frozenset({self.principal, *self.groups})

    def can_read(self, readers: Iterable[str] | None) -> bool:
        """``None`` means "everyone in the company"."""
        return readers is None or not self.refs.isdisjoint(readers)


class AuthenticationError(Exception):
    """Missing or invalid credentials. The server answers HTTP 401."""


class Authenticator(Protocol):
    """Turns a request into an `Identity`, or raises `AuthenticationError`."""

    async def authenticate(self, connection: HTTPConnection) -> Identity: ...


_NAME = re.compile(r'^[A-Za-z0-9._@~+-]+$')


class DevTokenAuthenticator:
    """Accepts unsigned development tokens. NOT FOR PRODUCTION.

    Format: ``Authorization: Bearer dev:<agent>:<principal>[:<group>,<group>...]``
    where ``<principal>`` is ``<kind>:<id>``, for example::

        Bearer dev:sales-assistant:user:tom:group:sales,group:legal

    names agent ``agent:sales-assistant`` acting for ``user:tom``, a member of
    ``group:sales`` and ``group:legal``. Bare group names get ``group:``.
    Anyone can forge these tokens.
    """

    def __init__(self) -> None:
        logger.warning(
            'DevTokenAuthenticator is enabled: it trusts UNSIGNED tokens that '
            'anyone can forge. Use it for tests and demos only, never in '
            'production.'
        )

    async def authenticate(self, connection: HTTPConnection) -> Identity:
        header = connection.headers.get('authorization', '')
        scheme, _, token = header.partition(' ')
        if scheme.lower() != 'bearer' or not token.strip():
            raise AuthenticationError('Missing bearer token.')
        return self.parse(token.strip())

    @staticmethod
    def parse(token: str) -> Identity:
        """Parse ``dev:<agent>:<kind>:<id>[:<groups>]`` into an `Identity`."""
        prefix, _, rest = token.partition(':')
        agent, _, rest = rest.partition(':')
        kind, _, rest = rest.partition(':')
        principal_id, _, group_list = rest.partition(':')
        if prefix != 'dev' or not all(
            _NAME.match(part) for part in (agent, kind, principal_id)
        ):
            raise AuthenticationError(
                'Invalid dev token; expected dev:<agent>:<kind>:<id>[:<groups>].'
            )
        groups = set()
        for group in filter(None, (g.strip() for g in group_list.split(','))):
            ref = group if ':' in group else f'group:{group}'
            if not all(_NAME.match(part) for part in ref.split(':', 1)):
                raise AuthenticationError(f'Invalid group in dev token: {group!r}')
            groups.add(ref)
        return Identity(
            agent=f'agent:{agent}',
            principal=f'{kind}:{principal_id}',
            groups=frozenset(groups),
        )

    @staticmethod
    def token(agent: str, principal: str, groups: Iterable[str] = ()) -> str:
        """Build a dev token, e.g. ``token('sales-assistant', 'user:tom', ['group:sales'])``."""
        agent = agent.removeprefix('agent:')
        suffix = ','.join(groups)
        return f'dev:{agent}:{principal}' + (f':{suffix}' if suffix else '')
