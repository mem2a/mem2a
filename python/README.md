# mem2a for Python

The reference implementation of [Mem2A v0.1](../spec/v0.1/mem2a.md), built on the [A2A Python SDK](https://github.com/a2aproject/a2a-python) 1.2: a memory server, a client for acting agents, a sandbox to try them in, and a conformance checker for any memory. [ARCHITECTURE.md](ARCHITECTURE.md) explains how the code works.

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
| `--push-origin ORIGIN` | Register a webhook origin for every agent, such as `http://10.0.0.5:9000`, or `http://10.0.0.5:*` for any port (repeatable). It replaces the default: any port on this machine (`127.0.0.1`, `localhost` and `[::1]`, over http or https). Memory sends push notifications to registered origins only. |

`python -m mem2a serve` does the same.

**Dev tokens.** The sandbox accepts bearer tokens of exactly this form: `dev:<agent>:<principal>[:<groups>]`. `<agent>` is a name (memory records it as `agent:<agent>`), `<principal>` has a kind (`user:tom`), and the optional `<groups>` is a comma-separated list of groups, each with a kind (`group:sales,group:legal`). So `dev:sales-assistant:user:tom:group:sales` is Tom's sales assistant, acting for Tom, who is in `group:sales`. Anyone can forge these tokens: never use them outside a sandbox.

## Write an agent

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

- `watch(task_id)` yields `(dossier, update)`: the current dossier first (`update` is None), then every new version with the update that came with it. It holds a `SubscribeToTask` stream open, resubscribes if the stream drops (the new stream starts with the task as it is now), and stops when the task ends. Acting on what it yields is reading before acting ([spec 8.3.7](../spec/v0.1/mem2a.md#83-listen)).
- `commit()` returns, rather than raises, when memory refuses a commit: check `error_of(task)`. On `stale-dossier`, `dossier_of(task)` is the current dossier; commit again against its version, listing in `conflicts` the items your action went against. Pass the same `message_id=` to retry a commit whose reply was lost: memory returns the first result and records nothing twice.
- Prefer a webhook to a stream? Register it with `negotiate(intent, push=PushTarget(url, bearer=secret))`; the sandbox accepts webhooks on this machine. A push is only a signal that the dossier changed: before acting, call `task = await memory.get(task.id)` and act on `dossier_of(task)`, never on the dossier in a POST body. `update_of(body)` is handy for logging what changed.

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
- **Constraints** come only from policies, which only people configure (`add_policy`). A policy yields the constraint with its id, in every dossier it applies to: when all of its basis items, confirmed facts or precedent, are in the dossier, or always, for a standing policy. In the Acme story, constraint `c-17` comes from such a policy, based on `f-311`. Claims never produce constraints.
- **Versions**: every fact, precedent and constraint has a version, bumped when it changes. A dossier gets a new version (memory-wide counter, a string) only when its items or their versions change; wording alone never does.
- **Updates**: every change (`add_fact`, `update_fact`, `retire_fact`, `confirm_fact`, `set_source_readers`, a commit, ...) re-checks the open tasks it may affect. An item the principal can no longer see is `removed`, exactly like a retired one.
- **Access as of each request** ([spec 10.3](../spec/v0.1/mem2a.md#10-identity-and-permissions)): a task keeps the groups of the latest request for it from its own agent and principal. When a request (a read, a commit, any operation on the task) carries other groups, memory adopts them and re-evaluates the task before answering: an open task gets an ordinary new version and an update; a finished task gets a new stored dossier, with no event. A change of who may read a source does the same to every task it affects. So `GetTask`, `ListTasks`, a stream's first event and a commit always agree on one current version.
- **Questions and watches expire**: with a `watch_timeout` (the card's `watchTimeout`), each question and each dossier carries `expiresAt`, and an open task left past it ends with phase `expired`.
- **Limits**: 100 open tasks per principal (`max_open_tasks`) and 5 push configs per task (`max_push_configs`); a `commit_policy` can refuse commits (`commit-refused`).
- **Payloads from newer versions**: memory ignores members this version doesn't define and validates the rest ([spec 2.5](../spec/v0.1/mem2a.md#2-conventions)); so does the client. What memory sends has only the members the schemas define.

## API

| | |
| --- | --- |
| `MemoryEngine(watch_timeout=, first_version=, max_open_tasks=, commit_policy=, summarize=, clock=)` | The memory. Content: `add_source`, `set_source_readers`, `set_entity_readers`, `restrict_action`, `add_fact`, `update_fact`, `retire_fact`, `confirm_fact`, `add_precedent` (with `When`), `update_precedent`, `retire_precedent`, `add_policy`, `update_policy`, `retire_policy`, `add_question`, `add_refusal`. Inspection: `facts()`, `claims()`, `precedents()`, `policies()`, `sources()`, `open_tasks()`, `tasks()`, `task(id)`, `review_queue`. |
| `create_app(engine, url=, authenticator=, ...)` | Serve it over A2A JSON-RPC ([options below](#create_app-options)). Returns a `Mem2AServer`: `.app` (ASGI), `.engine`, `.card`, `await .wait_idle()`, `await .sweep()`. |
| `await Mem2AClient.connect(url, token=)` | For acting agents: `negotiate(intent, push=)`, `answer(task, question_id, text)`, `commit(task, commit, message_id=)`, `watch(task_id)`, `subscribe(task_id)`, `get(task_id)`, `cancel(task_id)`, `add_push(task_id, target)`, `close()`. |
| `dossier_of`, `update_of`, `question_of`, `error_of`, `receipt_of`, `phase_of`, `state_of`, `text_of` | Read Mem2A payloads from a Task or a stream event, as an object or as JSON (a push body is a stream event as JSON; so is a raw `SendMessage` result). Members a newer memory adds are ignored. |
| `mem2a.models` | The payloads as pydantic models: `Intent.parse(data)` drops unknown members and validates the rest against the schema; `.dump()` gives the wire JSON. |
| `mem2a.seeds` | The stories: `seeded_engine('acme')`, `legal_clears(engine)`, and the dev tokens and intents of Tom, Priya and Maya. |
| `mem2a.auth` | `Identity`, the `Authenticator` protocol, and `DevTokenAuthenticator`. |

### `create_app` options

| Option | Default | |
| --- | --- | --- |
| `engine` | | The `MemoryEngine` to serve. |
| `url` | (required) | The public base URL, used in the Agent Card. |
| `authenticator` | (required) | Turns each request into an `Identity`: agent, principal, groups (see `mem2a.auth`). |
| `card` | `build_agent_card(url + rpc_path, watch_timeout=engine.watch_timeout)` | The Agent Card to serve. Pass your own to set the name, description, entity types or security schemes. |
| `rpc_path` | `/a2a/jsonrpc` | Where the JSON-RPC endpoint lives. |
| `push_origins` | `None` | The webhook origins registered for agents ([spec 8.3.10](../spec/v0.1/mem2a.md#83-listen)): a list, for every agent, or a mapping from agent (as authenticated, such as `agent:sales-assistant`, or `*` for every agent) to its origins. An origin is `scheme://host[:port]`; the port may be `*`. With nothing registered for the calling agent, memory refuses its push configs with InvalidParams, and the agent listens with a stream instead. `LOCALHOST_ORIGINS` registers this machine, for sandboxes and demos. |
| `push_url_validator` | the SDK's `validate_push_notification_url` | Checks the address a registered host name resolves to, at registration and again before each delivery. The default refuses names that resolve to private, loopback or link-local addresses. Hosts given as an address (an IP literal, or `localhost`) skip the check. `None` skips it always. |
| `push_client` | an `httpx.AsyncClient` | Delivers push notifications. The app closes it on shutdown. |
| `push_retries`, `push_backoff` | `3`, `0.25` | Retries per push notification, and the first backoff in seconds (it doubles each time). |
| `max_push_configs` | `5` | Push notification configs per task. More get InvalidParams (`-32602`) with a message that starts `limit-exceeded:`. |
| `validation` | `'log'` | What to do when a payload memory is about to send breaks its schema: `'raise'` (the turn fails), `'log'` (send it and log an error) or `'off'`. |
| `sweep_interval` | `60.0` | Seconds between sweeps that expire tasks past their `expiresAt`. `None` disables them (call `await server.sweep()` yourself). |
| `dev_admin` | `False` | Also serve the unauthenticated `/dev` admin routes. Sandboxes only. |

## Test your own memory

`mem2a-conform` checks any running memory from the outside, over raw JSON-RPC. It validates every payload against the schemas, with formats enforced, and reports each check with its spec section:

```sh
mem2a-conform --url http://127.0.0.1:8000 \
  --token dev:sales-assistant:user:tom:group:sales --principal user:tom \
  --entity account:acme --action send_quote \
  --other-token dev:account-assistant:user:priya:group:sales --allow-writes --dev-admin
```

```
PASS  5             card                required; listen push+subscribe; watchTimeout P7D
PASS  6.3           activation          -32008 without the header; no task left behind
PASS  8.1.3         refusals            invalid-intent and principal-mismatch, both REJECTED with phase refused
PASS  8.1           negotiate           dossier 12, inReplyTo set; the dossier artifact precedes its status
...
PASS  8.3.4, 10.3   access              unreadable source: its fact removed in dossier 20 (was 19)
PASS  7             payloads            55 Mem2A payloads valid; message rules hold

15 passed, 0 failed, 0 skipped
```

By default it records nothing: it opens tasks, sends commits a conforming memory must refuse, and cancels what it opened. `--other-token` adds the task-binding check, and `--allow-writes` commits a test claim and retries it. `--dev-admin` uses the sandbox's `/dev` routes to check two things: that a new fact reaches both a webhook and a stream (memory must accept a webhook on this machine), and that a fact whose source the principal can no longer read is `removed` in an update with a new version. `--json` prints machine-readable results; the exit code is 1 if any check fails. `python -m mem2a conform` is the same command.

## Production notes

- Replace `DevTokenAuthenticator` with an `Authenticator` that verifies real delegated tokens (for example OAuth 2.0 Token Exchange, with the principal as the subject and the agent in `act`), and pass a matching `card` (`build_agent_card(..., security_schemes=...)`).
- Webhooks: register each agent's webhook origins with `push_origins`; memory refuses push configs for anything else, and by default checks that a registered host name doesn't resolve to a private address. Push notifications go out in the background, in order per webhook, with up to 3 retries and no redirects. The check runs just before each delivery, but the HTTP client resolves the name again when it connects: pin the address in `push_client` if DNS rebinding matters to you.
- `create_app(validation='raise')` fails a turn rather than send a payload that breaks its schema; the default, `'log'`, sends it and logs an error. Any error inside a turn ends the task as FAILED (phase `failed`, error `internal`); the details stay in the server log.
- The engine keeps everything in process memory and runs on the server's event loop. A real memory would persist facts, tasks and push configs (the SDK's task and config stores are pluggable).

## Notes on a2a-sdk 1.2

The package pins `a2a-sdk>=1.2.1,<1.3`: the SDK has no public way to add events to an open task from outside a request, so `mem2a.server.run_internal_turn` uses one private attribute (`DefaultRequestHandlerV2._active_task_registry`). [ARCHITECTURE.md](ARCHITECTURE.md#the-internal-turn) explains how. The server also works around a few gaps:

- The SDK never marks extensions as activated or sends the `A2A-Extensions` response header. `ExtensionEchoMiddleware` does.
- A follow-up that names a `taskId` but no `contextId` gets a random new `contextId`, and the task then fails. The handler fills in the task's.
- A blocking `SendMessage` returns the first INPUT_REQUIRED event on the task's event stream, which can belong to an update still being delivered. The handler runs one turn at a time per task.
- A message to a finished task gets -32004 before an agent executor sees it, so retried commits are answered in the handler, from the task's history.
- The SDK's push sender posts inline (a slow webhook delays the reply), with `Content-Type: application/json` and no retries. `QueuedPushSender` replaces it.
- An exception in an executor reaches the client as -32603 with the exception's text. The executor catches everything and fails the task instead, and the handler turns other errors into a generic internal error.
- A message with no parts at all is refused with -32602 by the SDK's own validation, before Mem2A sees it.
