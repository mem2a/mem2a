# 0009: A task belongs to the agent and principal that opened it

**Status:** Accepted, 2026-09-30, after the spec review found the first draft didn't say so.

## Context

A2A leaves access to tasks up to each server. A memory that scoped tasks only by agent would still have conformed to the first draft, so an agent acting for both Tom and Priya could read, watch, register webhooks on, or commit to either one's task. That would leak each principal's intents and drafts, and let one principal's agent report actions on another's behalf.

## Decision

Memory binds each task to the agent and the principal that created it. Every operation on the task from anyone else (`GetTask`, `ListTasks`, `SubscribeToTask`, `CancelTask`, push config operations, follow-up messages) gets the same answer as for a task that doesn't exist. Memory doesn't draw on other principals' tasks through `referenceTaskIds` or a shared `contextId`, and stops delivering updates if the delegation behind a task is revoked.

## Consequences

- One agent serving many people keeps their work apart, even inside one agent.
- An agent can't hand a task over to another agent. If that's needed, the new agent opens its own task.
- `mem2a-conform` checks binding when given a second token.
