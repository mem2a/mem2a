# Contributing to Mem2A

Thanks for helping. Mem2A is an early draft, which is the cheapest time to change it. You don't need to write code to make a difference.

## Ways to help

| You can | How |
| --- | --- |
| Tell us about an agent that did something it shouldn't have | [Use case](https://github.com/mem2a/mem2a/issues/new?template=use-case.yml): three plain questions |
| Weigh in on a design question | Comment on an [open question](https://github.com/mem2a/mem2a/issues?q=is%3Aissue+is%3Aopen+label%3A%22open+question%22) |
| Try Mem2A on real agents | [Become a design partner](https://github.com/mem2a/mem2a/issues/new?template=design-partner.yml) |
| Point out a failure mode we missed | [Failure mode](https://github.com/mem2a/mem2a/issues/new?template=failure-mode.yml) |
| Ask anything | [Question](https://github.com/mem2a/mem2a/issues/new?template=question.yml) |
| Change the spec | Open a [spec change](https://github.com/mem2a/mem2a/issues/new?template=spec-change.yml) first, then a pull request |
| Build an implementation, in any language | Start with the [implementer's guide](docs/implementers-guide.md); get [listed](IMPLEMENTATIONS.md) |
| Report two implementations that don't work together | [Interop report](https://github.com/mem2a/mem2a/issues/new?template=interop-report.yml) |
| Break the security model | Read [SECURITY.md](SECURITY.md) first |

## Your first code contribution, in about 15 minutes

1. Pick an issue labeled [good first issue](https://github.com/mem2a/mem2a/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22), and say in a comment that you're on it.
2. Set up:

   ```sh
   git clone https://github.com/mem2a/mem2a && cd mem2a
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e "python[dev]" -r conformance/requirements.txt
   ```

3. Make your change, then run what CI runs:

   ```sh
   ruff check python examples && ruff format --check python examples
   mypy --strict python/src
   pytest python/tests conformance
   for demo in examples/*/demo.py; do python "$demo"; done
   python examples/sales-agent/agent.py --self-test
   ```

4. Commit with a sign-off (`git commit -s`, see below), push to your fork, and open a pull request that links the issue.

## Changing the spec

- Normative changes need an issue first, then a pull request that links to it. They stay open for comment for at least seven days ([GOVERNANCE.md](GOVERNANCE.md)).
- Write requirements in RFC 2119 language (MUST, SHOULD, MAY), one requirement per sentence.
- Keep the schemas, the examples and the text in step. If you change a payload, change its schema and every example that carries it, and copy the schemas into `python/src/mem2a/schemas` (a test checks they're identical).
- Run the conformance checks: `pytest conformance`.
- Record a significant design decision in [`adrs/`](adrs).
- Add a line to [CHANGELOG.md](CHANGELOG.md).
- Breaking changes get a new version and URI (`/v0.2`, ...). Before 1.0 they're expected, but say so in the pull request.

## Adding an example

Put it in `examples/<name>/` with a README that says what it shows and how to run it. CI runs every `examples/*/demo.py`, so a script with that name must exit 0 on its own, in a few seconds. Examples in other languages are especially welcome ([#17](https://github.com/mem2a/mem2a/issues/17)).

## Working on the reference implementation

The reference implementation in [`python/`](python) should stay small and readable. It's there to show the protocol working and to test other implementations, not to be a production memory. Its [README](python/README.md) explains how it's organized.

## Where to talk

Use issues for now: questions, ideas and proposals are all welcome there. Please keep discussion about security problems private ([SECURITY.md](SECURITY.md)).

## Sign-off

We use the [Developer Certificate of Origin](https://developercertificate.org/). Sign off every commit with `git commit -s`, which adds a `Signed-off-by` line certifying that you wrote the change or have the right to submit it under the project's license.

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## Conduct

Everyone taking part is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).
