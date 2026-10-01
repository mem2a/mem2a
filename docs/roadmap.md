# Roadmap

Each open item links to its issue. Comment there to help, or to argue with the plan.

## Draft 0.1 (now)

- [x] Specification text in RFC 2119 language
- [x] JSON Schemas for every payload, and worked examples
- [x] Conformance checks for the examples
- [x] Reference memory, agent client and sandbox server in Python, with the Acme demo
- [x] `mem2a-conform`, which checks much of the spec against any running memory
- [x] Outside review by an agent builder, a memory implementer, a newcomer, and a spec and security reviewer; fixes applied
- [x] Threat model
- [x] An A2A primer, a glossary and diagrams for readers new to A2A
- [x] A public repository, and a documentation site at [mem2a.github.io/mem2a](https://mem2a.github.io/mem2a/)
- [ ] Design partners: teams running agents from more than one vendor ([sign up](https://github.com/mem2a/mem2a/issues/new?template=design-partner.yml))
- [ ] Answers, from real deployments, to the [open questions](https://github.com/mem2a/mem2a/issues?q=is%3Aissue+is%3Aopen+label%3A%22open+question%22)

## Before 0.2

- [ ] An identity profile: tokens, delegation chains and scopes ([#11](https://github.com/mem2a/mem2a/issues/11))
- [ ] Schemas as an npm package with TypeScript types ([#13](https://github.com/mem2a/mem2a/issues/13)), and a TypeScript client ([#14](https://github.com/mem2a/mem2a/issues/14))
- [ ] A portable way to test Listen on any memory ([#15](https://github.com/mem2a/mem2a/issues/15))
- [ ] Freeze the names of phases, error codes and media types
- [ ] Publish `mem2a` to PyPI ([#22](https://github.com/mem2a/mem2a/issues/22)), and register the `w3id.org` redirect ([#21](https://github.com/mem2a/mem2a/issues/21))
- [ ] Co-maintainers from outside Sentra ([#19](https://github.com/mem2a/mem2a/issues/19))

## Toward an A2A extension

Following A2A's [extension governance](https://github.com/a2aproject/A2A/blob/main/docs/topics/extension-and-binding-governance.md):

- [ ] Raise what Mem2A needs from A2A and its SDKs ([#12](https://github.com/mem2a/mem2a/issues/12))
- [ ] Open a proposal issue on a2aproject/A2A, and find an A2A maintainer to sponsor Mem2A as an experimental extension
- [ ] Two independent implementations that interoperate, shown at a plugfest
- [ ] A patent policy for the specification ([#20](https://github.com/mem2a/mem2a/issues/20))
- [ ] Graduation to an official extension, which needs a production-quality reference implementation, documentation, evidence of adoption and a vote of A2A's Technical Steering Committee
- [ ] A stable URI (`/v1`) with a compatibility promise
