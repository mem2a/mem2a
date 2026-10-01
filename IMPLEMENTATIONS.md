# Implementations

Implementations of Mem2A 0.1 that we know of. Conformance is what `mem2a-conform` reports against each one; see the [implementer's guide](docs/implementers-guide.md#testing).

| Implementation | Role | Language | Built on | Conformance | Maintained by |
| --- | --- | --- | --- | --- | --- |
| [mem2a reference](python) | Memory, agent client, conformance checker | Python | a2a-sdk 1.2 | `mem2a-conform`: all 14 checks pass, including writes and Listen | Mem2A maintainers |

## Add yours

Open a pull request that adds a row, and paste the output of `mem2a-conform --json` against your implementation in the description. Partial conformance is welcome. Say which checks you skip and why, so that others know what to expect.

Agents and client libraries can be listed too: say which memories you've tested against.

## Interoperability

If two implementations don't work together, please file an [interop report](https://github.com/mem2a/mem2a/issues/new?template=interop-report.yml), even if you're not sure whose bug it is. Those reports are how the spec gets tighter.
