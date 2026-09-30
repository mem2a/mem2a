# Roadmap

## Now: draft 0.1 (private)

- [x] Specification text in RFC 2119 language
- [x] JSON Schemas for every payload, and worked examples
- [x] Conformance checks for the examples
- [x] Reference memory server and agent client on the A2A Python SDK, with the Acme demo
- [ ] Fresh-eyes review of the spec by people running agents from more than one vendor
- [ ] Security review of the claims-and-receipts model

## Before we make the repository public

- [ ] Resolve naming of phases, error codes and media types (breaking changes are cheap now)
- [ ] Register the `https://w3id.org/mem2a/` redirect with [w3id.org](https://w3id.org/)
- [ ] A second client, in TypeScript, to shake out anything Python-specific
- [ ] Tests that can run against any memory's URL, not just the reference server
- [ ] Publish the `mem2a` package to PyPI

## After it's public

- [ ] Open a proposal issue in [a2aproject/A2A](https://github.com/a2aproject/A2A), following A2A's [extension governance](https://github.com/a2aproject/A2A/blob/main/docs/topics/extension-and-binding-governance.md): an abstract, why the core protocol can't do this, and this draft.
- [ ] Find an A2A maintainer to sponsor Mem2A as an experimental extension (`experimental-ext-mem2a`).
- [ ] Draft 0.2, answering the open questions in [section 14](../spec/v0.1/mem2a.md#14-open-questions) as real deployments answer them.

## Toward 1.0

- [ ] Two independent implementations that interoperate
- [ ] Graduation to an official A2A extension, which needs a production-quality reference implementation, documentation, evidence of adoption and a TSC vote, or another neutral home if that fits better
- [ ] A stable URI (`/v1`) with a compatibility promise
