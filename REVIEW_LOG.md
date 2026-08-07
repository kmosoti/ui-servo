# Review log

Adversarial critic verdicts, one section per unit, appended in merge order.

Kept because the Gauntlet Loop's invariants are only auditable if the judging is
on the record: which unit, which critic, what it found, what was done about it.
Verdicts are written by the reviewer (`tools/review.sh`, schema
`tools/verdict.schema.json`); builders do not write their own entries.

Format per entry: unit id, critic, date, critical findings, minor findings,
resolution.
