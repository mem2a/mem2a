# Architecture decision records

Short records of the decisions that shape Mem2A, and why we made them. Each has a context, a decision and its consequences.

The first seven were accepted when the project started, and 0008 and 0009 after its first outside review. To challenge any of them, open an issue that links to it; a changed decision gets a new record that supersedes the old one.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-build-on-a2a.md) | Build Mem2A as an A2A extension | Accepted |
| [0002](0002-one-task-per-action.md) | One task per action | Accepted |
| [0003](0003-memory-carries-rules-enforcement-elsewhere.md) | Memory carries the rules; enforcement lives elsewhere | Accepted |
| [0004](0004-claims-are-not-facts.md) | What agents report are claims, not facts | Accepted |
| [0005](0005-permissions-follow-the-source.md) | Permissions follow the source | Accepted |
| [0006](0006-stale-commits-are-refused-then-reconciled.md) | Stale commits are refused, then reconciled | Accepted |
| [0007](0007-versions-are-opaque-strings.md) | Versions are opaque strings, and payloads have no numbers | Accepted |
| [0008](0008-updates-are-signals.md) | Updates are signals; agents read before acting | Accepted |
| [0009](0009-tasks-belong-to-their-owner.md) | A task belongs to the agent and principal that opened it | Accepted |

New records use the next number and the same layout: Context, Decision, Consequences.
