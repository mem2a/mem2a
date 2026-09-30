# 0004: What agents report are claims, not facts

**Status:** Accepted, 2026-09-30

## Context

Agents report what they did, and other agents need to hear about it. If those reports became facts immediately, one compromised or mistaken agent could plant a "fact" that every other agent then trusts. Self-copying prompt injections that hop from agent to agent have already been demonstrated in controlled tests; shared memory must not become their fastest route.

## Decision

- A commit's claims are recorded as facts with status `claim`, attributed to the agent, its principal, the time and the commit.
- Only a person or a system of record can confirm a claim. No commit, from any agent, counts as confirmation.
- Constraints derive only from confirmed facts, precedent or policy written by people. Never from claims.

## Consequences

- Other agents still learn about claims quickly, clearly marked as unconfirmed.
- An instruction smuggled into one agent can't become every agent's rule through memory.
- Memory has to track provenance for every fact, which it needs anyway for permissions.
- Confirming claims at scale, without a person in every loop, is left open in v0.1.
