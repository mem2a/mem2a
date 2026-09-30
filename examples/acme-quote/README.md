# The Acme quote

The spec's worked example ([examples 02 to 05](../../spec/v0.1/examples)), played end to end against the reference memory, with two agents:

- **Tom's sales agent** is about to send Acme a renewal quote. It negotiates first and leaves a webhook for updates.
- **Priya's account agent** is about to draft a follow-up to Acme. It negotiates too and keeps a `SubscribeToTask` stream open.

```sh
pip install -e "python[dev]"     # from the repository root
python examples/acme-quote/demo.py
```

The script starts a memory and a webhook receiver on localhost, runs the story, prints what each side sees, and exits in about a second.

## What happens

| Step | What memory knows | What the agents see |
| --- | --- | --- |
| 1 | Legal paused Acme pricing (`f-311`), so policy `c-17` applies. Precedent `p-4`: a Globex quote sent during a legal review had to be withdrawn. | Tom gets dossier 12: hold the quote. |
| 2 | Legal approves the pricing: `f-340` supersedes `f-311`, so `c-17` and `p-4` no longer apply. | Memory pushes dossier 13 and an update to Tom's webhook. |
| 3 | | Priya negotiates and gets dossier 14. |
| 4 | Tom's agent sends the quote and commits against dossier 13. Memory records a claim (`f-341`). | Tom gets a receipt. Priya's stream gets dossier 15, with Tom's claim. |
| 5 | The CRM confirms the claim. | Priya's stream gets dossier 16. Her agent cancels its follow-up. |

Dossier versions come from one counter for the whole memory, started at 12 so they match the spec's examples.

## Output

```
    tom | About to send Acme a renewal quote. Asking memory first.
 memory | dossier 12, awaiting-commit: "Hold: Do not send Acme new pricing until legal clears it."
        |   fact f-311 (confirmed by user:general-counsel): Legal paused new pricing for Acme on Friday, pending a contract review.
        |   fact f-208 (confirmed by system:crm): Acme's current contract renews on October 31, 2026.
        |   precedent p-4: In June, a renewal quote sent to Globex during a legal review had to be withdrawn.
        |   MUST c-17: Do not send Acme new pricing until legal clears it. (basis: f-311)
    tom | Holding the quote. Memory will call my webhook if this changes.

  legal | Approves Acme pricing at $1.2M a year (f-340 supersedes f-311).
 memory | push to tom: dossier 13 replaces 12.
        |   "Legal cleared Acme pricing."
        |   + fact f-340 (confirmed by user:general-counsel): Legal approved Acme renewal pricing at $1.2M a year.
        |   - fact f-311
        |   - constraint c-17
        |   - precedent p-4
    tom | No constraints left. Sending the quote.

  priya | About to draft a follow-up to Acme. Asking memory first.
 memory | dossier 14: "Nothing here blocks this. Latest: Legal approved Acme renewal pricing at $1.2M a year."

    tom | Quote sent. Committing against dossier 13.
 memory | receipt cm-1: f-341 recorded as a claim.
        |   Task COMPLETED, phase committed.
 memory | stream to priya: dossier 15 replaces 14.
        |   + fact f-341 (claim by user:tom): Tom sent Acme a renewal quote at $1.2M a year.

    crm | The CRM shows the quote went out: confirms f-341.
 memory | stream to priya: dossier 16 replaces 15.
        |   ~ fact f-341 (confirmed by system:crm): Tom sent Acme a renewal quote at $1.2M a year.
  priya | Tom already sent the quote, so no follow-up now. Canceling.
 memory | Task CANCELED, phase canceled. Memory stopped watching.
```

The first line on stderr is the development authenticator's warning: the demo uses unsigned `dev:` tokens, which are fine on localhost and never in production.
