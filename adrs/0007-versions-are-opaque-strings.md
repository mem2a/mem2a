# 0007: Versions are opaque strings

**Status:** Accepted, 2026-09-30

## Context

Mem2A compares versions constantly: a commit's `basedOn` against the current dossier, a fact's version across updates. A2A carries data parts as protobuf `Value`s, which represent every JSON number as a double, so the integer `12` comes back as `12.0` in some SDKs. Implementations may also want to use content hashes or ETags instead of counters.

## Decision

Every version in Mem2A is a string, compared for equality only. Senders must not use JSON numbers for versions.

## Consequences

- Versions survive any A2A binding and SDK unchanged.
- Implementations are free to use counters, hashes or anything else.
- Clients can't order versions. They don't need to: memory always says which version is current.
