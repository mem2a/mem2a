# Mem2A 0.1 examples

Worked A2A v1.0 exchanges over the JSON-RPC binding. They are non-normative, but [`conformance/`](../../../conformance) checks every Mem2A payload in them against the [schemas](../schemas) and the message rules in the [spec](../mem2a.md).

## The Acme quote

Tom's sales agent is about to send Acme a renewal quote. Example Corp's legal team paused Acme's pricing on Friday.

| File | Step | What happens |
| --- | --- | --- |
| [01-agent-card.json](01-agent-card.json) | Discover | The memory's Agent Card declares Mem2A. |
| [02-negotiate.json](02-negotiate.json) | Negotiate | Tom's agent sends its intent and leaves a push callback. Memory answers with dossier 12: hold the quote. |
| [03-listen-update.json](03-listen-update.json) | Listen | On Monday, legal clears the pricing. Memory pushes dossier 13, then an update listing what changed. |
| [04-commit-stale.json](04-commit-stale.json) | Commit | If the agent had reported against dossier 12, memory would refuse and record nothing. |
| [05-commit.json](05-commit.json) | Commit | The agent reports against dossier 13. Memory records a claim and completes the task with a receipt. |

## The Titan update

Maya's agent is about to tell leadership that project Titan will ship three weeks late.

| File | Step | What happens |
| --- | --- | --- |
| [06-question.json](06-question.json) | Negotiate | Memory asks whether the new date is funded. |
| [07-answer.json](07-answer.json) | Answer | It isn't. Memory answers with dossier 7, including a precedent the agent could never have searched for: leadership turned down an unfunded slip on another project. |

## A refusal

| File | Step | What happens |
| --- | --- | --- |
| [08-refusal.json](08-refusal.json) | Negotiate | The intent names Tom, but the credentials are Priya's. Memory refuses without revealing anything about Tom. |

Tokens, secrets and ids are placeholders.
