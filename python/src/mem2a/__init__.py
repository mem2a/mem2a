# SPDX-License-Identifier: Apache-2.0
"""Mem2A v0.1 reference implementation for Python, on a2a-sdk 1.2.x.

* `mem2a.engine`: the memory (facts, permissions, precedent, constraints,
  watches, claims), independent of any transport.
* `mem2a.server`: serves an engine as an A2A JSON-RPC agent.
* `mem2a.client`: a small client for acting agents.
* `mem2a.models` and `mem2a.validation`: the payloads and their JSON Schemas.
"""

from mem2a.auth import AuthenticationError, Authenticator, DevTokenAuthenticator, Identity
from mem2a.card import build_agent_card, mem2a_params
from mem2a.client import (
    Mem2AClient,
    PushTarget,
    dossier_of,
    error_of,
    parse_push,
    phase_of,
    question_of,
    receipt_of,
    state_of,
    text_of,
    update_of,
)
from mem2a.constants import EXTENSION_URI, PHASE_KEY, SPEC_VERSION
from mem2a.engine import MemoryEngine, Relevance, Reply
from mem2a.server import Mem2AServer, create_app


__version__ = '0.1.0.dev0'

__all__ = [
    'EXTENSION_URI',
    'PHASE_KEY',
    'SPEC_VERSION',
    'AuthenticationError',
    'Authenticator',
    'DevTokenAuthenticator',
    'Identity',
    'Mem2AClient',
    'Mem2AServer',
    'MemoryEngine',
    'PushTarget',
    'Relevance',
    'Reply',
    'build_agent_card',
    'create_app',
    'dossier_of',
    'error_of',
    'mem2a_params',
    'parse_push',
    'phase_of',
    'question_of',
    'receipt_of',
    'state_of',
    'text_of',
    'update_of',
]
