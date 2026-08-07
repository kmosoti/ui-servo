# Review log

Adversarial critic verdicts, one section per unit, appended in merge order.

Kept because the Gauntlet Loop's invariants are only auditable if the judging is
on the record: which unit, which critic, what it found, what was done about it.
Verdicts are written by the reviewer (`tools/review.sh`, schema
`tools/verdict.schema.json`); builders do not write their own entries.

Format per entry: unit id, critic, date, critical findings, minor findings,
resolution.

## Adversarial review outcomes, U0–U13

Every unit was built by an isolated Opus builder, then attacked by a fresh
`codex exec` critic (gpt-5.6-sol, read-only sandbox, schema-forced verdict).
Critical findings looped the builder; minor findings are recorded here.

Rounds run and criticals fixed:

| Unit | Rounds | Criticals fixed | Notable |
|---|---|---|---|
| U0 contract | 2 | 8 | deep-freeze of nested contract mappings; CSS injection via token values *and* names; wheel-packaged default contract |
| U1 evidence | 2 | 7 | cross-instance `flock`; lossless tagged payloads (bytes/inf/nan); `(turn, span)` composite join |
| U2 sanitizer | 2 | 21 | `data-hx-*` alias bypass; `hx-trigger` filter eval; WHATWG backslash origin escape; never raises on hostile input |
| U3 probe | 2 | 6 | exact duration/easing comparison; selector-aware CSSOM harvest; bounded beacon queue |
| U4 preview/ingest | 2 | 14 | real HTML tokenizer over regex; ordered head injection; bounded ingest + health endpoint |
| U5 sensor | 2 | 7 | per-observation artifact paths; child-span axe attribution; computed-style reduced-motion sweep |
| U6 site | 2 | 4 | `reducedMotionRequired` forwarding; htmx lifecycle classes in CSSOM; rem-base corruption |
| U7 island | 1 | 2 | lifecycle leak on reduced-motion remount; reconnect losing listeners |
| U8 judges | 2 | 11 | judges run with mutating tools disabled; prompt via stdin; no proxy trust |
| U9 critique | 1 | — | (findings recorded, see below) |
| U10 regulator | 1 | — | (findings recorded, see below) |
| U11 explore | 1 | — | (findings recorded, see below) |
| U12 gauntlet skill | 3 | 15 | per-comparison blind staging; builder dispatch payload; promotion protocol |
| U13 servo | — | — | integration-fixed in place (see below) |

### Open findings carried forward

The reviewer's last pass over U9, U10, U11 and U12 returned findings that are
real but did not block the end-to-end loop; they are the top of the backlog
rather than silently dropped:

- **U9** — `Candidate.notes` is builder-controlled prose (mitigated: blindness-
  checked at staging *and* at prompt build); duplicate same-family judges are
  called before dedup; the one-re-ask bound is not enforced across the adapter's
  own retry; `swapped()` leaves side-bearing prose in `Finding.gap`.
- **U10** — sanitised markup and observed render are bound by convention, not by
  a hash; axe tag policy is configurable in a way that can narrow the gate.
- **U11** — `admit()` does not verify `regulator_report.variant_id` matches the
  variant; `exemplar_name()` is non-injective across `part`/`id` boundaries.
- **U12** — the skill still describes one servo invocation per part while the
  authoritative CLI is per-part; critic tool confinement is a role contract, not
  a sandbox.

### Integration fixes applied by the orchestrator

Found by running the loop rather than by reading it:

1. **Blindness false positive on the operator's own path.** `enforce_blindness`
   scanned whole absolute paths, so a scratch directory named `/tmp/claude-1000/`
   failed every round on this machine. `blindness_violations` grew a `masked`
   parameter (length-preserving, so reported offsets stay true) and servo masks
   its `--out` root. The guard still polices every component servo generates.
2. **Blind staging leaked through my own candidates.** The first hero candidates
   carried `data-span-id="hero-agy-2"`; the guard stopped the round, which is
   exactly what it is for.
3. **Codex judge inherited `ultra` reasoning effort** from the operator's
   `config.toml` and timed out every call. `CodexJudge` now pins effort per call.
4. **A text-only family was silently abstaining.** The agy bridge returns an
   empty body for any prompt asking it to open a PNG, which is indistinguishable
   from "no opinion" — the panel quietly dropped from three families to two.
   Judges now declare `reads_images`; markup is inlined into every prompt so a
   text-only critic votes on the markup instead of abstaining.
5. **`FINGERPRINT` call sites** in the island suite passed no selector after a
   mid-flight refactor, so every canvas assertion queried `"undefined canvas"`.

### A structural constraint found by running the loop

With three judging families and the self-preference guard (a family never judges
its own work), a pairwise comparison between candidates from two *different*
families excludes two of three judges and leaves one. One vote can never satisfy
"at least two decorrelated critics", so **every such comparison escalates, no
matter how good the candidates are**. Measured, not theorised: the first live
hero round escalated all three comparisons for exactly this reason.

The eligible-judge count is `panel_families - families({A, B})`. For a 3-family
panel to reach a verdict, both candidates in a comparison must come from at most
one family. Three workable configurations:

1. **One builder family per round** (used for the demo round): all candidates
   from one family, so every comparison excludes one judge and two remain.
2. **Builders outside the panel** — the ideal, and the reason the design does not
   hard-code the roster: a fourth CLI that only builds restores full variety.
3. **A larger panel.** Five families tolerate two builder families per comparison.

This is a property of the method, not a defect in the code, and the guard is
correct to escalate rather than accept a single opinion. It is recorded here
because it constrains how a round is *staffed*, which is not obvious until a
round comes back unanimously escalated.
