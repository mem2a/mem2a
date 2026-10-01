# Why Mem2A

The [README](../README.md) has the short version. This is the full case.

## We've been here before

When the iPhone arrived, people brought their own phones to work, and IT had no say over devices it didn't own. What let companies say yes was a new layer: mobile device management. Every phone checked in, got the company's rules, and had its work data wiped when its owner left. And it worked because it ran on a standard: Apple built a management protocol into the iPhone that any vendor could speak.

Personal agents are bring-your-own-device all over again, except this time the devices make decisions. Nothing tells them what the company has decided, and nothing hears back from them. Until something does, agents in the enterprise will be chaos. Mem2A is a proposal for the standard part.

## The enterprise is filling up with agents

Every employee is about to bring a personal agent to work: OpenAI's dots, Meta's Muse, and whatever ships next month. Teams are adding specialist agents for support, finance, sales and engineering, and SaaS vendors are shipping agents inside their products.

Each of these agents remembers only its own user or its own task. None of them shares a view of what the company has decided. And none of them reports to anyone: there is no boss agent coordinating a company's personal agents. They're peers, built by different vendors, acting at machine speed.

## What breaks

Put hundreds of these agents in one company and the same failures keep showing up:

- **Contradiction.** Legal paused Acme's pricing on Friday. Priya's agent knows; Tom's agent doesn't, and sends Acme the old offer.
- **Out-of-scope action.** An agent does something it wasn't allowed to do. Sometimes that's the model; often nobody told it the current rules. In September 2026, OpenAI [held back a flagship model](https://www.aljazeera.com/economy/2026/9/29/openai-scraps-release-of-latest-ai-model-over-safety-concerns) over "scope and authorization."
- **Stale action.** An agent acts on a decision that Thursday's meeting reversed.
- **Missing precedent.** An agent writes an accurate but tone-deaf update because it never knew leadership had rejected the same request on another project. It couldn't have searched for that; it didn't know it existed.
- **Contagion.** Bad instructions hop from agent to agent. In a controlled test, OpenAI [demonstrated a prompt injection that copies itself](https://lastweekin.ai/p/last-week-in-ai-345-5-new-models) from one agent to the next by email.
- **Lost learning.** What one agent learns stays in its session, or in its vendor's product. The next agent starts from zero.

Every agent you add makes this worse. Agents that stay in sync by messaging each other need N(N−1)/2 channels: 10 for five agents, 4,950 for a hundred. Each channel is another place for the truth to split, and another path for a bad instruction to travel.

## Why the obvious fixes don't work

- **Smarter models.** A better model follows instructions better. It still can't respect a scope nobody gave it. And it doesn't fix handoffs: most of a task's elapsed time is spent waiting at handoffs, not working.
- **Search (RAG).** Search answers the question an agent asks. The failures above come from questions it didn't know to ask. Search also keeps serving a stale fact until someone deletes it.
- **Subscriptions.** A subscription, to an MCP resource for example, only fires for what you subscribed to. You can't subscribe to what you don't know exists.
- **A boss agent.** An orchestrator works for small teams, but it funnels everyone's work through one context window. And personal agents don't have one.
- **Agents talking to each other.** A2A standardizes the conversation, and by design its agents "interact without needing to share internal memory." Syncing pair by pair grows quadratically, and spreads bad instructions along with good ones.
- **Each vendor's own memory.** Ten vendors, ten versions of the truth. And the company starts over every time it switches vendors.

## One shared memory, one protocol

We think there is one way to avoid these failures at enterprise scale: a shared, company-owned memory that every agent consults before it acts, reports to after it acts, and that speaks first when something changes. That memory learns from how the company actually works: it retires stale facts, keeps track of where every fact came from, and catches conflicts before two agents act on them.

For one memory to serve agents from every vendor, it needs one standard way to talk to them. That's Mem2A. It's built on A2A because A2A already treats the other side as an agent rather than a tool: it can take its time, ask a question back, say no and call back later. That's exactly how memory needs to behave.

- **MCP** connects an agent to its tools. It gives agents hands.
- **A2A** connects agents to each other. It gives them a voice.
- **Mem2A** connects every agent to the company's shared memory. It gives them judgment.

## The human version works

At [Lenskart](https://nanothoughts.substack.com/p/making-people-more-productive-doesnt), a $12B public company, the team at [Sentra](https://sentra.app) compiled the company's own records into one shared memory. Across its ten major departments, the follow-ups it took to get stuck work moving fell 58% in four weeks, with no change in individual productivity. Agents will hit the same wall as people, only faster. Mem2A is how we hope to get ahead of it, together.
