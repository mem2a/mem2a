# Governance

Mem2A is at draft 0.1. This is how decisions get made today, and how we intend to hand them to a neutral home.

## Today

- The maintainers in [MAINTAINERS.md](MAINTAINERS.md) steward the specification and the reference implementation.
- **Normative changes** to the spec (anything that changes what an implementation must or should do) start as an issue and land as a pull request. They stay open for comment for at least **seven days** after the pull request is ready for review.
- **Lazy consensus.** A change is accepted if, after the comment period, no one has raised an unresolved objection. An objection is a review comment that requests changes and gives a reason. Objections are resolved by discussion. If that fails, the maintainers decide, and record what they decided and why in the pull request or in an [ADR](adrs).
- **Appeals.** Anyone can ask for a decision to be reconsidered by opening an issue labeled `governance`.
- **Review.** Once there are two or more maintainers, a normative change needs approval from a maintainer who didn't write it. Until then, the sole maintainer follows the same comment period and records every decision.
- **Versioning.** Every breaking change to the protocol gets a new version and extension URI.
- **Editorial changes** (wording, typos, examples that don't change behavior) can merge after one maintainer's review.

## Vendor neutrality

Mem2A was started by the team at [Sentra](https://sentra.app), but it isn't tied to any memory product. Conformance is judged against the spec, not against any implementation, including ours. We're actively looking for [co-maintainers from other organizations](https://github.com/mem2a/mem2a/issues/19), especially teams building memory systems or running agents from several vendors.

## Intellectual property

Everything in this repository is licensed under the [Apache License 2.0](LICENSE), which includes a patent license from each contributor for their contributions. Whether the specification should also carry an explicit royalty-free patent commitment for implementers is [an open decision](https://github.com/mem2a/mem2a/issues/20), to be made before 1.0.

## Where we're headed

We intend to propose Mem2A to the A2A project under its [extension governance](https://github.com/a2aproject/A2A/blob/main/docs/topics/extension-and-binding-governance.md). The path:

1. An experimental extension, sponsored by an A2A maintainer.
2. An official extension, once there's a production-quality reference implementation, documentation and real adoption, and A2A's Technical Steering Committee approves it.

If another neutral home fits better, we'll say so here. See the [roadmap](docs/roadmap.md).

## Becoming a maintainer

Maintainers are contributors who have made sustained, high-quality contributions and reviews. Existing maintainers nominate new ones in an issue and add them by consensus.
