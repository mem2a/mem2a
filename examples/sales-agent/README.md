# A sales agent that waits for memory

`agent.py` is a small acting agent built on `Mem2AClient`. It acts for Tom: it tells memory it is about to send Acme a renewal quote, holds while the dossier carries a `must` constraint, sends the quote the moment memory says the constraint lifted, and commits what it did against the dossier version it relied on.

## Run it in three terminals

From the repository root, after `pip install -e "python[dev]"`:

1. Start a sandbox memory seeded with the Acme story, with its admin routes on:

   ```sh
   mem2a serve --seed acme --dev-admin
   ```

2. Run the agent. It negotiates, gets dossier 12 ("Hold: Do not send Acme new pricing until legal clears it."), and waits:

   ```sh
   python examples/sales-agent/agent.py
   ```

3. Play legal and clear the pricing:

   ```sh
   curl -X POST http://127.0.0.1:8000/dev/scenarios/acme/legal-clears
   ```

   The answer lists the tasks that got an update: `{"ok": true, "updatedTasks": ["<the agent's task id>"]}`.

The agent hears about it right away, sends the quote and commits:

```
sales-agent: acting for user:tom against http://127.0.0.1:8000
Asking memory first: Send Acme a renewal quote.
Dossier 12: Hold: Do not send Acme new pricing until legal clears it.
Holding the quote: Do not send Acme new pricing until legal clears it. Waiting for memory...
Memory called back: Legal cleared Acme pricing.
  Dossier 13: added fact f-340, removed fact f-311, removed constraint c-17, removed precedent p-4.
Nothing blocks the quote now. Sending it.
Committed against dossier 13: receipt cm-1, f-341 (claim).
```

`curl http://127.0.0.1:8000/dev/state` shows the result: `f-311` retired, `f-340` confirmed, and Tom's report recorded as `f-341`, a claim until someone confirms it (`POST /dev/facts/f-341/confirm` with `{"by": "system:crm"}`).

## Or all at once

```sh
python examples/sales-agent/agent.py --self-test
```

starts a sandbox memory on a free port, runs the agent, and makes the same `legal-clears` request once the agent is waiting. It exits 0 when the agent has committed.

## What to copy

- `memory.watch(task.id)` yields `(dossier, update)`: the current dossier first, then every new version. The agent holds while `blocking(dossier)` is non-empty and acts on the first dossier without `must` constraints.
- `memory.commit(...)` returns, rather than raises, when memory refuses a commit. On `stale-dossier` (memory changed after the agent's last dossier), the agent commits again against the current dossier and lists, in `conflicts`, the rules its action already went against.
- Options: `--url` (default `http://127.0.0.1:8000`) and `--token` (default Tom's sandbox token, `dev:sales-assistant:user:tom:group:sales`).
