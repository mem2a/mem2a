# FAQ

### Isn't this just a boss agent?

No. A boss agent decides what everyone does next. Memory never hands out work. It keeps track of what the company has decided is true and allowed, and those decisions come from people. Agents stay peers; they just stop disagreeing about reality.

### Isn't this just search with extra steps?

Search answers the question an agent asks. The expensive failures come from questions the agent didn't know to ask: the precedent on another project, the pricing pause decided in a meeting it wasn't in. In Mem2A the agent says what it's about to do, and memory decides what it needs to know, then speaks first when that changes.

### Why not build it on MCP?

MCP was built for tools. It has added follow-up questions, long-running jobs and change alerts, but at heart you call a tool and it answers. Memory has to behave like a colleague: take its time, ask a question back, say no, and call back later when something changes. A2A was built for exactly that kind of counterpart, so Mem2A rides on A2A. A memory can still offer MCP resources for plain lookups. How the two should work together is an [open question](https://github.com/mem2a/mem2a/issues/7).

### Why not just give every agent all the company's documents?

Because the documents disagree with each other, and plenty of them are out of date. More pages don't tell an agent which one is true today, or which ones it's allowed to see.

### Won't one shared memory become a bottleneck, or a single point of failure?

One *logical* memory, not one machine. It can be replicated like any other service. Companies already run one identity provider that every app depends on; memory plays a similar role for what the company knows and has decided. And an agent can act without memory, just as it does today. It just acts without knowing what it shouldn't do.

### What if an agent ignores the dossier?

Then Mem2A alone won't stop it. Memory carries the rules; enforcement is the job of something the agent can't reach, such as a sandbox, an egress policy or a watchdog. Those enforcement points can consult the same memory.

### What stops a compromised agent from spreading bad instructions through memory?

What an agent reports is recorded as a claim, attributed to that agent and its principal, and only a person or a system of record can confirm it. Claims never become rules, and a claim can never lift one. An instruction smuggled into one agent can't become every agent's truth. The [threat model](threat-model.md) has the details.

### Can agents see things their user isn't allowed to see?

No. Permissions follow the source: a fact is shown only if the person behind the agent could read every source it came from, checked again every time memory sends anything. If the agent itself has narrower access than its user, the narrower one applies.

### Do I have to use Python?

No. Mem2A is A2A plus JSON, so any language that can send HTTP works. The [wire quickstart](wire-quickstart.md) shows every request with curl. The Python package is the reference implementation, and a [TypeScript client](https://github.com/mem2a/mem2a/issues/14) is wanted.

### Can I use it in production today?

Not yet. Mem2A is a draft, and breaking changes are expected before 1.0. The reference implementation keeps everything in memory and ships with development tokens. It's for learning, prototyping and testing your own implementation. The [roadmap](roadmap.md) says what comes next.

### Does this lock us into Sentra?

No. Mem2A is an open specification under Apache 2.0, and it doesn't care how a memory is built. [Sentra](https://sentra.app) builds one implementation; the reference implementation in this repo is another, deliberately simple one. Conformance is judged against the spec, not against any product, and we want [co-maintainers from other companies](https://github.com/mem2a/mem2a/issues/19).

### How do I get involved?

See [Take part](community/index.md). The shortest path is to tell us about [an agent that did something it shouldn't have](https://github.com/mem2a/mem2a/issues/new?template=use-case.yml).

### Why "Mem2A"?

Memory-to-agent, by analogy with A2A (agent-to-agent). The name "M2A" was taken.
