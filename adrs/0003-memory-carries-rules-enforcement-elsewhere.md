# 0003: Memory carries the rules; enforcement lives elsewhere

**Status:** Accepted, 2026-09-30

## Context

It's tempting to make memory the place that blocks bad actions: if the dossier says "hold the quote", why not have memory stop the quote? But the agent is the one reading the dossier. Anything an agent can read, it can try to game, and a compromised agent can simply skip asking. An enforcement point has to sit where the agent can't reach it.

## Decision

In Mem2A, constraints tell an agent what it shouldn't do. They are advice with a source, not enforcement. Blocking actions is left to sandboxes, egress policy and watchdogs outside the agent's reach, which can consult the same memory.

## Consequences

- Mem2A alone won't stop a rogue agent. The spec says so plainly ([11.1](../spec/v0.1/mem2a.md#11-security-considerations)).
- The threat model is clearer: memory's job is to make sure every well-behaved agent knows the rules; enforcement's job is to handle the rest.
- Enforcement integrations (for example, a watchdog that checks commits and outbound actions against memory) are future work.
