# Mem2A: a memory-to-agent protocol

> **Status:** draft 0.1, private while we work on it. Mem2A is designed as an extension to [A2A](https://a2a-protocol.org/) v1.0.

Mem2A gives every agent in a company, whoever built it, one shared memory that it checks before it acts and reports to after it acts. Hundreds of agents can then work inside the same company without contradicting each other or stepping outside what they're allowed to do.

Each action is one A2A task:

| Step | A2A primitive | What happens |
| --- | --- | --- |
| Discover | Agent Card | Memory declares the Mem2A extension, how agents can listen for changes, and how an agent proves who it acts for. |
| Negotiate | `SendMessage` opens a task | The agent sends an **intent**. Memory answers with a versioned **dossier**, asks a question back, or refuses. |
| Listen | Push notifications or `SubscribeToTask` | While the task is open, memory sends a new dossier version whenever something the agent relied on changes. |
| Commit | `SendMessage` on the same task | The agent reports what it did against the dossier version it used. Memory records it as a claim and completes the task. |

## What's here so far

- [`spec/v0.1/schemas`](spec/v0.1/schemas): JSON Schemas for the eight Mem2A payloads (intent, answer, commit, dossier, question, update, receipt, error) and for the extension's Agent Card params.
- [`spec/v0.1/examples`](spec/v0.1/examples): worked A2A JSON-RPC exchanges. The Acme quote story (negotiate, listen, a stale commit, a commit), the Titan leadership update (a question and an answer), and a refusal.
- [`conformance`](conformance): checks that every example follows the schemas and the message rules.

```sh
pip install -r conformance/requirements.txt
pytest conformance
```

## Coming next

- `spec/v0.1/mem2a.md`: the specification text.
- `python/`: a reference memory server and agent client on the A2A Python SDK, with a runnable Acme demo.
- Docs, design decision records, contribution guide and CI.

## License

[Apache 2.0](LICENSE)
