# Mem2A: a memory-to-agent protocol

> **Status: draft 0.1, private while we work on it.** Proposed by the team at Sentra. Mem2A is an extension to [A2A](https://a2a-protocol.org/) v1.0.

**What we're attempting, in one sentence:** give every agent in a company, whoever built it, one shared memory that it checks before it acts and reports to after it acts, so hundreds of agents can work inside the same company without contradicting each other or doing what they shouldn't.

[Spec](spec/v0.1/mem2a.md) · [How it works](docs/how-it-works.md) · [Run the demo](examples/acme-quote) · [FAQ](docs/faq.md) · [Roadmap](docs/roadmap.md)

## Why we're building this

### We've been here before

When the iPhone arrived, people brought their own phones to work, and IT had no say over devices it didn't own. What let companies say yes was a new layer: mobile device management. Every phone checked in, got the company's rules, and had its work data wiped when its owner left. And it worked because it ran on a standard: Apple built a management protocol into the iPhone that any vendor could speak.

Personal agents are bring-your-own-device all over again, except this time the devices make decisions. Nothing tells them what the company has decided, and nothing hears back from them. Until something does, agents in the enterprise will be chaos. Mem2A is our proposal for the standard part.

### The enterprise is filling up with agents

Every employee is about to bring a personal agent to work: OpenAI's dots, Meta's Muse, Instinct and whatever ships next month. Teams are adding specialist agents for support, finance, sales and engineering, and SaaS vendors are shipping agents inside their products.

Each of these agents remembers only its own user or its own task. None of them shares a view of what the company has decided. And none of them reports to anyone: there is no boss agent coordinating a company's personal agents. They're peers, built by different vendors, acting at machine speed.

### What breaks

Put hundreds of these agents in one company and the same failures keep showing up:

- **Contradiction.** Legal paused Acme's pricing on Friday. Priya's agent knows; Tom's agent doesn't, and sends Acme the old offer.
- **Out-of-scope action.** An agent does something it wasn't allowed to do. Sometimes that's the model; often nobody told it the current rules. In September 2026, OpenAI [held back a flagship model](https://www.aljazeera.com/economy/2026/9/29/openai-scraps-release-of-latest-ai-model-over-safety-concerns) over "scope and authorization."
- **Stale action.** An agent acts on a decision that Thursday's meeting reversed.
- **Missing precedent.** An agent writes an accurate but tone-deaf update because it never knew leadership had rejected the same request on another project. It couldn't have searched for that; it didn't know it existed.
- **Contagion.** Bad instructions hop from agent to agent. In a controlled test, OpenAI [demonstrated a prompt injection that copies itself](https://lastweekin.ai/p/last-week-in-ai-345-5-new-models) from one agent to the next by email.
- **Lost learning.** What one agent learns stays in its session, or in its vendor's product. The next agent starts from zero.

Every agent you add makes this worse. Agents that stay in sync by messaging each other need N(N−1)/2 channels: 10 for five agents, 4,950 for a hundred. Each channel is another place for the truth to split, and another path for a bad instruction to travel.

### Why the obvious fixes don't work

- **Smarter models.** A better model follows instructions better. It still can't respect a scope nobody gave it. And it doesn't fix handoffs: most of a task's elapsed time is spent waiting at handoffs, not working.
- **Search (RAG).** Search answers the question an agent asks. The failures above come from questions it didn't know to ask. Search also keeps serving a stale fact until someone deletes it.
- **Subscriptions.** A subscription, to an MCP resource for example, only fires for what you subscribed to. You can't subscribe to what you don't know exists.
- **A boss agent.** An orchestrator works for small teams, but it funnels everyone's work through one context window. And personal agents don't have one.
- **Agents talking to each other.** A2A standardizes the conversation, and by design its agents "interact without needing to share internal memory." Syncing pair by pair grows quadratically, and spreads bad instructions along with good ones.
- **Each vendor's own memory.** Ten vendors, ten versions of the truth. And the company starts over every time it switches vendors.

### The only way through we see: one shared memory, one protocol

We think there is one way to avoid these failures at enterprise scale: a shared, company-owned memory that every agent consults before it acts, reports to after it acts, and that speaks first when something changes. That memory learns continuously from how the company actually works: it retires stale facts, keeps a receipt for every fact, and catches conflicts before two agents act on them.

For one memory to serve agents from every vendor, it needs one standard way to talk to them. That's Mem2A. We're building it on A2A because A2A already treats the other side as an agent rather than a tool: it can take its time, ask a question back, say no and call back later. That's exactly how memory needs to behave.

- **MCP** connects an agent to its tools. It gives agents hands.
- **A2A** connects agents to each other. It gives them a voice.
- **Mem2A** connects every agent to the company's shared memory. It gives them judgment.

### We've seen the human version work

At [Lenskart](https://nanothoughts.substack.com/p/making-people-more-productive-doesnt), a $12B public company, we compiled the company's own records into one shared memory. Across its ten major departments, the follow-ups it took to get stuck work moving fell 58% in four weeks, with no change in individual productivity. Agents will hit the same wall as people, only faster. Mem2A is how we plan to get ahead of it.

## How it works

Each action is one A2A task.

| Step | A2A primitive | What happens |
| --- | --- | --- |
| Discover | Agent Card | Memory declares the Mem2A extension, how agents can listen for changes, and how an agent proves who it acts for. |
| Negotiate | `SendMessage` opens a task | The agent sends an **intent**. Memory answers with a versioned **dossier**, asks a question back, or refuses. |
| Listen | Push notifications, `SubscribeToTask` | While the task is open, memory sends a new dossier version whenever something the agent relied on changes. |
| Commit | `SendMessage` on the same task | The agent reports what it did against the dossier version it used. Memory records it as a claim and completes the task. |

Tom's agent is about to send Acme a renewal quote. It opens a task with an intent:

```json
{
  "action": "send_quote",
  "summary": "Send Acme a renewal quote",
  "entities": ["account:acme", "doc:acme-renewal-quote"],
  "onBehalfOf": "user:tom"
}
```

Memory answers with a dossier (trimmed here) and keeps the task open:

```json
{
  "version": "12",
  "summary": "Hold the quote: legal paused Acme pricing on Friday.",
  "facts": [
    {
      "id": "f-311",
      "version": "3",
      "statement": "Legal paused new pricing for Acme on Friday, pending a contract review.",
      "status": "confirmed",
      "confirmedBy": "user:general-counsel",
      "source": { "kind": "meeting", "ref": "meeting:legal-weekly-2026-09-25" }
    }
  ],
  "precedent": [],
  "constraints": [
    {
      "id": "c-17",
      "statement": "Do not send Acme new pricing until legal clears it.",
      "level": "must",
      "basis": ["f-311"]
    }
  ],
  "watching": ["f-311", "c-17"]
}
```

On Monday legal clears the pricing, and memory pushes dossier 13 with the approved number. Tom's agent sends the quote and commits against version 13. Memory records it as a claim, and Priya's agent, which is watching Acme, hears about it before she opens her inbox. Every message of that exchange is in [`spec/v0.1/examples`](spec/v0.1/examples), and [`examples/acme-quote`](examples/acme-quote) runs it.

## Principles

1. **Memory is an agent, not a tool.** Mem2A rides on A2A so memory can take its time, ask a question back, say no, and call back later.
2. **Memory carries the rules; enforcement lives elsewhere.** Anything an agent can read, it can try to game. Blocking an action is the job of a sandbox or watchdog outside the agent's reach.
3. **Permissions follow the source.** If a person couldn't read the source, their agent can't see what memory learned from it.
4. **Every fact has a receipt:** a source, a version, and who or what confirmed it. What an agent reports stays a claim until a person or a system of record confirms it, so one poisoned agent can't plant a fact in everyone's memory.
5. **Newer decisions supersede older ones.** Stale facts are retired, not left to compete.

## What Mem2A is, and isn't

- **It is** a protocol for agents to consult and update a shared company memory, built as an A2A extension.
- **It isn't** a memory product. Bring your own memory system; Mem2A defines how agents talk to it.
- **It isn't** a replacement for MCP or A2A. It sits alongside them.
- **It isn't** a boss agent. Memory never hands out work; it keeps track of what the company has decided is true and allowed.
- **It isn't** an enforcement layer. Memory carries the rules; sandboxes and watchdogs outside the agent's reach enforce them.

## What's in this repo

| Path | What it is |
| --- | --- |
| [`spec/v0.1/mem2a.md`](spec/v0.1/mem2a.md) | The specification. |
| [`spec/v0.1/schemas`](spec/v0.1/schemas) | JSON Schemas for every Mem2A payload (normative). |
| [`spec/v0.1/examples`](spec/v0.1/examples) | Worked A2A exchanges, message by message. |
| [`python`](python) | A reference memory server and agent client, built on the A2A Python SDK. |
| [`examples/acme-quote`](examples/acme-quote) | The Acme story, end to end, in one command. |
| [`conformance`](conformance) | Checks that the examples follow the schemas and message rules. |
| [`docs`](docs) | How it works, FAQ and roadmap. |
| [`adrs`](adrs) | Why the design is the way it is. |

## Try it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e "python[dev]"
python examples/acme-quote/demo.py            # the Acme story, end to end
pip install -r conformance/requirements.txt
pytest python/tests conformance               # the tests
```

## Open questions

- How should memory judge relevance from an intent without leaking facts across permission boundaries?
- What push semantics work for facts that change often (batching, debouncing, priority)?
- How do claims get confirmed at scale without a person in every loop?
- Should constraints be machine-checkable policy, natural language, or both?
- How should Mem2A work alongside MCP resources and subscriptions for teams already on MCP?

The full list is in [section 14 of the spec](spec/v0.1/mem2a.md#14-open-questions).

## Contributing

Open an issue with a use case Mem2A doesn't cover, or a failure mode we missed. Pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

We'd especially like to hear from teams running agents from more than one vendor in production, people building memory systems and knowledge graphs, and security researchers who want to break the claims-and-receipts model.

## License

[Apache 2.0](LICENSE)
