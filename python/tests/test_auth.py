# SPDX-License-Identifier: Apache-2.0
"""Development tokens: ``Bearer dev:<agent>:<principal>[:<group>,...]``."""

from __future__ import annotations

import pytest

from mem2a.auth import AuthenticationError, DevTokenAuthenticator, Identity


def test_parse_agent_principal_and_groups() -> None:
    identity = DevTokenAuthenticator.parse('dev:sales-assistant:user:tom:group:sales,legal')
    assert identity == Identity(
        'agent:sales-assistant', 'user:tom', frozenset({'group:sales', 'group:legal'})
    )
    assert DevTokenAuthenticator.parse('dev:bot:system:crm').groups == frozenset()


def test_token_round_trip() -> None:
    token = DevTokenAuthenticator.token('agent:bot', 'user:maya', ['group:leadership'])
    assert token == 'dev:bot:user:maya:group:leadership'
    assert DevTokenAuthenticator.parse(token).principal == 'user:maya'


@pytest.mark.parametrize(
    'token',
    ['', 'dev:', 'dev:bot', 'dev:bot:tom', 'prod:bot:user:tom', 'dev:bot:user:', 'dev:b t:user:x'],
)
def test_invalid_tokens(token: str) -> None:
    with pytest.raises(AuthenticationError):
        DevTokenAuthenticator.parse(token)


def test_can_read() -> None:
    tom = Identity('agent:x', 'user:tom', frozenset({'group:sales'}))
    assert tom.can_read(None)
    assert tom.can_read(['group:sales'])
    assert tom.can_read(['user:tom'])
    assert not tom.can_read(['group:legal'])
    assert not tom.can_read([])
