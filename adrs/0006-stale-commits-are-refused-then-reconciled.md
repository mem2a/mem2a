# 0006: Stale commits are refused, then reconciled

**Status:** Accepted, 2026-09-30

## Context

An agent can act on dossier 12 while dossier 13 already exists: it missed the update, or the update arrived while it was acting. Memory could accept any commit, reject it outright, or accept it with a flag.

Accepting silently records an action against information the agent never saw corrected. Rejecting outright loses the record of something that really happened in the world.

## Decision

A commit names the latest dossier version the agent has read (`basedOn`): normally the one it acted on. If that isn't the current version, memory records nothing and replies with the current version. The agent reads the current dossier and commits again, listing in `conflicts` anything in it that its action, already taken, goes against. Memory records those conflicts and routes them to a person.

## Consequences

- No action goes unrecorded, but none is recorded before the agent has seen what changed.
- Conflicts surface explicitly, instead of hiding in a stale record.
- A stale commit costs one extra round trip.
