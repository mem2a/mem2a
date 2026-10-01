# 0001: Build Mem2A as an A2A extension

**Status:** Accepted, 2026-09-30

## Context

A shared memory has to talk to agents from every vendor, so it needs a standard wire protocol. We considered three options: an MCP server, a new standalone protocol, and an extension to A2A.

Memory's side of the conversation isn't a function call. It sometimes needs time, sometimes needs to ask a question back, sometimes has to say no, and it has to speak first when something changes, long after the original request.

MCP is built around calling tools. It has added follow-up questions, long-running jobs and change alerts, but its center of gravity is still request and response. A new protocol would mean new SDKs, new security reviews and a new ecosystem to grow from nothing. A2A already has tasks that stay open, an input-required state, a rejected state, push notifications, streaming, Agent Cards with security schemes, and a mechanism for extensions.

## Decision

Mem2A is a profile extension of A2A v1.0. The memory is an A2A agent. Mem2A adds no RPC methods and no task states. It uses data parts with media types, metadata for sub-states (the phase), artifacts for the dossier and receipt, and A2A's own push notifications and streaming for updates.

## Consequences

- Any A2A agent can adopt Mem2A with an extension library rather than a new stack, and it inherits A2A's authentication, push and streaming.
- We are bound by A2A's state machine, so Mem2A's states live in metadata (`question`, `awaiting-commit`, and so on).
- We inherit A2A SDK gaps. The Python SDK (1.2.1) has no public way for a server to add events to an open task from outside a request; the reference implementation works around it (see [`python`](../python)).
- Mem2A has a path to become an official A2A extension through A2A's extension governance ([roadmap](../docs/roadmap.md)).
