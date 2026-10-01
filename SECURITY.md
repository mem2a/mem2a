# Security

Mem2A is a protocol for agents that act inside companies, so security problems in it matter. Thank you for looking.

## Reporting a problem

Please don't open a public issue for a security problem. Report it privately through GitHub's private vulnerability reporting on this repository (**Security → Report a vulnerability**). If that isn't available to you, contact a maintainer listed in [MAINTAINERS.md](MAINTAINERS.md) directly, or write to the team through [Sentra's contact page](https://www.sentra.app/contact) and ask for a private channel. Please leave the details of the problem out of that first message.

We aim to acknowledge reports within three working days.

## What's in scope

- **The specification:** ways the protocol lets information leak across permission boundaries, lets a claim become trusted or lift a rule without confirmation, lets instructions spread between agents through memory, lets one principal reach another's task, lets webhooks carry data out, or lets updates be forged or replayed.
- **The reference implementation** in [`python/`](python): bugs that break the spec's security requirements.

The [threat model](docs/threat-model.md) lists what we've considered so far. Finding something it misses is exactly what we hope for.

## Known limitations

- Mem2A is not an enforcement layer. An agent that ignores its dossier isn't a protocol vulnerability ([spec 11.1](spec/v0.1/mem2a.md#11-security-considerations)).
- The reference implementation's `DevTokenAuthenticator` and the sandbox's `--dev-admin` routes trust anyone. They exist for tests and demos only; never deploy them.
- Relevance leakage and the identity profile are open questions ([#1](https://github.com/mem2a/mem2a/issues/1), [#11](https://github.com/mem2a/mem2a/issues/11)). Ideas are welcome as ordinary issues.
