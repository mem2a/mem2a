# Implementer's guide: Mem2A on your memory

For teams that have a memory, knowledge graph or context engine and want any vendor's agents to use it through Mem2A. It walks through what to build, in any language, in the order you'll need it, with the [spec](../spec/v0.1/mem2a.md) sections that govern each step. The [Python reference implementation](../python) does all of it in about 6,000 lines, and is there to read: its [architecture notes](../python/ARCHITECTURE.md) map each MUST in the spec to the code and the test that cover it.

New to A2A? Read the [A2A primer](a2a-primer.md) first; it covers the A2A you need for this guide in about 15 minutes. The [glossary](glossary.md) defines every term.

## What you're building

An A2A v1.0 agent that:

1. declares the Mem2A extension in its Agent Card,
2. answers intents with dossiers built from your memory, filtered by who's asking,
3. watches what each open task depends on, and pushes new versions when it changes,
4. records what agents report as claims, never as facts.

You don't need to change how your memory stores or learns anything. Mem2A only defines the conversation.

## Step by step

### 1. The Agent Card ([spec 5](../spec/v0.1/mem2a.md#5-discovery))

- Declare the extension in `capabilities.extensions` with `required: true` and params `{specVersion: "0.1", listen: [...], watchTimeout: "P7D"}`.
- Set `capabilities.pushNotifications` and `capabilities.streaming` to match `listen`.
- List the Mem2A media types in `defaultInputModes` and `defaultOutputModes`, and declare a security scheme.

[Example 01](../spec/v0.1/examples/01-agent-card.json) is a complete card.

### 2. Activation and authentication ([6](../spec/v0.1/mem2a.md#6-activation), [10.1–10.2](../spec/v0.1/mem2a.md#10-identity-and-permissions))

