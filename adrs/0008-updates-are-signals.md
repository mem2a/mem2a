# 0008: Updates are signals; agents read before acting

**Status:** Accepted, 2026-09-30

## Context

Memory sends updates while a task waits in `TASK_STATE_INPUT_REQUIRED`. A2A is ambiguous about whether a `SubscribeToTask` stream stays open in that state: §3.1.6 ends streams at terminal states, while the HTTP+JSON binding (§11.7) describes streams that close at interrupted states, and SDKs differ. A2A push notifications are delivered at least once, may be retried or dropped, and are authenticated with a shared secret. If every pushed dossier were authoritative, a late, replayed or forged push could steer an agent.

## Decision

- Push notifications and stream events are signals that the dossier changed.
- Before acting, an agent reads the current dossier, with `GetTask` or from a stream it has held open continuously, and acts only on that version.
- An agent whose stream closes on a task that isn't terminal subscribes again or polls.
- Memory should keep streams open, and must deliver to webhooks in order, without delaying its replies.

## Consequences

- One extra request before each action, in exchange for robustness against lost, late and forged deliveries, and against SDK differences.
- Stale commits remain the backstop: an agent that acts on an old version is refused until it has seen the change.
- The stream ambiguity is worth fixing in A2A itself ([#12](https://github.com/mem2a/mem2a/issues/12)).
