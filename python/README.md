# mem2a for Python

The reference implementation of [Mem2A v0.1](../spec/v0.1/mem2a.md): a memory server and a small client for acting agents, built on the [A2A Python SDK](https://github.com/a2aproject/a2a-python) 1.2.

- **Memory** (`mem2a.engine`, `mem2a.server`): an in-memory company memory served as an A2A JSON-RPC agent. It answers intents with versioned dossiers, asks questions back, pushes updates while tasks are open, and records commits as claims.
- **Agent** (`mem2a.client`): `Mem2AClient` negotiates, answers, commits, subscribes and cancels, and a few helpers read dossiers, updates and receipts out of tasks, stream events and push bodies.

Status: a draft for a draft spec. The engine keeps everything in memory, the bundled authenticator trusts unsigned development tokens, and agents whose access is narrower than their principal's ([spec 10.3](../spec/v0.1/mem2a.md#10-identity-and-permissions)) aren't modelled yet.

## Install

From the repository root:

```sh
pip install -e "python[dev]"
python examples/acme-quote/demo.py    # the Acme story, end to end, in about a second
pytest python/tests
```

Python 3.10 or later.

## Run a memory

Seed a memory with what the company knows, then serve it:

```python
# memory.py
import uvicorn

from mem2a import DevTokenAuthenticator, MemoryEngine, create_app

memory = MemoryEngine()
memory.add_source('meeting:legal-weekly', kind='meeting', readers=['group:sales', 'group:legal'])
memory.add_fact(
    'f-311',
    'Legal paused new pricing for Acme, pending a contract review.',
    source='meeting:legal-weekly',
    confirmed_by='user:general-counsel',
    entities=['account:acme'],
)
memory.add_policy(
    'c-17',
    'Do not send Acme new pricing until legal clears it.',
    level='must',
    basis=['f-311'],  # applies while f-311 is a confirmed fact the principal can see
    actions=['send_quote'],
)
server = create_app(memory, url='http://127.0.0.1:8000', authenticator=DevTokenAuthenticator())
uvicorn.run(server.app, host='127.0.0.1', port=8000)
```

The Agent Card is at `http://127.0.0.1:8000/.well-known/agent-card.json` and the JSON-RPC endpoint is at `/a2a/jsonrpc`. `python -m mem2a.server` runs an empty memory.

## Point an agent at it

```python
# agent.py
import asyncio

from mem2a import DevTokenAuthenticator, Mem2AClient, dossier_of, receipt_of

TOKEN = DevTokenAuthenticator.token('sales-assistant', 'user:tom', ['group:sales'])


async def main() -> None:
    async with await Mem2AClient.connect('http://127.0.0.1:8000', token=TOKEN) as memory:
        intent = {
            'action': 'send_quote',
            'summary': 'Send Acme a renewal quote',
            'entities': ['account:acme'],
            'onBehalfOf': 'user:tom',
        }
        task = await memory.negotiate(intent)
        dossier = dossier_of(task)
        print(dossier.summary)  # Hold: Do not send Acme new pricing until legal clears it.
        if any(rule.level == 'must' for rule in dossier.constraints):
            await memory.cancel(task.id)  # not acting after all: end the watch
            return
        # ...act, then report what happened against the version relied on:
        task = await memory.commit(task, {
            'basedOn': dossier.version,
            'action': 'send_quote',
            'outcome': 'done',
            'summary': 'Sent Acme the renewal quote.',
            'claims': [{'statement': 'Tom sent Acme a renewal quote.'}],
        })
        print(receipt_of(task))


asyncio.run(main())
```

To hear about changes while a task is open, pass `push=PushTarget(url, bearer=secret)` to `negotiate` (memory POSTs every event of the task to your webhook), or read `memory.subscribe(task.id)`:

```python
async for event in memory.subscribe(task.id):
    if (update := update_of(event)) is not None:
        print(update.summary, [(c.id, c.change) for c in update.changes])
```

The helpers `dossier_of`, `update_of`, `question_of`, `error_of`, `receipt_of`, `phase_of`, `state_of` and `text_of` accept a Task, a stream event, a push body (a dict), a Message or an Artifact.

## How this memory decides

`MemoryEngine` is deliberately simple, so the protocol rules are easy to see:

- **Facts** go into a dossier when they are not retired, share an entity with the intent, and the principal may read every source they derive from (`add_source(..., readers=[...])`; `None` means everyone).
- **Claims** are facts recorded from commits, never confirmed by a commit. Besides the claimant, only principals who may read every entity a claim names (`set_entity_readers`) can see it. `confirm_fact` is how a person or a system of record stands behind one.
- **Precedent** is cited when one of its `Relevance` rules matches: the intent's action or entities, an answer to a question (`add_question(..., tags=...)`), or a confirmed fact in the same dossier.
- **Constraints** come only from policies people configure (`add_policy`), based on confirmed facts, precedent, or a standing policy. Claims never produce constraints.
- **Updates**: every change (`add_fact`, `update_fact`, `retire_fact`, `confirm_fact`, `set_source_readers`, a commit, ...) re-checks the open tasks it may affect. If a dossier changes, memory replaces the artifact and sends an update; an item the principal can no longer see is `removed`, exactly like a retired one.
- **Versions** are strings from one memory-wide counter. Swap the one-line summary writer with `MemoryEngine(summarize=...)`.

Group membership comes from the credentials presented when the task started.

## Production notes

- Replace `DevTokenAuthenticator` with an `Authenticator` that verifies real delegated tokens (for example OAuth 2.0 Token Exchange, with the principal as the subject and the agent in `act`), and pass matching `security_schemes` to `build_agent_card`.
- Pass `push_url_validator=a2a.utils.push_url_validator.validate_push_notification_url` to `create_app` to screen webhook URLs.
- `create_app(validation='raise')` fails a turn rather than send a payload that breaks its schema; the default, `'log'`, sends it and logs an error. The tests and the demo use `'raise'`.

## Notes on a2a-sdk 1.2

The package pins `a2a-sdk>=1.2.1,<1.3`: the SDK has no public way to add events to an open task from outside a request, so `mem2a.server.run_internal_turn` uses one private attribute (`DefaultRequestHandlerV2._active_task_registry`). The server also works around a few gaps:

- The SDK never marks extensions as activated or sends the `A2A-Extensions` response header. `ExtensionEchoMiddleware` does.
- A follow-up that names a `taskId` but no `contextId` gets a random new `contextId`, and the task then fails. The handler fills in the task's.
- A blocking `SendMessage` returns the first INPUT_REQUIRED event on the task's event stream, which can belong to an update still being delivered. The handler runs one turn at a time per task.
- Push notifications are sent with `Content-Type: application/json` (the spec's examples show `application/a2a+json`), inline and without retries.
