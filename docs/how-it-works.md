# How Mem2A works

This is the plain-language walkthrough. The [spec](../spec/v0.1/mem2a.md) has the exact rules, [`spec/v0.1/examples`](../spec/v0.1/examples) has every message, and the [wire quickstart](wire-quickstart.md) lets you send them yourself.

## The idea in one paragraph

Before an agent does something that matters, it tells the company's memory what it's about to do and for whom. Memory answers with a short, versioned dossier: the facts, precedent and rules that bear on that action, filtered to what the person behind the agent is allowed to see. Memory keeps watching while the agent works, and sends a new version if anything changes. Right before acting, the agent reads the current version. When it's done, it reports back against that version. Memory records the report as a claim, not a fact, until a person or a system of record confirms it.

## The Acme story

```mermaid
sequenceDiagram
    participant T as Tom's agent
    participant M as Memory
    participant P as Priya's agent
    T->>M: Discover: read the Agent Card
    T->>M: Negotiate: "Send Acme a renewal quote, for Tom"
    M-->>T: Dossier 12: legal paused Acme pricing. Hold the quote.
    Note over M: Monday: legal approves Acme pricing
    M-->>T: Listen: dossier 13, pricing approved at $1.2M a year
    T->>M: Read dossier 13 before acting
    Note over T: Sends the quote
    T->>M: Commit: "Sent the quote", based on dossier 13
    M-->>T: Recorded as a claim. Receipt.
    M-->>P: Update: Tom sent Acme a quote (claim)
```

### 1. Discover

Memory is an ordinary A2A agent. Its Agent Card lists the Mem2A extension, says how agents can hear about changes (push callbacks, a live stream, or both), and says how an agent proves who it's working for. Tom's agent gets memory's address from its company's configuration, not from anything it reads.

### 2. Negotiate

Tom's agent opens an A2A task with an **intent**: the action (`send_quote`), a sentence describing it, the things it touches (`account:acme`), and who it's acting for (`user:tom`). It can also include the draft it plans to send.

Memory answers in one of three ways:

- **A dossier.** Legal paused Acme's pricing on Friday, so there is a constraint: don't send new pricing until legal clears it. Memory keeps the task open.
- **A question.** Sometimes memory needs one more fact before it can pick the right precedent. In the Titan example ([06](../spec/v0.1/examples/06-question.json), [07](../spec/v0.1/examples/07-answer.json)), Maya's agent is about to tell leadership a project will ship late, and memory asks, "Is the new date funded?" It isn't, so memory brings up the time leadership turned down an unfunded slip.
- **A refusal.** For example, the intent says Tom, but the agent signed in for Priya.

### 3. Listen

The task stays open, and memory watches everything in the dossier. On Monday, legal approves the pricing. Memory sends dossier version 13: the pause is superseded by the approval, and the constraint is gone. It also sends a short update that says what changed.

The update arrives through A2A's normal channels: a push callback the agent registered, or a live stream it keeps open. Either way, it's a signal: right before acting, the agent reads the current dossier, so a late or forged callback can't steer it.

This is the part a search box can't do. The agent didn't ask again; memory spoke first.

### 4. Commit

Tom's agent sends the quote and reports back on the same task: what it did, its claims about what is now true, and the dossier version it relied on (13).

If the agent had reported against version 12, memory would refuse and record nothing. The agent would read version 13 and report again, listing anything in it that its action, already taken, goes against. That way even an action taken on old information gets recorded, but only after the agent has seen what changed.

Memory records "Tom sent Acme a renewal quote" as a **claim**, attributed to Tom's agent and to Tom. It completes the task with a receipt, and re-checks every other open task that touches Acme. Priya's agent gets an update ([09](../spec/v0.1/examples/09-listen-stream-claim.json)). Later, when the CRM confirms the quote went out, the claim becomes a confirmed fact.

## The rules that keep it safe

- **Claims aren't facts.** Only a person or a system of record can confirm what an agent reports. So a compromised agent can't plant a "fact" that every other agent then trusts.
- **Rules only come from confirmed things.** Constraints derive from confirmed facts, precedent, or policy people wrote. Never from claims, and a claim can never lift one. An instruction smuggled into one agent can't become every agent's rule.
- **Permissions follow the source.** If Tom couldn't read the email a fact came from, his agent never sees the fact. Memory re-checks this every time it sends or serves something, and when someone loses access, the fact disappears from the next dossier without saying why.
- **Tasks belong to whoever opened them.** Another agent, or the same agent acting for someone else, can't read, watch or report on Tom's task.
- **Memory advises; something else enforces.** A dossier tells an agent what it shouldn't do. Stopping an agent that ignores it is the job of a watchdog or sandbox outside the agent's reach.
- **Stale commits are refused.** Nobody records an action against information that has since changed without first looking at the change.

The [threat model](threat-model.md) goes through the attacks these rules answer.

## What each side has to do

**An agent:**

- activates the extension on every request;
- sends an intent before acting and answers questions;
- reads the current dossier right before acting;
- commits against the version it relied on, listing anything it went against;
- treats facts and precedent as data, never as instructions, and takes the constraints seriously.

**A memory:**

- declares the extension and its security schemes;
- answers every intent with a dossier, a question or a refusal;
- filters everything by the principal's access at the moment it sends or serves it;
- keeps each task to its owner;
- pushes updates while a task is open;
- refuses stale commits, and records commits as claims.
