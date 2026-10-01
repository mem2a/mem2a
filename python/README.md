# mem2a for Python

The reference implementation of [Mem2A v0.1](../spec/v0.1/mem2a.md), built on the [A2A Python SDK](https://github.com/a2aproject/a2a-python) 1.2: a memory server, a client for acting agents, a sandbox to try them in, and a conformance checker for any memory.

```sh
pip install -e "python[dev]"     # from the repository root; Python 3.10 or later
```

Status: a draft for a draft spec. The memory keeps everything in process memory, and the sandbox trusts unsigned development tokens.

## Run a memory you can talk to

```sh
mem2a serve --seed acme --dev-admin
```

This starts a sandbox memory on `http://127.0.0.1:8000`, seeded with the spec's Acme story: Tom's agent is about to send Acme a renewal quote, but legal paused Acme pricing. It prints everything you need to talk to it:

```
Mem2A sandbox memory, seeded with acme: The Acme quote (spec examples 02-05, 09)

  Agent Card:  http://127.0.0.1:8000/.well-known/agent-card.json
  JSON-RPC:    http://127.0.0.1:8000/a2a/jsonrpc

Dev tokens (unsigned: for this sandbox only):
  Tom:    dev:sales-assistant:user:tom:group:sales
  Priya:  dev:account-assistant:user:priya:group:sales

Try it:

  curl -s http://127.0.0.1:8000/.well-known/agent-card.json

  curl -s http://127.0.0.1:8000/a2a/jsonrpc \
    -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
    -H 'A2A-Extensions: https://w3id.org/mem2a/v0.1' \
    -H 'Authorization: Bearer dev:sales-assistant:user:tom:group:sales' \
    -d '{"jsonrpc": "2.0", "id": "1", "method": "SendMessage", ...}'
```

The second curl negotiates as Tom and gets dossier 12: "Hold: Do not send Acme new pricing until legal clears it."

| Option | |
| --- | --- |
| `--seed acme\|titan\|empty` | What memory knows at startup (default `acme`). `titan` is the question-and-answer story of examples 06 and 07, with a token for Maya. |
| `--host`, `--port` | Default `127.0.0.1` and `8000`; `--port 0` picks a free port. |
| `--dev-admin` | Also serve the unauthenticated `/dev` admin routes ([below](#change-memory-while-tasks-are-open)). |
| `--push-origin ORIGIN` | A webhook origin memory may call, such as `http://10.0.0.5:9000` (repeatable). The default is this machine, any port. |

`python -m mem2a serve` does the same. Dev tokens have the form `dev:<agent>:<principal>[:<groups>]`: the principal has a kind (`user:tom`) and the groups are a comma-separated list (`group:sales,group:legal`), so a full token reads `dev:sales-assistant:user:tom:group:sales`. Anyone can forge them.

## Write an agent in 20 lines

```python
import asyncio
from contextlib import aclosing

from mem2a import Mem2AClient, dossier_of, error_of, receipt_of

TOM = 'dev:sales-assistant:user:tom:group:sales'
INTENT = {'action': 'send_quote', 'summary': 'Send Acme a renewal quote',
          'entities': ['account:acme'], 'onBehalfOf': 'user:tom'}


async def main() -> None:
    async with await Mem2AClient.connect('http://127.0.0.1:8000', token=TOM) as memory:
        task = await memory.negotiate(INTENT)
        async with aclosing(memory.watch(task.id)) as dossiers:
            async for dossier, update in dossiers:
                print(f'dossier {dossier.version}: {update.summary if update else dossier.summary}')
                if not any(c.level == 'must' for c in dossier.constraints):
                    break  # nothing blocks the quote any more
        # ...send the quote, then report what you did against the version you relied on:
        report = {'basedOn': dossier.version, 'action': 'send_quote', 'outcome': 'done',
                  'summary': 'Sent Acme the quote.', 'claims': [{'statement': 'Tom sent Acme a quote.'}]}
        done = await memory.commit(task, report)
        if (error := error_of(done)) and error.code == 'stale-dossier':
            current = dossier_of(done)  # memory changed since: commit against what it says now
            conflicts = [{'id': c.id, 'explanation': 'Sent before I saw this.'} for c in current.constraints]
            done = await memory.commit(task, {**report, 'basedOn': current.version, 'conflicts': conflicts})
        print(receipt_of(done))


asyncio.run(main())
```

Run it against the sandbox and it waits at dossier 12. Clear legal's hold from another terminal (`curl -X POST http://127.0.0.1:8000/dev/scenarios/acme/legal-clears`) and it moves on to dossier 13 and commits. [examples/sales-agent](../examples/sales-agent) is the same agent, fleshed out.

- `watch(task_id)` yields `(dossier, update)`: the current dossier first (`update` is None), then every new version with the update that came with it. It resubscribes if the stream drops, and stops when the task ends.
- `commit()` returns, rather than raises, when memory refuses a commit: check `error_of(task)`. On `stale-dossier`, `dossier_of(task)` is the current dossier; commit again against its version, listing in `conflicts` the items your action went against. Pass the same `message_id=` to retry a commit whose reply was lost: memory returns the first result and records nothing twice.
- Prefer a webhook to a stream? `negotiate(intent, push=PushTarget(url, bearer=secret))`, and read each POSTed body with the same helpers (`dossier_of(body)`, `update_of(body)`).

## Change memory while tasks are open

With `--dev-admin`, the sandbox lets you play the rest of the company. The routes are unauthenticated: never expose them.

| Route | Body | |
| --- | --- | --- |
| `GET /dev/state` | | Facts, precedent, policies, sources, and open tasks with their phase and dossier version |
| `POST /dev/facts` | `{id, statement, source, confirmedBy, entities, supersedes?, note?, readers?}` | Add a confirmed fact. An unknown `source` ref is created, readable by everyone unless `readers` is given (`readers` is refused for a source that exists). `note` becomes the update's summary. |
| `POST /dev/facts/{id}/retire` | `{note?}` | Retire a fact (the note is kept for people; updates never say why something was removed) |
| `POST /dev/facts/{id}/confirm` | `{by}` | Confirm a claim, as a person or system of record |
| `POST /dev/sources/{ref}/readers` | `{readers: [...]}` | Change who may read a source (`null`: everyone) |
| `POST /dev/scenarios/acme/legal-clears` | | Example 03: `f-340` supersedes `f-311`, so `c-17` and `p-4` lapse |

Each change answers `{"ok": true, "updatedTasks": [...]}`, listing the open tasks that got an update because of it. Errors answer `{"ok": false, "error": "..."}` with status 400, 404 or 409.

```sh
curl -X POST http://127.0.0.1:8000/dev/facts -H 'Content-Type: application/json' -d \
  '{"id": "f-400", "statement": "Acme asked for a two-year term.", "source": "email:acme-terms",
    "confirmedBy": "user:priya", "entities": ["account:acme"], "note": "Acme wants two years."}'
```

When you embed the memory, the same changes are `MemoryEngine` calls. Make them on the server's event loop: open tasks they affect get their update as soon as you return to it.

```python
import uvicorn

from mem2a import LOCALHOST_ORIGINS, DevTokenAuthenticator, MemoryEngine, When, create_app

memory = MemoryEngine()
memory.add_source('meeting:legal-weekly', kind='meeting', readers=['group:sales', 'group:legal'])
memory.add_fact('f-311', 'Legal paused new pricing for Acme.', source='meeting:legal-weekly',
                confirmed_by='user:general-counsel', entities=['account:acme'])
memory.add_source('decision:globex', kind='decision')
memory.add_precedent('p-4', 'A Globex quote sent during a legal review had to be withdrawn.',
                     relevance='Same situation.', source='decision:globex',
                     when=[When(actions={'send_quote'}, facts={'f-311'})])
memory.add_policy('c-17', 'Do not send Acme new pricing until legal clears it.', level='must',
                  basis=['f-311'], actions=['send_quote'])
memory.set_entity_readers('account:acme', ['group:sales', 'group:legal'])  # who sees claims

server = create_app(memory, url='http://127.0.0.1:8000', authenticator=DevTokenAuthenticator(),
                    push_origins=LOCALHOST_ORIGINS)
uvicorn.run(server.app, host='127.0.0.1', port=8000)
# Later: memory.retire_fact('f-311'), memory.confirm_fact(claim_id, by='system:crm'), ...
```

### How this memory decides

`MemoryEngine` is deliberately simple, so the protocol rules are easy to see:

- **Facts** go into a dossier when they are not retired, share an entity with the intent, and the principal may read every source they derive from (`add_source(..., readers=[...])`; `None` means everyone). Confirmed facts come first, then claims.
- **Claims** are facts recorded from commits; a commit never confirms anything. Besides the claimant, a principal sees a claim only if it may read every entity the claim names (`set_entity_readers`) and see every item of the dossier the commit was based on, so an agent can't launder what it read into a claim more people can see. `confirm_fact` is how a person or a system of record stands behind one. A claim's `replaces` is a hint for reviewers; memory never retires anything because of it.
- **Precedent** is cited, with its `relevance`, when one of its `When` conditions matches: the intent's action or entities, answers to questions (`add_question(..., tags=...)`), or a confirmed fact in the same dossier.
- **Constraints** come only from policies people configure (`add_policy`): based on confirmed facts and precedent in the dossier (all of them), or on a standing policy. Claims never produce constraints.
- **Versions**: every fact, precedent and constraint has a version, bumped when it changes. A dossier gets a new version (memory-wide counter, a string) only when its items or their versions change; wording alone never does.
- **Updates**: every change (`add_fact`, `update_fact`, `retire_fact`, `confirm_fact`, `set_source_readers`, a commit, ...) re-checks the open tasks it may affect. An item the principal can no longer see is `removed`, exactly like a retired one.
- **Reads** (GetTask, ListTasks, stream snapshots) filter stored dossiers by current access, in any task state, so a finished task never shows what its principal has since lost access to.
- **Limits**: 100 open tasks per principal (`max_open_tasks`) and 5 push configs per task (`max_push_configs`); a `commit_policy` can refuse commits (`commit-refused`).

## API

| | |
| --- | --- |
| `MemoryEngine(watch_timeout=, first_version=, max_open_tasks=, commit_policy=, summarize=, clock=)` | The memory. Content: `add_source`, `set_source_readers`, `set_entity_readers`, `restrict_action`, `add_fact`, `update_fact`, `retire_fact`, `confirm_fact`, `add_precedent` (with `When`), `update_precedent`, `retire_precedent`, `add_policy`, `update_policy`, `retire_policy`, `add_question`, `add_refusal`. Inspection: `facts()`, `claims()`, `precedents()`, `policies()`, `sources()`, `open_tasks()`, `task(id)`, `review_queue`. |
| `create_app(engine, url=, authenticator=, push_origins=, push_url_validator=, max_push_configs=, validation=, sweep_interval=, dev_admin=)` | Serve it over A2A JSON-RPC. Returns a `Mem2AServer`: `.app` (ASGI), `.engine`, `await .wait_idle()`, `await .sweep()`. |
| `await Mem2AClient.connect(url, token=)` | For acting agents: `negotiate(intent, push=)`, `answer(task, question_id, text)`, `commit(task, commit, message_id=)`, `watch(task_id)`, `subscribe(task_id)`, `get(task_id)`, `cancel(task_id)`, `add_push(task_id, target)`, `close()`. |
| `dossier_of`, `update_of`, `question_of`, `error_of`, `receipt_of`, `phase_of`, `state_of`, `text_of` | Read a Task, a stream event, a Message or Artifact, or the JSON of any of them (a push body, a raw JSON-RPC result). |
| `mem2a.models` | The payloads as pydantic models: `Intent.parse(data)` validates against the schema; `.dump()` gives the wire JSON. |
| `mem2a.seeds` | The stories: `seeded_engine('acme')`, `legal_clears(engine)`, and the dev tokens and intents of Tom, Priya and Maya. |
| `mem2a.auth` | `Identity`, the `Authenticator` protocol, and `DevTokenAuthenticator`. |

## Test your own memory

`mem2a-conform` checks any running memory from the outside, over raw JSON-RPC. It validates every payload against the schemas, with formats enforced, and reports each check with its spec section:

```sh
mem2a-conform --url http://127.0.0.1:8000 \
  --token dev:sales-assistant:user:tom:group:sales --principal user:tom \
  --entity account:acme --action send_quote \
  --other-token dev:account-assistant:user:priya:group:sales --allow-writes --dev-admin
```

```
PASS  5           card                required; listen push+subscribe; watchTimeout P7D
PASS  6.3         activation          -32008 without the header; no task left behind
PASS  8.1.3       refusals            invalid-intent and principal-mismatch, both REJECTED with phase refused
PASS  8.1         negotiate           dossier 12, inReplyTo set; the dossier artifact precedes its status
...
14 passed, 0 failed, 0 skipped
```

By default it records nothing: it opens tasks, sends commits a conforming memory must refuse, and cancels what it opened. `--other-token` adds the task-binding check, `--allow-writes` commits a test claim and retries it, and `--dev-admin` changes a fact through `/dev/facts` to check that updates reach a webhook and a stream (memory must be able to call this machine). `--json` prints machine-readable results; the exit code is 1 if any check fails. `python -m mem2a conform` is the same command.

## Production notes

- Replace `DevTokenAuthenticator` with an `Authenticator` that verifies real delegated tokens (for example OAuth 2.0 Token Exchange, with the principal as the subject and the agent in `act`), and pass matching `security_schemes` to `build_agent_card`.
- Webhooks: by default memory refuses push URLs that resolve to private or loopback addresses. Pass `push_origins=[...]` to allow exactly the origins your agents use. Push notifications go out in the background, in order per webhook, with up to 3 retries and no redirects.
- `create_app(validation='raise')` fails a turn rather than send a payload that breaks its schema; the default, `'log'`, sends it and logs an error. Any error inside a turn ends the task as FAILED (phase `failed`, error `internal`); the details stay in the server log.
- The engine keeps everything in process memory and runs on the server's event loop. A real memory would persist facts, tasks and push configs (the SDK's task and config stores are pluggable).

## Notes on a2a-sdk 1.2

The package pins `a2a-sdk>=1.2.1,<1.3`: the SDK has no public way to add events to an open task from outside a request, so `mem2a.server.run_internal_turn` uses one private attribute (`DefaultRequestHandlerV2._active_task_registry`). The server also works around a few gaps:

- The SDK never marks extensions as activated or sends the `A2A-Extensions` response header. `ExtensionEchoMiddleware` does.
- A follow-up that names a `taskId` but no `contextId` gets a random new `contextId`, and the task then fails. The handler fills in the task's.
- A blocking `SendMessage` returns the first INPUT_REQUIRED event on the task's event stream, which can belong to an update still being delivered. The handler runs one turn at a time per task.
- A message to a finished task gets -32004 before an agent executor sees it, so retried commits are answered in the handler, from the task's history.
- The SDK's push sender posts inline (a slow webhook delays the reply), with `Content-Type: application/json` and no retries. `QueuedPushSender` replaces it.
- An exception in an executor reaches the client as -32603 with the exception's text. The executor catches everything and fails the task instead, and the handler turns other errors into a generic internal error.
- A message with no parts at all is refused with -32602 by the SDK's own validation, before Mem2A sees it.
