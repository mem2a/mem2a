# A2A primer: what Mem2A uses

Mem2A is an extension of [A2A](https://a2a-protocol.org/), the open protocol for agents to talk to each other. A2A was contributed by Google and is now a Linux Foundation project. The [Mem2A spec](../spec/v0.1/mem2a.md) assumes you know A2A v1.0. This page covers the parts it uses, in about 15 minutes, so you can read the spec and the [examples](../spec/v0.1/examples) without the much longer A2A specification open beside them. Section numbers like (A2A §3.1.6) point into the [A2A specification](https://a2a-protocol.org/latest/specification/) for when you want the full text.

If you'd rather learn by doing, run the [wire quickstart](wire-quickstart.md) and come back here when something looks strange. The [glossary](glossary.md) defines every term in one place.

## Who's who

A2A has a **client** that sends requests and a **server**, the remote agent, that answers them. In Mem2A, the memory is the server and the agent about to act is the client.

| A2A | In Mem2A |
| --- | --- |
| Server (remote agent) | The company's memory |
| Client | The agent about to act, such as Tom's sales assistant |
| Message with role `ROLE_USER` | A message from the agent: an intent, an answer or a commit |
| Message with role `ROLE_AGENT` | A message from memory |
| Task | One action the agent is about to take, from intent to commit |
| Artifact | The dossier, and the receipt at the end |

The roles trip up almost everyone. In A2A, `ROLE_USER` means "the side that sends requests", not "a human". The agent's messages to memory carry `ROLE_USER`, and memory's carry `ROLE_AGENT`.

## Finding the memory: the Agent Card

Every A2A server publishes an Agent Card, a JSON document at `/.well-known/agent-card.json` (A2A §8). The fields Mem2A relies on:

| Field | What it says | For Mem2A |
| --- | --- | --- |
| `supportedInterfaces` | Where to send requests, and in which protocol binding | For example `https://memory.example.com/a2a/jsonrpc`, binding `JSONRPC`, version `1.0` |
| `capabilities.extensions` | The extensions the server supports | The Mem2A entry, with `required: true` |
| `capabilities.streaming`, `capabilities.pushNotifications` | Whether the server can stream events and call webhooks | How agents can hear about changes |
| `securitySchemes`, `securityRequirements` | How clients must authenticate | How an agent proves who it is and who it acts for |
| `defaultInputModes`, `defaultOutputModes` | The media types it accepts and produces | The Mem2A payload types |
| `skills` | What it can do, for people and directories | `negotiate` and `commit`, descriptive only |

[Example 01](../spec/v0.1/examples/01-agent-card.json) is a complete card. An agent takes its company's memory URL from its own configuration, never from something it read along the way ([spec 5.7](../spec/v0.1/mem2a.md#5-discovery)).

## Calling it: JSON-RPC over HTTP

A2A has three bindings: JSON-RPC, gRPC and HTTP+JSON (REST). Mem2A works over any of them; the spec's examples use JSON-RPC 2.0, where every call is an HTTP `POST` to the interface URL:

```jsonc
// POST https://memory.example.com/a2a/jsonrpc
// Content-Type: application/json
// A2A-Version: 1.0                              (A2A requires it on every request)
// A2A-Extensions: https://w3id.org/mem2a/v0.1   (activates Mem2A; see below)
// Authorization: Bearer <token>
{
  "jsonrpc": "2.0",
  "id": "1",                 // yours; the response echoes it
  "method": "SendMessage",
  "params": { "message": { /* ... */ } }
}

// The response has either "result" or "error":
{ "jsonrpc": "2.0", "id": "1", "result": { "task": { /* ... */ } } }
{ "jsonrpc": "2.0", "id": "1", "error": { "code": -32001, "message": "Task not found" } }
```

The methods a Mem2A agent uses (A2A §3.1):

| Method | Does | In Mem2A |
| --- | --- | --- |
| `SendMessage` | Sends a message, and returns the task it belongs to | Send an intent, an answer or a commit |
| `SendStreamingMessage` | The same, but the response is a stream of events | Optional: watch memory work on a message |
| `GetTask` | Returns a task as it stands now, with its artifacts | Read the current dossier right before acting |
| `ListTasks` | Lists your tasks | Find the tasks you have open |
| `CancelTask` | Cancels a task | Decide not to act |
| `SubscribeToTask` | Opens a stream of a task's events | Listen for updates |
| `CreateTaskPushNotificationConfig` | Registers a webhook for a task | Listen for updates by callback |
| `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig` | Manage webhooks | |

## Messages and parts

A message (A2A §4.1.4) is one turn from one side:

| Field | Meaning |
| --- | --- |
| `messageId` | Unique per message, chosen by the sender. Memory uses it to spot retries ([spec 8.4.8](../spec/v0.1/mem2a.md#84-commit)). |
| `role` | `ROLE_USER` (client) or `ROLE_AGENT` (server). |
| `parts` | The content: a list of parts. |
| `taskId`, `contextId` | Which task the message belongs to. Leave both out of the first message; the server creates the task. Send both on every follow-up. |
| `extensions` | URIs of the extensions this message uses. Mem2A messages list `https://w3id.org/mem2a/v0.1`. |
| `metadata` | Extra data, keyed by namespace. Mem2A puts the phase here. |

A part (A2A §4.1.6) holds one piece of content, and has exactly one of:

- `text`: plain text, for people;
- `data`: any JSON value, with a `mediaType` saying what it is;
- `raw` or `url`: a file, with a `mediaType` and an optional `filename`.

Every Mem2A payload is a `data` part with a media type such as `application/vnd.mem2a.intent+json` ([spec 7.1](../spec/v0.1/mem2a.md#71-payload-parts)). Memory also adds a `text` part to each status message, so people, and agents that don't know Mem2A, can follow along.

## Tasks

A task (A2A §4.1.1) is the unit of work. The server creates it when the first message arrives, and keeps it until it ends.

| Field | Meaning |
| --- | --- |
| `id`, `contextId` | The task's id, and the conversation it belongs to |
| `status` | `state`, plus the latest status `message` from the server and a `timestamp` |
| `artifacts` | What the task has produced so far |
| `history` | The messages so far, if the server keeps them |

A task's state is one of these (A2A §4.1.3):

| State | Kind | In Mem2A |
| --- | --- | --- |
| `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING` | Active | Memory is preparing its answer (phase `working`). |
| `TASK_STATE_INPUT_REQUIRED` | Interrupted: waiting for the client | Memory asked a question (phase `question`), or delivered a dossier and waits for the commit (phase `awaiting-commit`). |
| `TASK_STATE_AUTH_REQUIRED` | Interrupted | Memory needs fresh credentials (phase `reauth`; no 0.1 flow uses it). |
| `TASK_STATE_COMPLETED` | Terminal | Memory recorded the commit (phase `committed`). |
| `TASK_STATE_REJECTED` | Terminal | Memory refused the intent (phase `refused`). |
| `TASK_STATE_CANCELED` | Terminal | The agent canceled, or the watch expired (phases `canceled`, `expired`). |
| `TASK_STATE_FAILED` | Terminal | Memory couldn't finish (phase `failed`). |

A terminal task never changes again, and A2A refuses new messages to it with `-32004`. Two Mem2A phases share `TASK_STATE_INPUT_REQUIRED`, which is why every status message carries the phase in its metadata ([spec 7.2](../spec/v0.1/mem2a.md#72-phase)).

Artifacts (A2A §4.1.7) are a task's outputs, each with an `artifactId`, a `name` and parts. A server can replace an artifact by sending it again with the same `artifactId`. Mem2A keeps the dossier in an artifact called `dossier` and replaces it whole when it changes; the receipt goes in one called `receipt`.

## Anatomy of a Mem2A task

Here is memory's answer to Tom's intent, from [example 02](../spec/v0.1/examples/02-negotiate.json), trimmed and annotated:

```jsonc
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "task": {
      "id": "task-acme-quote",                     // send it back with every follow-up...
      "contextId": "ctx-tom-acme",                 // ...along with this
      "status": {
        "state": "TASK_STATE_INPUT_REQUIRED",      // A2A: waiting for the client
        "message": {
          "role": "ROLE_AGENT",                    // from memory
          "parts": [
            { "text": "Hold the quote: legal paused Acme pricing on Friday. I'll call back if that changes." }
          ],
          "metadata": {
            "https://w3id.org/mem2a/v0.1/phase": "awaiting-commit",   // Mem2A: act, then commit
            "https://w3id.org/mem2a/v0.1/inReplyTo": "msg-tom-001"    // the message this answers
          },
          "extensions": ["https://w3id.org/mem2a/v0.1"]
        }
      },
      "artifacts": [
        {
          "artifactId": "dossier",
          "name": "dossier",
          "parts": [
            {
              "mediaType": "application/vnd.mem2a.dossier+json",     // a Mem2A payload
              "data": {
                "version": "12",                                     // commit against this
                "summary": "Hold the quote: legal paused Acme pricing on Friday.",
                "facts": [ /* f-311: legal paused Acme pricing; f-208: the renewal date */ ],
                "precedent": [ /* p-4: a quote sent during a legal review was withdrawn */ ],
                "constraints": [
                  { "id": "c-17", "statement": "Do not send Acme new pricing until legal clears it.",
                    "level": "must", "basis": ["f-311"] }
                ],
                "watching": ["f-311", "f-208", "p-4", "c-17"],      // what memory is watching for Tom
                "expiresAt": "2026-10-05T09:10:02Z"                 // the watch ends then
              }
            }
          ]
        }
      ]
    }
  }
}
```

Read it as: the task is open and waiting for Tom's agent (`INPUT_REQUIRED`, `awaiting-commit`). The rules for this action are in dossier 12, and memory will send a new version if any of those items change.

## Hearing about changes

A2A has three ways for a client to follow a task (A2A §3.1, §4.3). Mem2A uses all three.

**Polling.** Call `GetTask` whenever you like. Mem2A requires it, or an equivalent stream, right before acting, so the agent acts on the current dossier ([spec 8.3.7](../spec/v0.1/mem2a.md#83-listen)).

**Streaming.** `SubscribeToTask` (and `SendStreamingMessage`) answer with [server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html): a long-lived HTTP response in which each event is a `data:` line. In the JSON-RPC binding, each line holds a JSON-RPC response whose `result` is one stream event (A2A §3.2.3):

| Event | Carries |
| --- | --- |
| `task` | The whole task. `SubscribeToTask` always starts with one: the task as it stands. |
| `statusUpdate` | A new status: state, message and metadata. Mem2A updates arrive this way. |
| `artifactUpdate` | A new or replaced artifact. A new dossier version arrives this way, just before its `statusUpdate`. |
| `message` | A standalone message. Mem2A doesn't use these. |

A2A says a stream ends when the task reaches a terminal state (A2A §3.1.6), but its HTTP+JSON binding describes streams that also close at interrupted states (A2A §11.7), and SDKs differ. Mem2A asks memory to keep streams open while a task waits for a commit, and asks agents to subscribe again, or poll, if a stream closes anyway ([spec 8.3.6](../spec/v0.1/mem2a.md#83-listen)).

**Push notifications.** The client gives the server a webhook, a `TaskPushNotificationConfig` (A2A §4.3.1):

```json
{
  "url": "https://sales-assistant.example.com/a2a/callbacks",
  "authentication": { "scheme": "Bearer", "credentials": "<a secret for this task>" }
}
```

It can send one in the first `SendMessage`, as `params.configuration.taskPushNotificationConfig`, or add one later with `CreateTaskPushNotificationConfig`. The server then POSTs each event to the URL, with the same bodies as a stream (`statusUpdate`, `artifactUpdate` and so on) and the header `Authorization: Bearer <the secret>` (A2A §4.3.3). A2A delivers at least once, so a webhook can see the same event twice. That, and the chance of forged or lost calls, is why Mem2A treats every push as a signal to read the task, never as the dossier itself ([ADR 0008](../adrs/0008-updates-are-signals.md)). A Mem2A memory also delivers only to webhook origins the company registered for the agent ([spec 8.3.10](../spec/v0.1/mem2a.md#83-listen)).

**Blocking.** By default, `SendMessage` waits until the task is interrupted or terminal, then returns it (A2A §3.2.2; set `configuration.returnImmediately` to return at once). Memory always emits the dossier artifact before the status that says `awaiting-commit`, so the blocking response already contains the dossier ([spec 8.1.4](../spec/v0.1/mem2a.md#81-negotiate)).

## Extensions and activation

An A2A extension adds behavior on top of A2A (A2A §4.6). Three things make one work:

1. **Declaration.** The server lists it in `capabilities.extensions` of its card: a `uri` that names it, a `description`, `required`, and `params` for its settings. Mem2A's params say which ways to listen are supported and how long a watch lasts ([example 01](../spec/v0.1/examples/01-agent-card.json)).
2. **Activation.** The client asks for it on each request, in the `A2A-Extensions` header (a comma-separated list of URIs; gRPC uses request metadata). A server whose extension is `required: true` refuses requests that don't ask for it with `ExtensionSupportRequiredError` (`-32008`). Mem2A is required, so every Mem2A request carries the header ([spec 6](../spec/v0.1/mem2a.md#6-activation)).
3. **Data.** Messages and artifacts that use an extension list its URI in their `extensions` field, and extension data goes in `metadata` under keys that start with the URI, such as `https://w3id.org/mem2a/v0.1/phase`.

Mem2A is a *profile* extension: it adds no RPC methods and no task states. It gives structure and rules to things A2A already has: data parts, metadata, artifacts, push notifications and streams ([ADR 0001](../adrs/0001-build-on-a2a.md)).

## Errors

Errors at the A2A level come back as JSON-RPC errors (A2A §5.4):

| Code | A2A error | When a Mem2A agent sees it |
| --- | --- | --- |
| `-32700`, `-32600` | Parse error, invalid request | The body isn't valid JSON-RPC. |
| `-32601` | Method not found | A typo in the method name. |
| `-32602` | Invalid params | The params don't match A2A's shapes, or memory refused a push notification config. |
| `-32603` | Internal error | Something broke on the server. |
| `-32001` | `TaskNotFoundError` | Wrong task id, or a task that belongs to another agent or principal. |
| `-32004` | `UnsupportedOperationError` | A message to a task that has ended, among others. |
| `-32008` | `ExtensionSupportRequiredError` | The request didn't activate Mem2A. |
| `-32009` | `VersionNotSupportedError` | The request didn't send `A2A-Version: 1.0`. |

Mem2A's own errors, such as `stale-dossier` or `unexpected-message`, are different: they arrive as an `application/vnd.mem2a.error+json` part inside the task's status message. Most of them leave the task open, so the agent can fix its message and send it again on the same task ([spec 7.5](../spec/v0.1/mem2a.md#75-what-memory-does-with-each-message)).

## Why Mem2A payloads have no numbers

A2A defines its data model in Protocol Buffers. A data part's value is a `google.protobuf.Value`, whose only kind of number is a double. Any hop through gRPC or a protobuf-based SDK can turn `12` into `12.0`, which a client in a strictly typed language may refuse to read as an integer. So every Mem2A version, count and duration is a string: `"version": "12"`, `"watchTimeout": "P7D"` ([ADR 0007](../adrs/0007-versions-are-opaque-strings.md)). Versions are also opaque: compare them for equality, and never parse them.

## Credentials and delegation

A2A leaves authentication to standard HTTP schemes, which the card declares in `securitySchemes`: OAuth 2.0, OpenID Connect, API keys, HTTP authentication or mutual TLS (A2A §4.5). Mem2A needs more than "who is calling": memory must know both the agent and the person it acts for, because what the dossier may contain depends on the person's access ([spec 10](../spec/v0.1/mem2a.md#10-identity-and-permissions)).

The recommended way is **OAuth 2.0 Token Exchange** ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693)): the agent trades the person's token for one that names the person as the subject and the agent as the actor. Decoded, such a token looks like this:

```jsonc
{
  "iss": "https://login.example.com",
  "aud": "https://memory.example.com",       // issued for memory only (RFC 8707)
  "sub": "user:tom",                          // the principal: who the agent acts for
  "act": { "sub": "agent:sales-assistant" },  // the actor: the agent itself (RFC 8693)
  "exp": 1790000000,
  "cnf": { "jkt": "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I" }  // bound to the agent's key
}
```

- **Audience** ([RFC 8707](https://www.rfc-editor.org/rfc/rfc8707)): a token issued for memory can't be replayed against another service, and the other way round.
- **Sender-constrained tokens**: with DPoP ([RFC 9449](https://www.rfc-editor.org/rfc/rfc9449)) or mutual TLS ([RFC 8705](https://www.rfc-editor.org/rfc/rfc8705)), a stolen token is useless without the agent's private key. The spec recommends them in production.
- **Chains**: if an agent acts through another agent, `act` claims nest, and memory applies the narrowest access along the chain.

A full identity profile is [in progress](https://github.com/mem2a/mem2a/issues/11). The sandbox skips all this and accepts unsigned development tokens like `dev:sales-assistant:user:tom:group:sales`: agent, principal and groups in one string.

## Other standards, in a line each

- **MUST, SHOULD, MAY** in capitals are requirement levels as defined by [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) and [RFC 8174](https://www.rfc-editor.org/rfc/rfc8174). A constraint's `level` of `must` or `should` is the company's rule, not one of these.
- **JSON Schema** [2020-12](https://json-schema.org/draft/2020-12) is how the [schemas](../spec/v0.1/schemas) define each payload's syntax.
- **Timestamps** follow [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) (`2026-09-28T09:10:02Z`), and durations ISO 8601 (`P7D` is seven days).
- **MCP**, the [Model Context Protocol](https://modelcontextprotocol.io/), connects an agent to tools and data. Mem2A covers what a tool call can't: memory that asks back, speaks first when something changes, and keeps track of what agents did ([spec 13](../spec/v0.1/mem2a.md#13-relationship-to-mcp-and-a2a)).

## Next

- [How Mem2A works](how-it-works.md): the protocol as a story, with diagrams.
- [Wire quickstart](wire-quickstart.md): every request with curl, against a sandbox memory.
- [The spec](../spec/v0.1/mem2a.md), now that its vocabulary is familiar.
