---
template: home.html
title: Mem2A
description: Mem2A is an open protocol, built on A2A, that lets any AI agent consult a company's shared memory before it acts, hear when that changes, and report back after.
hide:
  - navigation
  - toc
---

<div class="m2a-section" markdown>

## How it works

Memory is an ordinary A2A agent, and each action an agent takes is one A2A task. Any agent that speaks A2A can use any memory that implements Mem2A.

<ol class="m2a-steps" markdown="block">
<li markdown="block">
<h3>Discover</h3>
<p>The agent reads memory's Agent Card, which declares Mem2A and how an agent proves who it acts for.</p>
<p class="m2a-steps__wire"><code>GET /.well-known/agent-card.json</code></p>
</li>
<li markdown="block">
<h3>Negotiate</h3>
<p>Before acting, the agent says what it's about to do and for whom. Memory answers with a versioned dossier of facts, precedent and rules, asks a question, or refuses.</p>
<p class="m2a-steps__wire"><code>SendMessage</code></p>
</li>
<li markdown="block">
<h3>Listen</h3>
<p>While the task is open, memory sends a new version whenever something in the dossier changes. The agent reads the current version right before it acts.</p>
<p class="m2a-steps__wire"><code>SubscribeToTask</code>, push notifications, <code>GetTask</code></p>
</li>
<li markdown="block">
<h3>Commit</h3>
<p>After acting, the agent reports what it did against the latest version it read. Memory records the report as a claim until a person or a system of record confirms it.</p>
<p class="m2a-steps__wire"><code>SendMessage</code></p>
</li>
</ol>

[Follow the story step by step](how-it-works.md){ .m2a-more } [See every message](../spec/v0.1/examples/README.md){ .m2a-more }

</div>

<div class="m2a-section" markdown>

## What it guarantees

<dl class="m2a-guarantees" markdown="block">
<div markdown="block">
<dt>Claims aren't facts</dt>
<dd markdown="span">What an agent reports stays a claim until a person or a system of record confirms it, so one compromised agent can't plant a fact that every other agent trusts. [Why](../adrs/0004-claims-are-not-facts.md)</dd>
</div>
<div markdown="block">
<dt>Permissions follow the source</dt>
<dd markdown="span">An agent never sees a fact its principal couldn't read at the source, checked every time memory sends anything. [Why](../adrs/0005-permissions-follow-the-source.md)</dd>
</div>
<div markdown="block">
<dt>Memory advises; something else enforces</dt>
<dd markdown="span">Dossiers tell agents what they shouldn't do. Blocking an agent that ignores them is the job of a gateway or sandbox outside the agent's reach. [Why](../adrs/0003-memory-carries-rules-enforcement-elsewhere.md)</dd>
</div>
<div markdown="block">
<dt>Nothing is recorded against old information</dt>
<dd markdown="span">A report based on an outdated dossier is refused until the agent has seen what changed, and anything its action went against goes to a person. [Why](../adrs/0006-stale-commits-are-refused-then-reconciled.md)</dd>
</div>
<div markdown="block">
<dt>Tasks belong to their owner</dt>
<dd markdown="span">Another agent, or the same agent acting for someone else, can't read, watch or report on a task. [Why](../adrs/0009-tasks-belong-to-their-owner.md)</dd>
</div>
<div markdown="block">
<dt>Nothing new to deploy for A2A</dt>
<dd markdown="span">Mem2A adds no methods and no task states. It gives structure to what A2A already has: data parts, artifacts, streams and push notifications. [Why](../adrs/0001-build-on-a2a.md)</dd>
</div>
</dl>

</div>

<div class="m2a-section" markdown>

## Find your way in

<ul class="m2a-paths" markdown="block">
<li markdown="block">
<h3>Building an agent</h3>
<p>Negotiate before acting, listen for changes, commit after. About a page of Python, or plain HTTP from any language.</p>
<p markdown="span">[Python SDK](../python/README.md) [Wire quickstart](wire-quickstart.md)</p>
</li>
<li markdown="block">
<h3>Running a memory or knowledge graph</h3>
<p>Answer intents from what your memory already knows, and test yours against the spec from the outside.</p>
<p markdown="span">[Implementer's guide](implementers-guide.md) [mem2a-conform](../python/README.md#test-your-own-memory)</p>
</li>
<li markdown="block">
<h3>Running tools or a gateway</h3>
<p>Sit where actions execute: hold a tool call the company's rules forbid, and report what ran, without changing the agent.</p>
<p markdown="span">[Who it's for](ecosystem.md) [Tool gateway example](../examples/tool-gateway/README.md)</p>
</li>
<li markdown="block">
<h3>Reviewing the standard</h3>
<p>Every rule in RFC 2119 language, normative JSON Schemas, and the decisions behind them.</p>
<p markdown="span">[Specification](../spec/v0.1/mem2a.md) [Decision records](../adrs/README.md) [Threat model](threat-model.md)</p>
</li>
</ul>

</div>

<div class="m2a-section m2a-section--status" markdown>

## Status

Mem2A 0.1 is a draft, open for review, and now is the cheapest time to change it. The plan is to propose it to the A2A project as an official extension, and to hand it to a neutral home with maintainers from several companies. Read the [roadmap](roadmap.md) and the [governance](../GOVERNANCE.md), or [take part](community/index.md).

</div>
