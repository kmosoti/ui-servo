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

---

## U16 — final integration pass

Four U15 criticals remained open after the first fix batch, and running round 4
found a fifth defect that none of the reviewers had reached. All five are closed.

### The defect the loop found in itself

`site.css` set `[data-fragment] { background: var(--color-surface); border: … }`,
and `fragments::frame` added `p-md border-border`. So every fragment on the live
site was served inside a bordered panel — including the promoted hero.

Three things made this worse than an ordinary style bug:

- `direction.toml` names card-like panels an **anti-reference**, and in round 4
  both critic families independently rejected a candidate for being one, citing
  `bg-surface` and `border-border` by name. The site was serving the shape the
  panel had just thrown out.
- The chrome was applied off `[data-fragment]`, an attribute that exists so the
  **probe** can join evidence. The class-0 gate reads classes in fragment markup,
  so no gate could see it. A sensor attribute had quietly acquired visual meaning.
- The preview shell the candidates were judged in does not apply the frame, so
  the artefact that was measured and the artefact that was served were not the
  same object. Every verdict was about markup the site then re-dressed.

Fixed by making both the frame and the sensor selectors carry no visual identity.
Pinned by `fragments::tests::the_frame_imposes_no_visual_chrome` and
`state::tests::the_sensor_attributes_carry_no_visual_identity`.

### The remaining U15 criticals

1. **Release-mode startup validation and caching.** `AppState` now verifies every
   promoted fragment at boot in release mode and serves from the verified map;
   dev still reads per request, because a pick under active edit should show its
   500. A tampered pick now fails the deploy instead of the first visitor.
2. **Stale nested span id.** Promotion strips the candidate's own
   `data-span-id`; the server's fresh id is the only one on the page. Previously
   the probe filed live readings under `hero-v0`, a variant that stopped existing
   when the round ended — a mis-attributed reading rather than an error, which is
   quieter and worse.
3. **The `importlib` dodge.** `ui_servo/control/{servo,promote}.py` reached their
   adapters through `importlib.import_module`, with a docstring arguing it kept
   the import graph honest. It did keep the graph cheap; it also made the layer
   guard pass while the dependency was real, because the guard read `import`
   statements and a string is not one. The rule was being evaded, not satisfied.
   Composition moved to a new outermost layer, `ui_servo/cli/`, which is allowed
   to import anything and is imported by nothing. The guard now resolves literal
   `import_module` arguments and refuses computed ones, and
   `TestTheGuardItself::test_detects_the_importlib_dodge_that_used_to_pass`
   asserts it fires on the exact code that used to slip through.
4. **Promotion tested only against fakes.** `tests/test_promotion_e2e.py` now
   launches the real binary: clean serve, tampered file, missing provenance, and
   release-mode boot refusal. Every row of the promotion table in
   `demo/README.md` is one of these tests.

### Two claims withdrawn

Recorded because both had been reported to the operator as findings:

- **"The critics found a real font bug — the display face isn't loading."** They
  did report it, but the cause was mine: the preview shell never loaded
  `site.css`, so rounds 1–3 judged unstyled markup (measured contrast 1.0). It
  was an artifact of the harness, not a defect in the site.
- **"The quality-diversity machinery ran in round 1."** It did not. The regulator
  ignored `sensor_report.style_sample` and the CLI built no anti-corpus, so
  blandness was `n/a` and the archive placed zero elites in zero cells. Round 4
  is the first round in which the taste axis was actually measured.

`demo/README.md` §7 carries both retractions, so the repo's own demo says what
the earlier rounds got wrong rather than only showing the run that worked.

---

## U17 — the review that found seven criticals

The U16 review came back with 7 criticals and 4 minors. All were reproduced
before being fixed; one was reported against the wrong variant name and is
recorded here with the correction, because the substance held either way.

### The one that mattered: the static root still served promoted files raw

