# Wire quickstart: Mem2A with curl

The requests an agent makes most, ready to paste, against a sandbox memory seeded with the Acme story. If you can send HTTP and read server-sent events, you can speak Mem2A from any language. New to A2A? The [A2A primer](a2a-primer.md) explains the shapes you'll see here.

You'll play Tom's sales agent: negotiate before sending Acme a quote, get told to hold, hear memory call back when legal clears the pricing, read the new dossier, and report what you did. Then you'll play Priya's agent, which sees Tom's report as a claim.

## Start a sandbox memory

The sandbox is the [Python reference implementation](../python). You need Python 3.10 or later once, to run it; everything after that is plain HTTP, with `curl` and [`jq`](https://jqlang.org/).

```sh
git clone https://github.com/mem2a/mem2a && cd mem2a
python3 -m venv .venv && . .venv/bin/activate
pip install -e "python[dev]"
mem2a serve --seed acme --dev-admin
```

It prints its URLs and two development tokens, and listens on `http://127.0.0.1:8000`. `--dev-admin` turns on unauthenticated routes that change memory, so you can play legal. Never expose them.

## What every request needs

| Header | Value | Why |
| --- | --- | --- |
| `A2A-Version` | `1.0` | A2A requires it. Without it, the server assumes 0.3 and answers `-32009`. |
| `A2A-Extensions` | `https://w3id.org/mem2a/v0.1` | Activates Mem2A. Without it, a message gets `-32008`. |
| `Authorization` | `Bearer <token>` | Says which agent you are, and who you act for. |
| `Content-Type` | `application/json` | JSON-RPC. |

The sandbox accepts unsigned development tokens of the form `dev:<agent>:<principal>[:<groups>]`. The principal has a kind (`user:tom`) and groups are a comma-separated list, so Tom's token is `dev:sales-assistant:user:tom:group:sales`. A real memory verifies real tokens instead ([spec 10](../spec/v0.1/mem2a.md#10-identity-and-permissions)).

Set up a shell helper, so the commands below stay short:

```sh
export M=http://127.0.0.1:8000
export TOM='dev:sales-assistant:user:tom:group:sales'
export PRIYA='dev:account-assistant:user:priya:group:sales'
rpc() {  # rpc <token> <json-rpc body>
  curl -s $M/a2a/jsonrpc -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
    -H 'A2A-Extensions: https://w3id.org/mem2a/v0.1' -H "Authorization: Bearer $1" -d "$2"
}
```

The `jq` filters below trim the output to the interesting parts, and a few steps use `jq` to save ids. Drop the filters to see whole responses, or compare with the [spec's examples](../spec/v0.1/examples).

## 1. Discover

```sh
curl -s $M/.well-known/agent-card.json | jq '.capabilities.extensions[0]'
```

```json
{
  "uri": "https://w3id.org/mem2a/v0.1",
  "description": "Mem2A: negotiate before acting, listen for changes, commit after acting.",
  "required": true,
  "params": { "specVersion": "0.1", "listen": ["push", "subscribe"], "watchTimeout": "P7D" }
}
```

## 2. Negotiate

Tom's agent says what it's about to do, and for whom:

```sh
rpc "$TOM" '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{
  "messageId":"msg-1","role":"ROLE_USER","extensions":["https://w3id.org/mem2a/v0.1"],
  "parts":[{"mediaType":"application/vnd.mem2a.intent+json","data":{
    "action":"send_quote","summary":"Send Acme a renewal quote",
    "entities":["account:acme","doc:acme-renewal-quote"],"onBehalfOf":"user:tom"}}]}}}' \
| tee negotiate.json | jq '.result.task | {id, contextId, state: .status.state,
    says: .status.message.parts[0].text,
    dossier: .artifacts[0].parts[0].data | {version, constraints: [.constraints[].statement], watching}}'
```

```json
{
  "id": "06490d53-0587-45cd-9759-24fe87ba71c3",
  "contextId": "7fab4efc-1c0c-4620-8238-d7e06a1483d5",
  "state": "TASK_STATE_INPUT_REQUIRED",
  "says": "Hold: Do not send Acme new pricing until legal clears it.",
  "dossier": {
    "version": "12",
    "constraints": ["Do not send Acme new pricing until legal clears it."],
    "watching": ["f-311", "f-208", "p-4", "c-17"]
  }
}
```

Memory answered with dossier 12 and kept the task open, waiting for a commit. Versions are opaque strings: compare them for equality, and never parse them or do arithmetic on them. The sandbox happens to count from 12, so its numbers match the spec's examples. Save the ids:

```sh
export TASK=$(jq -r .result.task.id negotiate.json) CTX=$(jq -r .result.task.contextId negotiate.json)
```

## 3. Listen

In a second terminal (with the same `export` lines), keep a stream open:

```sh
curl -sN $M/a2a/jsonrpc -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -H 'A2A-Extensions: https://w3id.org/mem2a/v0.1' -H "Authorization: Bearer $TOM" \
  -H 'Accept: text/event-stream' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"SubscribeToTask\",\"params\":{\"id\":\"$TASK\"}}"
```

Each event is one `data:` line holding a JSON-RPC response, whose `result` is one A2A stream event: a `task`, an `artifactUpdate` or a `statusUpdate`. The first is a snapshot of the task, as A2A requires. Lines that start with `:` are keep-alive comments, sent every 15 seconds; skip them. The stream stays open while the task waits for a commit, and closes when the task ends.

Prefer callbacks? When you negotiate, put a push notification config in `params`, next to `message`:

```json
"configuration": {
  "taskPushNotificationConfig": {
    "url": "http://127.0.0.1:9000/hook",
    "authentication": { "scheme": "Bearer", "credentials": "<a secret you choose>" }
  }
}
```

Memory then POSTs each of the task's events to that URL, as `application/a2a+json` with the header `Authorization: Bearer <your secret>`, so you can tell its calls from anyone else's. Each body is a bare event, such as `{"statusUpdate": {...}}`, without the `result` wrapper that the stream puts around it. The sandbox sends callbacks only to this machine. A real memory sends them only to the origins your company registered for your agent, and refuses any other URL with `-32602`.

## 4. Change memory

In a third terminal, play legal and clear the pricing:

```sh
curl -s -X POST $M/dev/scenarios/acme/legal-clears
```

```json
{"ok": true, "updatedTasks": ["06490d53-0587-45cd-9759-24fe87ba71c3"]}
```

The stream in the second terminal gets two events: the replaced dossier, then a status update saying what changed. Trimmed:

```json
{"result": {"artifactUpdate": {"artifact": {"artifactId": "dossier", "parts": [{"data": {"version": "13"}}]}}}}
{"result": {"statusUpdate": {"status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [
  {"text": "Legal cleared Acme pricing. Dossier is now version 13."},
  {"mediaType": "application/vnd.mem2a.update+json", "data": {"dossierVersion": "13", "previousVersion": "12",
    "changes": [{"id": "f-340", "kind": "fact", "change": "added"}, {"id": "f-311", "kind": "fact", "change": "removed"},
                {"id": "c-17", "kind": "constraint", "change": "removed"}, {"id": "p-4", "kind": "precedent", "change": "removed"}]}}]}}}}}
```

## 5. Read before acting

Events are signals. Before acting, read the current dossier ([spec 8.3.7](../spec/v0.1/mem2a.md#83-listen)):

```sh
rpc "$TOM" "{\"jsonrpc\":\"2.0\",\"id\":\"3\",\"method\":\"GetTask\",\"params\":{\"id\":\"$TASK\"}}" \
| jq '.result.artifacts[0].parts[0].data | {version, constraints}'
```

```json
{ "version": "13", "constraints": [] }
```

Nothing blocks the quote now, so Tom's agent sends it.

## 6. Commit

Report what you did, against the version you relied on. First, see what happens if you report against the old one:

```sh
commit() {  # commit <messageId> <basedOn>
  rpc "$TOM" "{\"jsonrpc\":\"2.0\",\"id\":\"4\",\"method\":\"SendMessage\",\"params\":{\"message\":{
    \"messageId\":\"$1\",\"taskId\":\"$TASK\",\"contextId\":\"$CTX\",\"role\":\"ROLE_USER\",
    \"extensions\":[\"https://w3id.org/mem2a/v0.1\"],
    \"parts\":[{\"mediaType\":\"application/vnd.mem2a.commit+json\",\"data\":{
      \"basedOn\":\"$2\",\"action\":\"send_quote\",\"outcome\":\"done\",\"summary\":\"Sent Acme the renewal quote.\",
      \"claims\":[{\"statement\":\"Tom sent Acme a renewal quote at \$1.2M a year.\",\"entities\":[\"account:acme\"]}]}}]}}}"
}
commit msg-2 12 | jq '.result.task.status.message.parts[] | select(.mediaType=="application/vnd.mem2a.error+json") | .data'
```

```json
{
  "code": "stale-dossier",
  "currentVersion": "13",
  "message": "Commit is based on dossier 12; the current version is 13. Read the current dossier and commit again."
}
```

Nothing was recorded. Now report against version 13:

```sh
commit msg-3 13 | jq '.result.task | {state: .status.state,
  receipt: (.artifacts[] | select(.artifactId=="receipt") | .parts[0].data)}'
```

```json
{
  "state": "TASK_STATE_COMPLETED",
  "receipt": {
    "commitId": "cm-1",
    "basedOn": "13",
    "recorded": [{ "factId": "f-341", "version": "1", "status": "claim" }],
    "conflicts": [],
    "at": "2026-10-01T00:41:26Z"
  }
}
```

Always send `contextId` with `taskId` on follow-ups: some A2A SDKs mishandle a follow-up without it. Retrying a commit with the same `messageId` returns the same receipt; it's never recorded twice.

## 7. Priya's agent

Priya's agent is about to draft a follow-up to Acme:

```sh
rpc "$PRIYA" '{"jsonrpc":"2.0","id":"5","method":"SendMessage","params":{"message":{
  "messageId":"p-1","role":"ROLE_USER","extensions":["https://w3id.org/mem2a/v0.1"],
  "parts":[{"mediaType":"application/vnd.mem2a.intent+json","data":{
    "action":"draft_followup","summary":"Draft a follow-up email to Acme",
    "entities":["account:acme"],"onBehalfOf":"user:priya"}}]}}}' \
| tee priya.json | jq '[.result.task.artifacts[0].parts[0].data.facts[] | {id, status, statement}]'
```

```json
[
  { "id": "f-340", "status": "confirmed", "statement": "Legal approved Acme renewal pricing at $1.2M a year." },
  { "id": "f-208", "status": "confirmed", "statement": "Acme's current contract renews on October 31, 2026." },
  { "id": "f-341", "status": "claim", "statement": "Tom sent Acme a renewal quote at $1.2M a year." }
]
```

Tom's report shows up as a claim, not a confirmed fact. Tasks belong to whoever opened them, so Tom can't read Priya's:

```sh
export PTASK=$(jq -r .result.task.id priya.json)
rpc "$TOM" "{\"jsonrpc\":\"2.0\",\"id\":\"6\",\"method\":\"GetTask\",\"params\":{\"id\":\"$PTASK\"}}" | jq -c '.error | {code, message}'
```

```json
{"code":-32001,"message":"Task not found"}
```

Priya's agent decides there's nothing to follow up, and cancels:

```sh
rpc "$PRIYA" "{\"jsonrpc\":\"2.0\",\"id\":\"7\",\"method\":\"CancelTask\",\"params\":{\"id\":\"$PTASK\"}}" \
| jq '.result.status | {state, says: .message.parts[0].text}'
```

```json
{ "state": "TASK_STATE_CANCELED", "says": "Canceled. Memory stopped watching this task and recorded nothing from it." }
```

Finally, play the CRM and confirm Tom's claim: `curl -s -X POST $M/dev/facts/f-341/confirm -H 'Content-Type: application/json' -d '{"by":"system:crm"}'`.

## 8. Lose access

Permissions follow the source: an agent sees only what its principal may read, checked every time memory sends something ([spec 10.3](../spec/v0.1/mem2a.md#10-identity-and-permissions)). To watch that happen, restart the sandbox (Ctrl-C, then `mem2a serve --seed acme --dev-admin`) so the numbers below match. Play legal, then negotiate as Tom with the same request as in step 2:

```sh
curl -s -X POST $M/dev/scenarios/acme/legal-clears
rpc "$TOM" '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{
  "messageId":"msg-1","role":"ROLE_USER","extensions":["https://w3id.org/mem2a/v0.1"],
  "parts":[{"mediaType":"application/vnd.mem2a.intent+json","data":{
    "action":"send_quote","summary":"Send Acme a renewal quote",
    "entities":["account:acme","doc:acme-renewal-quote"],"onBehalfOf":"user:tom"}}]}}}' \
| tee negotiate.json | jq '.result.task.artifacts[0].parts[0].data | {version, watching}'
export TASK=$(jq -r .result.task.id negotiate.json) CTX=$(jq -r .result.task.contextId negotiate.json)
```

```json
{ "version": "12", "watching": ["f-340", "f-208"] }
```

Fact `f-340` comes from legal's approval email. Now the company decides only legal may read that email:

```sh
curl -s -X POST $M/dev/sources/email:legal-acme-approval-2026-09-28/readers \
  -H 'Content-Type: application/json' -d '{"readers": ["group:legal"]}'
```

```json
{"ok": true, "updatedTasks": ["7e9462a1-d65c-4e62-b8c3-57d82dd5b7da"]}
```

Tom's task got a new dossier version without the fact:

```sh
rpc "$TOM" "{\"jsonrpc\":\"2.0\",\"id\":\"8\",\"method\":\"GetTask\",\"params\":{\"id\":\"$TASK\"}}" \
| jq '.result | {says: .status.message.parts[0].text,
    update: .status.message.parts[1].data | {dossierVersion, previousVersion, changes},
    watching: .artifacts[0].parts[0].data.watching}'
```

```json
{
  "says": "Dossier changed: 1 fact removed. Dossier is now version 13.",
  "update": {
    "dossierVersion": "13",
    "previousVersion": "12",
    "changes": [{ "id": "f-340", "kind": "fact", "change": "removed" }]
  },
  "watching": ["f-208"]
}
```

The update lists `f-340` as removed, exactly as it would a retired fact, so it doesn't reveal why ([spec 8.3.4](../spec/v0.1/mem2a.md#83-listen)). Every read now returns version 13, and a commit against it is recorded: `commit msg-4 13`.

## Errors you'll meet

| Code | Meaning | Fix |
| --- | --- | --- |
| HTTP `401` | Not authenticated | Send `Authorization: Bearer <token>` with a valid token. The sandbox answers with code `-32000` and `"id": null`. |
| `-32009` | Version not supported | Send `A2A-Version: 1.0`. |
| `-32008` | Extension required | Send `A2A-Extensions: https://w3id.org/mem2a/v0.1`. |
| `-32001` | Task not found | Wrong id, or the task belongs to another agent or principal. |
| `-32004` | Unsupported operation | For example, a new message to a task that has already ended. |
| `-32602` | Invalid params | The request doesn't match A2A's shapes, or a push notification config was refused: its URL isn't on a registered origin, or the task already has as many as memory allows (the message starts with `limit-exceeded`). |

Mem2A's own errors (`stale-dossier`, `invalid-commit`, `unexpected-message`, and so on) arrive as an error part in the task's status message, not as JSON-RPC errors. The [spec](../spec/v0.1/mem2a.md#75-what-memory-does-with-each-message) lists when each one happens.

## The sandbox's admin routes

Only with `--dev-admin`. They take and return JSON. Every change answers `{"ok": true, "updatedTasks": [...]}`, listing the open tasks that got an update, once memory has produced those updates.

| Route | Does |
| --- | --- |
| `GET /dev/state` | Facts, precedent, policies, sources and open tasks |
| `POST /dev/facts` | Add a fact: `{id, statement, source, confirmedBy, entities, supersedes?, note?, readers?}` |
| `POST /dev/facts/{id}/retire` | Retire a fact: `{note?}` |
| `POST /dev/facts/{id}/confirm` | Confirm a claim: `{by}` |
| `POST /dev/sources/{ref}/readers` | Change who may read a source: `{readers: [...]}` or `{readers: null}` for everyone |
| `POST /dev/scenarios/acme/legal-clears` | Legal approves Acme pricing |

## Next

- Write an agent in your language. [Issue #17](https://github.com/mem2a/mem2a/issues/17) is open for a JavaScript or Go example.
- Building a memory instead? Read the [implementer's guide](implementers-guide.md).
