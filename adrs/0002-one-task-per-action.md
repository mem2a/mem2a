# 0002: One task per action

**Status:** Accepted, 2026-09-30

## Context

Memory needs a unit to watch, to audit and to clean up. The alternatives were a long-lived session per agent, or standing subscriptions per entity ("tell me about anything that happens to Acme").

A session per agent mixes unrelated actions and never ends on its own. Standing subscriptions only fire for what an agent subscribed to, and an agent can't subscribe to what it doesn't know exists, which is the failure Mem2A exists to prevent.

## Decision

Each action an agent is about to take is one A2A task. The intent opens it, the commit completes it, and a cancel or an expired watch ends it. Memory watches the items in the task's dossier, plus anything new it judges relevant to the intent, for as long as the task is open.

## Consequences

- Watches are bounded by the action, and they end on their own.
- Every action leaves an audit trail: intent, dossier versions, updates and commit, in one task.
- An agent taking many actions opens many tasks. That costs some overhead, but it makes each action's context explicit.
- Agents that only want to monitor, without an action in hand, aren't served yet. That's an open question in the spec.
