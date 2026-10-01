# SPDX-License-Identifier: Apache-2.0
"""Development tokens: ``dev:<agent>:<principal>[:<groups>]``."""

from __future__ import annotations

import pytest

from mem2a.auth import AuthenticationError, DevTokenAuthenticator, Identity


def test_parse_agent_principal_and_groups() -> None:
    identity = DevTokenAuthenticator.parse('dev:sales-assistant:user:tom:group:sales,group:legal')
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
    [
        '',
        'dev:',
        'dev:bot',
        'dev:bot:tom',  # the principal needs its kind
        'prod:bot:user:tom',
        'dev:bot:user:',
        'dev:b t:user:x',
        'dev:bot:user:tom:sales',  # so do groups
        'dev:bot:user:tom:group:sales,',
    ],
)
def test_invalid_tokens(token: str) -> None:
    with pytest.raises(AuthenticationError) as caught:
        DevTokenAuthenticator.parse(token)
    assert 'dev:<agent>:<principal>[:<groups>]' in str(caught.value)
    assert 'dev:sales-assistant:user:tom:group:sales' in str(caught.value)


def test_can_read() -> None:
    tom = Identity('agent:x', 'user:tom', frozenset({'group:sales'}))
    assert tom.can_read(None)
    assert tom.can_read(['group:sales'])
    assert tom.can_read(['user:tom'])
    assert not tom.can_read(['group:legal'])
    assert not tom.can_read([])
