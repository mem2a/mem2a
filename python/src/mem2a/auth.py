# SPDX-License-Identifier: Apache-2.0
"""Who is calling: the acting agent and the principal it acts for.

Memory needs both on every request: the principal decides what memory may
show (permissions follow the source), and the agent is recorded on claims.
Plug in your own `Authenticator`, for example one that validates an OAuth 2.0
Token Exchange JWT whose subject is the principal and whose ``act`` claim
names the agent. `DevTokenAuthenticator` is for tests and demos only.
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

#: The shape of a development token.
DEV_TOKEN_FORMAT = 'dev:<agent>:<principal>[:<groups>]'
#: A full development token, for the docs and error messages.
DEV_TOKEN_EXAMPLE = 'dev:sales-assistant:user:tom:group:sales'


@dataclass(frozen=True)
class Identity:
    """An authenticated caller.

    Attributes:
        agent: The acting agent (an AgentRef), for example ``agent:sales-assistant``.
        principal: The person or system it acts for, for example ``user:tom``.
        groups: Groups the principal belongs to, for example ``group:sales``.
    """

    agent: str
    principal: str
    groups: frozenset[str] = field(default_factory=frozenset)

    @property
    def refs(self) -> frozenset[str]:
        """Every principal reference that can appear in a reader set."""
        return frozenset({self.principal, *self.groups})

    def can_read(self, readers: Iterable[str] | None) -> bool:
        """``None`` means everyone in the company; an empty set means nobody."""
        return readers is None or not self.refs.isdisjoint(readers)


class AuthenticationError(Exception):
    """Missing or invalid credentials. The server answers HTTP 401."""


class Authenticator(Protocol):
    """Turns a request into an `Identity`, or raises `AuthenticationError`."""

    async def authenticate(self, connection: HTTPConnection) -> Identity: ...


_NAME = r'[A-Za-z0-9._@~+-]+'
_REF = re.compile(rf'^{_NAME}:{_NAME}$')
_TOKEN = re.compile(rf'^dev:(?P<agent>{_NAME}):(?P<principal>{_NAME}:{_NAME})(?::(?P<groups>.+))?$')


class DevTokenAuthenticator:
    """Accepts unsigned development tokens. NOT FOR PRODUCTION.

    A token has the form ``dev:<agent>:<principal>[:<groups>]``:

    * ``<agent>`` names the acting agent; memory records it as ``agent:<agent>``.
    * ``<principal>`` is who the agent acts for, with its kind: ``user:tom``.
    * ``<groups>``, optional, is a comma-separated list of the principal's
      groups, each with its kind: ``group:sales,group:legal``.

    So ``Authorization: Bearer dev:sales-assistant:user:tom:group:sales`` is
    agent ``agent:sales-assistant`` acting for ``user:tom``, a member of
    ``group:sales``. Anyone can forge these tokens.
    """

    def __init__(self) -> None:
        logger.warning(
            'DevTokenAuthenticator is enabled: it trusts UNSIGNED tokens that anyone can '
            'forge. Use it for tests and demos only, never in production.'
        )

    async def authenticate(self, connection: HTTPConnection) -> Identity:
        header = connection.headers.get('authorization', '')
        scheme, _, token = header.partition(' ')
        if scheme.lower() != 'bearer' or not token.strip():
            raise AuthenticationError(
                f'Missing bearer token. Send Authorization: Bearer {DEV_TOKEN_EXAMPLE}'
            )
        return self.parse(token.strip())

    @staticmethod
    def parse(token: str) -> Identity:
        """Parse a development token into an `Identity`.

        Raises:
            AuthenticationError: `token` is not ``dev:<agent>:<principal>[:<groups>]``.
        """
        match = _TOKEN.match(token)
        groups = match['groups'].split(',') if match and match['groups'] else []
        if match is None or not all(_REF.match(group) for group in groups):
            raise AuthenticationError(
                f'Invalid dev token. Expected {DEV_TOKEN_FORMAT}, where <principal> has a '
                'kind and <groups> is a comma-separated list, for example '
                f'{DEV_TOKEN_EXAMPLE} or {DEV_TOKEN_EXAMPLE},group:legal.'
            )
        return Identity(
            agent=f'agent:{match["agent"]}',
            principal=match['principal'],
            groups=frozenset(groups),
        )

    @staticmethod
    def token(agent: str, principal: str, groups: Iterable[str] = ()) -> str:
        """Build a dev token: ``token('sales-assistant', 'user:tom', ['group:sales'])``."""
        agent = agent.removeprefix('agent:')
        suffix = ','.join(groups)
        return f'dev:{agent}:{principal}' + (f':{suffix}' if suffix else '')
