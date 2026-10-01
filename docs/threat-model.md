# Threat model

Mem2A puts one memory between every agent and what a company knows. That makes memory valuable to attackers, and it makes mistakes in memory spread. This page lists what we protect, from whom, and which rule in the [spec](../spec/v0.1/mem2a.md) does it. Ways to break it are welcome: see [SECURITY.md](../SECURITY.md).

## What we protect

- **Company knowledge:** facts, precedent and their sources, which carry their own access rules.
- **The rules:** constraints, which tell agents what they shouldn't do.
- **The shared truth:** what every agent believes is confirmed.
- **Each principal's work:** intents, drafts and tasks.
- **The company's boundary:** what leaves through webhooks and agents.

## Who we worry about

- **A careless agent:** it acts on stale information or ignores a hold.
- **A compromised agent:** prompt-injected, for example by an email it read, and following someone else's instructions.
- **A curious or malicious insider:** using an agent to see what they can't read, or to act for someone else.
- **An outside attacker:** forging callbacks, or impersonating memory.

A compromised memory, model alignment, and enforcement are out of scope (see below).

## Threats and mitigations

| Threat | Mitigation | Spec |
| --- | --- | --- |
| A dossier shows a fact derived from a source the principal can't read. | Include an item only if the principal can read every source behind it, checked at every send and every read. | [10.3](../spec/v0.1/mem2a.md#10-identity-and-permissions) |
| Something leaks indirectly: through a question, a refusal, a summary, an id in `watching` or `basis`, or an update saying why something vanished. | Every field is covered. Lost access looks exactly like retirement. | [10.4](../spec/v0.1/mem2a.md#10-identity-and-permissions), [8.3.4](../spec/v0.1/mem2a.md#83-listen) |
| What memory finds relevant reveals what it knows. | Relevance is decided using only what the principal may see. Doing this well is open. | [11.5](../spec/v0.1/mem2a.md#11-security-considerations), [#1](https://github.com/mem2a/mem2a/issues/1) |
| A compromised agent plants a "fact" that every agent then trusts. | Agent reports are claims. Only people and systems of record confirm them. | [9.3](../spec/v0.1/mem2a.md#9-facts-claims-and-receipts) |
| A compromised agent spreads instructions through memory, worm-style. | Claims never become constraints or precedent, and never lift anything. Statements are data. | [9.4–9.8](../spec/v0.1/mem2a.md#9-facts-claims-and-receipts), [11.2–11.3](../spec/v0.1/mem2a.md#11-security-considerations) |
| A claim lifts a hold, for example by "superseding" the fact a constraint rests on. | Only confirmed facts supersede. A claim's `replaces` is a hint for reviewers. | [9.8](../spec/v0.1/mem2a.md#9-facts-claims-and-receipts), [8.4.5](../spec/v0.1/mem2a.md#84-commit) |
| An agent launders what it read into a claim that more people can see. | A claim is visible only to those who could see the entities it names and everything behind the dossier it was based on. | [10.5](../spec/v0.1/mem2a.md#10-identity-and-permissions) |
| An agent acts for someone it isn't acting for. | `onBehalfOf` must match the authenticated principal, and access narrows along delegation chains. | [10.2](../spec/v0.1/mem2a.md#10-identity-and-permissions), [8.1.3](../spec/v0.1/mem2a.md#81-negotiate) |
| One agent reads, watches, cancels or commits on another principal's task. | Tasks are bound to their agent and principal for every operation. | [10.7](../spec/v0.1/mem2a.md#10-identity-and-permissions) |
| A sandboxed agent uses webhooks to send company data out, or to reach internal hosts. | Webhook origins are registered per agent, redirects aren't followed, and addresses are checked. Memory never fetches URLs on an agent's behalf. | [8.3.10](../spec/v0.1/mem2a.md#83-listen), [11.4](../spec/v0.1/mem2a.md#11-security-considerations) |
| A draft or intent prompt-injects memory's own models. | Agent text is untrusted input. Filtering happens before any model sees it. | [11.3](../spec/v0.1/mem2a.md#11-security-considerations) |
| A forged, replayed or late push makes an agent act on the wrong dossier. | Pushes are signals. Agents read the current dossier before acting and verify webhook authenticity. | [8.3.7](../spec/v0.1/mem2a.md#83-listen), [11.4](../spec/v0.1/mem2a.md#11-security-considerations) |
| An agent acts on information that changed. | Agents read before acting. Memory refuses stale commits until the agent has seen the change. | [8.3.7](../spec/v0.1/mem2a.md#83-listen), [8.4.3](../spec/v0.1/mem2a.md#84-commit) |
| An agent is pointed at a fake memory. | Memory's URL comes from configuration, and signed Agent Cards are verified. | [5.7](../spec/v0.1/mem2a.md#5-discovery), [11.6](../spec/v0.1/mem2a.md#11-security-considerations) |
| An attacker floods memory with tasks, webhooks or huge payloads. | Caps on open tasks and push configs, `maxItems` in the schemas, and watch expiry. | [11.8](../spec/v0.1/mem2a.md#11-security-considerations) |
| A task's draft reaches other principals. | Drafts are confidential to their task and never inform other answers. | [10.6](../spec/v0.1/mem2a.md#10-identity-and-permissions) |

## Out of scope, on purpose

- **An agent that ignores its dossier.** Memory carries the rules; blocking actions is the job of enforcement outside the agent's reach ([ADR 0003](../adrs/0003-memory-carries-rules-enforcement-elsewhere.md)).
- **A compromised memory.** Everything above assumes memory follows the spec. Audit logs ([11.7](../spec/v0.1/mem2a.md#11-security-considerations)) help detect when it doesn't.
- **What models do with a dossier.** Mem2A gives agents the right information; it can't make them use it well.

## Known gaps

- Relevance leakage has no testable rule yet ([#1](https://github.com/mem2a/mem2a/issues/1)).
- The identity profile is unfinished ([#11](https://github.com/mem2a/mem2a/issues/11)): token mapping, audiences and sender-constrained tokens.
- Confirmation at scale is open ([#3](https://github.com/mem2a/mem2a/issues/3)). Whoever confirms claims becomes a target.
- The reference implementation uses unsigned development tokens. Never deploy it as is.
