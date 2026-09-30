# Contributing to Mem2A

Thanks for helping. Mem2A is an early draft, which is the cheapest time to change it.

## Ways to help

- **A use case Mem2A doesn't cover.** Open an issue with the "Use case" template.
- **A failure mode we missed**, or one Mem2A makes worse. Use the "Failure mode" template.
- **A change to the spec.** Open a "Spec change" issue before you write a pull request, so we can agree on the problem first.
- **An implementation**, in any language, or against your own memory system. Tell us about it; interoperability bugs are the most valuable bugs.
- **A security review.** Please read [SECURITY.md](SECURITY.md) first.

## Changing the spec

- Normative changes need an issue first, then a pull request that links to it.
- Write requirements in RFC 2119 language (MUST, SHOULD, MAY), one requirement per sentence.
- Keep the schemas, the examples and the text in step. If you change a payload, change its schema and every example that carries it.
- Run the conformance checks: `pytest conformance`.
- Record a significant design decision in [`adrs/`](adrs).
- Add a line to [CHANGELOG.md](CHANGELOG.md).
- Breaking changes get a new version and URI (`/v0.2`, ...). Before 1.0 they're expected, but say so in the pull request.

## Working on the reference implementation

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e "python[dev]"
pytest python/tests
python examples/acme-quote/demo.py
```

The reference implementation should stay small and readable. It is there to show the protocol working, not to be a production memory.

## Sign-off

We use the [Developer Certificate of Origin](https://developercertificate.org/). Sign off every commit with `git commit -s`, which adds a `Signed-off-by` line certifying that you wrote the change or have the right to submit it under the project's license.

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## Conduct

Everyone taking part is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).
