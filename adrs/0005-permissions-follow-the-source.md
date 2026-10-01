# 0005: Permissions follow the source

**Status:** Accepted, 2026-09-30. Refined the same day: stored dossiers are replaced with a new version when access narrows, and a claim's visibility also depends on the dossier its commit was based on.

## Context

Memory learns from email, documents, meetings, tickets and systems of record, each with its own access controls. A fact memory derives from those sources can leak what the sources protect: a one-line summary of a confidential email is still confidential. Access also changes over time, and an update can leak just by saying why something disappeared.

## Decision

- Memory shows a fact, precedent or constraint only if the principal can read every source it was derived from, checked each time memory sends anything, not just when a task begins.
- Before memory serves a stored dossier (to `GetTask`, `ListTasks` or a new stream), it checks it again. If the principal can no longer see an item in it, memory first replaces the dossier with a new version without that item. Every read, and every commit, then refers to the same version.
- If the calling agent has narrower access than its principal, the narrower one applies. A vendor's agent acting for an HR manager sees only what both may read.
- When an item becomes invisible, updates list it as `removed`, exactly like a retired item.
- Questions, refusals, error messages and summaries must not reveal what the principal can't see.
- A recorded claim is visible to the principal who committed it, and otherwise only to principals who can read every entity it names and every source behind the dossier version the commit was based on. Without the second condition, an agent could launder what it read in a confidential dossier into a claim that everyone can see.
- An intent's draft is confidential to its task.

## Consequences

- The rule is conservative. It will sometimes hide a fact that would have been fine to show; we accept that trade.
- Memory implementations need source-level access tracking.
- Relevance itself can leak (asking a question can reveal what memory knows). That remains an open question.
