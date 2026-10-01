# Changelog

## 0.1 (draft, unreleased)

### Specification

- First draft of the specification, as an A2A v1.0 extension with URI `https://w3id.org/mem2a/v0.1`.
- JSON Schemas for the eight payloads (intent, answer, commit, dossier, question, update, receipt, error) and the extension's Agent Card params.
- Worked examples: the Acme quote, the Titan leadership update, a refusal, a claim reaching another agent on a stream, and the read, callback and cancel calls.
- Revised after an outside review:
  - tasks are bound to the agent and principal that opened them;
  - stored dossiers are filtered by current access on every read;
  - claims can never lift a rule;
  - limits on push delivery;
  - a table of how memory handles each message;
  - the `working`, `reauth` and `failed` phases;
  - `inReplyTo` on replies;
  - idempotent retries;
  - versions on every item;
  - no numbers in payloads;
  - updates treated as signals, with agents reading before acting.

### Reference implementation (Python)

- Memory engine, A2A server and agent client on a2a-sdk 1.2, with the Acme demo.
- `mem2a serve`: a seeded sandbox memory with optional development admin routes.
- `Mem2AClient.watch()`, which yields each dossier with the update that produced it.
- `mem2a-conform`: a black-box conformance checker for any running memory.
- A standalone sales agent example.

### Project

- Contributor guide, governance, security policy, threat model, implementer's guide and wire quickstart.
- Issue templates for use cases, failure modes, questions, design partners, spec changes and interop reports.
- Starter issues for every open question, and good first issues.
