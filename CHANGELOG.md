# Changelog

## 0.1 (draft, unreleased)

### Specification

- First draft of the specification, as an A2A v1.0 extension with URI `https://w3id.org/mem2a/v0.1`.
- JSON Schemas for the eight payloads (intent, answer, commit, dossier, question, update, receipt, error) and the extension's Agent Card params.
- Worked examples: the Acme quote, a stale Globex commit that records a conflict, the Titan leadership update, a refusal, a claim reaching another agent on a stream, and the read, callback and cancel calls.
- Revised after an outside review:
  - tasks are bound to the agent and principal that opened them;
  - stored dossiers are checked against current access before every read, and replaced with a new version when access has narrowed;
  - claims can never lift a rule;
  - limits on push delivery;
  - a table of how memory handles each message;
  - the `working`, `reauth` and `failed` phases;
  - `inReplyTo` on replies;
  - idempotent retries;
  - versions on every item;
  - no numbers in payloads;
  - updates treated as signals, with agents reading before acting.
- Clarified for readers new to A2A: a fuller terminology, a phase diagram, why Mem2A errors travel as parts, how push configs are refused, and links from the rules to the decision records behind them.

### Reference implementation (Python)

- Memory engine, A2A server and agent client on a2a-sdk 1.2, with the Acme demo.
- `mem2a serve`: a seeded sandbox memory with optional development admin routes.
- `Mem2AClient.watch()`, which yields each dossier with the update that produced it.
- `mem2a-conform`: a black-box conformance checker for any running memory, with 15 checks, including what happens when a principal loses access.
- Push notification configs are accepted only for webhook origins registered for the agent.
- [ARCHITECTURE.md](python/ARCHITECTURE.md): how the code is organized, and which function and test cover each MUST.
- A standalone sales agent example.

### Project

- Contributor guide, governance, security policy, threat model, implementer's guide and wire quickstart.
- An A2A primer, a glossary, and a suggested reading order in [docs](docs).
- A documentation site built from the repository with Material for MkDocs ([`mkdocs.yml`](mkdocs.yml)): the spec, its schemas and examples rendered as pages, published to GitHub Pages once the repository is public.
- A logo and brand assets in [`docs/assets/brand`](docs/assets/brand).
- [Who it's for](docs/ecosystem.md): what companies, agent builders, memory providers, tool platforms and security vendors each build and get.
- A [tool gateway example](examples/tool-gateway) that holds tool calls a `must` constraint forbids and reports what ran, for agents with no Mem2A code; and an open question on a gateway profile ([#23](https://github.com/mem2a/mem2a/issues/23)).
- A private contact route, through [Sentra's contact page](https://www.sentra.app/contact).
- Issue templates for use cases, failure modes, questions, design partners, spec changes and interop reports.
- Starter issues for every open question, and good first issues.
