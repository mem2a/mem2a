# Security

Mem2A is a protocol for agents that act inside companies, so security problems in it matter. Thank you for looking.

## Reporting a problem

Please don't open a public issue for a security problem. Report it privately through GitHub's private vulnerability reporting on this repository (**Security → Report a vulnerability**). While the repository is private, contact a maintainer listed in [MAINTAINERS.md](MAINTAINERS.md) directly.

We aim to acknowledge reports within three working days.

## What's in scope

- **The specification**: ways the protocol lets information leak across permission boundaries, lets a claim become trusted without confirmation, lets instructions spread between agents through memory, or lets updates be forged or replayed.
- **The reference implementation** in [`python/`](python): bugs that break the spec's security requirements.

## Known limitations

- Mem2A is not an enforcement layer. An agent that ignores its dossier isn't a protocol vulnerability ([spec 11.1](spec/v0.1/mem2a.md#11-security-considerations)).
- The reference implementation's `DevTokenAuthenticator` accepts unsigned development tokens. It exists for tests and demos only; never deploy it.
- Relevance leakage is an [open question](spec/v0.1/mem2a.md#14-open-questions). Ideas are welcome as ordinary issues.
