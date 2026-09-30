# SPDX-License-Identifier: Apache-2.0
"""Names and values fixed by the Mem2A v0.1 specification."""

from __future__ import annotations

from typing import Final, Literal


EXTENSION_URI: Final = 'https://w3id.org/mem2a/v0.1'
SPEC_VERSION: Final = '0.1'
SCHEMA_BASE: Final = f'{EXTENSION_URI}/schemas/'

#: Metadata key carried by every memory status message.
PHASE_KEY: Final = f'{EXTENSION_URI}/phase'

# ---------------------------------------------------------------- media types
INTENT: Final = 'application/vnd.mem2a.intent+json'
ANSWER: Final = 'application/vnd.mem2a.answer+json'
COMMIT: Final = 'application/vnd.mem2a.commit+json'
DOSSIER: Final = 'application/vnd.mem2a.dossier+json'
QUESTION: Final = 'application/vnd.mem2a.question+json'
UPDATE: Final = 'application/vnd.mem2a.update+json'
RECEIPT: Final = 'application/vnd.mem2a.receipt+json'
ERROR: Final = 'application/vnd.mem2a.error+json'

PayloadKind = Literal[
    'intent',
    'answer',
    'commit',
    'dossier',
    'question',
    'update',
    'receipt',
    'error',
]

#: Media type -> payload kind (also the schema file stem).
MEDIA_TYPES: Final[dict[str, PayloadKind]] = {
    INTENT: 'intent',
    ANSWER: 'answer',
    COMMIT: 'commit',
    DOSSIER: 'dossier',
    QUESTION: 'question',
    UPDATE: 'update',
    RECEIPT: 'receipt',
    ERROR: 'error',
}
MEDIA_TYPE_OF: Final[dict[PayloadKind, str]] = {
    kind: media_type for media_type, kind in MEDIA_TYPES.items()
}

#: Payloads an acting agent sends (exactly one per message).
AGENT_PAYLOADS: Final[frozenset[PayloadKind]] = frozenset(
    {'intent', 'answer', 'commit'}
)

# ------------------------------------------------------- reserved artifact ids
DOSSIER_ARTIFACT: Final = 'dossier'
RECEIPT_ARTIFACT: Final = 'receipt'

# ---------------------------------------------------------------------- phases
Phase = Literal[
    'question',  # TASK_STATE_INPUT_REQUIRED
    'awaiting-commit',  # TASK_STATE_INPUT_REQUIRED
    'committed',  # TASK_STATE_COMPLETED
    'refused',  # TASK_STATE_REJECTED
    'expired',  # TASK_STATE_CANCELED
    'canceled',  # TASK_STATE_CANCELED
]
OPEN_PHASES: Final[frozenset[str]] = frozenset({'question', 'awaiting-commit'})

# ----------------------------------------------------------------- error codes
ErrorCode = Literal[
    'invalid-intent',
    'principal-mismatch',
    'not-authorized',
    'refused',
    'invalid-answer',
    'unknown-question',
    'invalid-commit',
    'stale-dossier',
    'watch-expired',
    'internal',
]
