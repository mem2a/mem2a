# 0007: Versions are opaque strings, and payloads have no numbers

**Status:** Accepted, 2026-09-30. Extended the same day to cover every number, after an implementer's Go client failed to decode `0.0` as an integer.

## Context

Mem2A compares versions constantly: a commit's `basedOn` against the current dossier, an item's version across updates. A2A carries data parts and Agent Card params as protobuf `Value`s, which represent every JSON number as a double, so the integer `12` can come back as `12.0`. Strictly typed clients then fail to decode it. Implementations may also want content hashes or ETags instead of counters.

## Decision

- Every version in Mem2A is a string, compared for equality only.
- Mem2A payloads and params contain no JSON numbers at all. Counts are replaced by lists (a receipt lists its conflicts rather than counting them), and durations are ISO 8601 strings (`watchTimeout: "P7D"`).

## Consequences

- Payloads survive any A2A binding, SDK and language unchanged.
- Implementations are free to use counters, hashes or anything else as versions.
- Clients can't order versions. They don't need to: memory always says which version is current.