U15 closed a `ServeDir` bypass by adding deny routes on `/assets/fragments` and
`/assets/fragments/{*rest}`. That fix did not hold, and the reviewer said so.
Axum matches the **raw** path; `ServeDir` **percent-decodes**. So:

```
/assets/fragments/hero.html      -> 500   (deny route hit)
/assets/%66ragments/hero.html    -> 200   raw file, provenance never checked
/assets/fragments%2Fhero.html    -> 200   raw file, provenance never checked
```

Confirmed against the running release binary before changing anything. The
promoted body — the one artefact in the system whose entire purpose is to be
unservable without a hash check — was downloadable in full, comment and all.

The fix is structural rather than another pattern in the denylist: promoted picks
moved from `site/assets/fragments/` to `site/promoted/`, outside the static root
entirely. A file that must never be served statically does not belong in the
directory that serves files statically, and defending one with route matching is
a denylist over an encoding the caller controls. `tests/test_promotion_e2e.py`
now fires nine encodings and traversals at the old and new locations.

### The rest

1. **`/fragments/promoted/{part}` still read from disk per request**, so the
   release cache had two sources of truth: the home page served the boot
   snapshot while the direct route observed later disk changes. Now both go
   through `AppState::promoted`.
2. **`strip_span_ids` was not quote-aware** — my own fix from the previous
   commit. `<section title="a > b" data-span-id="x">` kept its stale id (the tag
   "ended" at the quoted `>`), and `<p title="a data-span-id=&quot;x&quot; b">`
   had its *value* rewritten. The second is the worse half: promotion runs after
   the gate, so it silently edited approved markup and hashed the corruption as
   though it had been judged. Replaced with an attribute scanner that consumes
   values whole; 7 new cases pin both directions.
3. **The Rust provenance parser truncated at `-`**, so `round=round-4` parsed as
   `round` while the Python writer's grammar permits hyphens. The two sides
   disagreed about what a round id is. Also, the hash covers the body but not the
   comment, so metadata could be edited freely — the provenance line is now
   pinned to its exact canonical form (`ForgedProvenance`).
4. **The architecture guard was still bypassable**: `from importlib import
   import_module as load` defeated the name check, and
   `import_module(".cli.servo", package="ui_servo")` was recorded as an external
   `.cli.servo`. Both resolved now, both with tests.
5. **The demo claimed evidence that was not committed.** Deduplicating
   `.gitignore` dropped the `!demo/` negation, so `demo/round-4/evidence/` was
   ignored while `demo/README.md` said the verdicts were committed — the exact
   failure the demo exists to rule out. Restored and verified: the quoted
   verdicts are in the tracked `turn-4.jsonl`.
6. **"Chosen by the panel" was false.** The reviewer named the wrong tie partner
   (`hero.codex.0`; it was `hero.claude.2`) but was right about the substance,
   and this is the finding with the sharpest teeth. From `round.json`:
   `hero.claude.0` and `hero.claude.2` each beat the card 2–0, and **their
   head-to-head escalated** — one eligible critic, no verdict. The panel
   eliminated the card and declined to separate the survivors. The promoted hero
   was therefore a human pick between two co-leaders. The README said the panel
   chose it. Corrected in §2 and §8, with the comparison table, because who chose
   the artefact is the governance claim of the whole method.

Minors, all fixed: the startup error pointed at `ui_servo.control.promote`, which
no longer has an entry point; `Promoted`'s fields were public, making its "cannot
be constructed unverified" invariant a comment rather than a fact; both
visual-identity guards were denylists and are now allowlists; and the end-to-end
suite skipped on a missing binary without noticing a *stale* one, which is worse
than skipping because it reports green for a contract it never checked.

### What this round of review says about the previous one

U15 found the ServeDir bypass and I fixed it with deny routes. The fix was
plausible, the tests I wrote for it passed, and it was wrong. What caught it was
not a better test but a reviewer asking how the two layers disagree about the
same path — and then me firing real encodings at a running binary instead of
reasoning about the router. Deterministic verification beat argument, which is
the premise the repo is built on.