- Refuse any `SendMessage` without `A2A-Extensions: https://w3id.org/mem2a/v0.1` with `ExtensionSupportRequiredError` (`-32008`), before creating a task. Echo the header on responses.
- Map every credential to an agent and a principal. Refuse an intent whose `onBehalfOf` isn't that principal (`principal-mismatch`). Say in your card's description how you name principals. A full identity profile is in progress ([#11](https://github.com/mem2a/mem2a/issues/11)).

### 3. Task binding ([10.7](../spec/v0.1/mem2a.md#10-identity-and-permissions))

Record the agent and principal that opened each task. Answer every operation from anyone else, including `GetTask`, `ListTasks`, `SubscribeToTask`, `CancelTask`, push config operations and follow-ups, as if the task didn't exist.

### 4. Message handling ([7.5](../spec/v0.1/mem2a.md#75-what-memory-does-with-each-message))

Route each message by its single Mem2A payload and the task's phase, using the table in 7.5. Every status message you send carries:

- a text part;
- the `phase` metadata;
- `inReplyTo` when it answers a message;
- the extension URI in `extensions`.

Remember a task's processed `messageId`s, so a retry returns the task as it stands instead of being processed twice.

### 5. Building a dossier ([8.1](../spec/v0.1/mem2a.md#81-negotiate), [9](../spec/v0.1/mem2a.md#9-facts-claims-and-receipts), [10.3–10.4](../spec/v0.1/mem2a.md#10-identity-and-permissions))

- **Pick relevant items:** facts about the intent's entities, the precedent that bears on the action, and the constraints your company's policies attach to them. Which items count as relevant is your judgment, and it's where your memory competes.
- **Filter first.** Include an item only if the principal may read *every* source it came from. Do this deterministically, before any model sees the candidates or the agent's draft.
- **Never derive a constraint or a precedent from a claim.**
- **Give every item an id and a version.** Ids are unique across facts, precedent and constraints, and versions are strings.
- **Set `watching` to exactly the ids of the dossier's items.** Set `expiresAt` if you declare a `watchTimeout`.
- **Emit the `dossier` artifact before the status update**, so that a blocking `SendMessage` returns it.

### 6. Watching and updates ([8.3](../spec/v0.1/mem2a.md#83-listen))

- **Map changes to tasks.** When an item changes, is retired or superseded, or a principal loses access, find the open tasks in phase `awaiting-commit` that depend on it, or whose intent it's relevant to, and rebuild their dossiers. A reverse index from item and entity to tasks is the usual approach.
- **Decide what counts as a change.** A new version only when the set of ids or any item's version changed, never for wording alone.
- **Send the update.** Replace the `dossier` artifact, then send an `INPUT_REQUIRED` status with an update part listing `added`, `updated` and `removed` items. Something the principal can no longer see is just `removed`.
- **Deliver everywhere.** Send each update to every registered webhook and every open `SubscribeToTask` stream, and make sure `GetTask` reflects it.
- **Webhooks:**
  - let the company register each agent's webhook origins when it admits the agent, and refuse a push config on any other origin with `InvalidParamsError` ([8.3.10](../spec/v0.1/mem2a.md#83-listen));
  - deliver only to those origins, without following redirects, checking where the host name resolves when you connect;
  - deliver in order per webhook, in the background, with retries.
- **Streams:** keep `SubscribeToTask` streams open through `INPUT_REQUIRED`.

### 7. Commits ([8.4](../spec/v0.1/mem2a.md#84-commit))

- **Refuse stale commits.** If `basedOn` isn't the latest version you've produced, record nothing and answer `stale-dossier` with `currentVersion`.
- **Record a valid commit:**
  - Each claim becomes a fact with status `claim`, `claimedBy` and source `agent-commit`.
  - Who may see it: the committing principal, plus principals who can read every entity it names and every source of every item in the dossier the commit was based on ([10.5](../spec/v0.1/mem2a.md#10-identity-and-permissions)).
  - Record the conflicts for a person to review.
  - A claim never supersedes, retires or lifts anything.
- **Close the loop.** Re-evaluate other open tasks the claims affect, then emit the `receipt` artifact and complete the task.

### 8. Reads, failures and limits

- **Reads:** before you serve a stored dossier (`GetTask`, `ListTasks`, a stream's first event), in any task state, check it against the caller's current access. If it holds an item they may no longer see, replace it with a new version first: an ordinary update for an open task, a quiet artifact replacement for a finished one. Then every read, and every commit, sees the same version ([10.3](../spec/v0.1/mem2a.md#10-identity-and-permissions)).
- **Failures:** on an internal failure, fail the task with phase `failed` and error `internal`, without exception text ([8.6](../spec/v0.1/mem2a.md#86-failure)).
- **Limits:** cap open tasks per principal and agent, refusing further intents with `limit-exceeded`. Cap push configs per task, refusing further configs with `InvalidParamsError` whose message starts with `limit-exceeded` ([11.8](../spec/v0.1/mem2a.md#11-security-considerations)).

## Pitfalls in A2A SDKs

Mem2A asks for some things A2A SDKs don't make easy. These are what we hit, or what reviewers reported; check them against the SDK version you use. [#12](https://github.com/mem2a/mem2a/issues/12) tracks raising them upstream.

**a2a-sdk (Python) 1.2:**

- There's no public way to add events to an open task from outside a request. The reference implementation runs an "internal turn" through a private attribute; see `run_internal_turn` in [`python/src/mem2a/server.py`](../python/src/mem2a/server.py).
- **Extensions:** never marked activated, and the `A2A-Extensions` response header is never sent. Add a middleware.
- **contextId:** a follow-up with a `taskId` but no `contextId` gets a new `contextId`, and the task fails. Fill it in from the task.
- **Reply matching:** a blocking `SendMessage` returns at the first `INPUT_REQUIRED` event on the task, which can belong to a concurrent update. Run one turn at a time per task, and set `inReplyTo`.
- **Push:** notifications are sent inline, with `Content-Type: application/json` and no retries. Replace the sender.
- **Executor errors:** they leak their text in `-32603` errors. Catch them and fail the task yourself.

**@a2a-js/sdk (JavaScript), as reported in review on 1.3.0:**

- `SubscribeToTask` streams closed after the first update at `INPUT_REQUIRED`.
- An artifact changed out of band didn't show up in `GetTask` until it was written to the task store.
- Echoing the `A2A-Extensions` header is opt-in.
- It does infer `contextId` correctly.

**Any SDK:** numbers in data parts become doubles. Mem2A payloads avoid numbers for that reason ([2.6](../spec/v0.1/mem2a.md#2-conventions)).

## Testing

- Use the [spec's examples](../spec/v0.1/examples) as fixtures. Your payloads must validate against the [schemas](../spec/v0.1/schemas), with formats enforced. The schemas are plain JSON Schema 2020-12 and work with Ajv; an npm package is wanted ([#13](https://github.com/mem2a/mem2a/issues/13)).
- Run `mem2a-conform` against your memory, from a checkout of this repository:

  ```sh
  pip install -e "python[dev]"
  mem2a-conform --url https://memory.test.example.com --token "$TOKEN" --principal user:tom \
    --entity account:acme --action send_quote --other-token "$TOKEN_FOR_SOMEONE_ELSE"
  ```

  It checks your card, activation, refusals, negotiation, dossier rules, stale and invalid commits, unexpected messages, task binding, reads, cancel and payloads. `--allow-writes` adds a real commit and a retry; run it only against a test memory. `--dev-admin` adds the listen and access checks, which need a way to change your memory from the outside: today they use the reference server's `/dev` routes, and a portable contract is [wanted](https://github.com/mem2a/mem2a/issues/15). Each line of its report names the spec section it checks.

## Get listed, and report problems

- When `mem2a-conform` passes, add your implementation to [IMPLEMENTATIONS.md](../IMPLEMENTATIONS.md) with a pull request that includes its output.
- If your memory and someone's agent don't work together, file an [interop report](https://github.com/mem2a/mem2a/issues/new?template=interop-report.yml). These are the most useful bugs we can get.
- If the spec is unclear, that's a bug too: [propose a change](https://github.com/mem2a/mem2a/issues/new?template=spec-change.yml).
