# Round 1 — the hero, end to end

A transcript of one real gauntlet round: four candidates in, one promoted to the
live site. Nothing here is staged; the commands are the ones in the README and
the verdicts are what the three CLIs actually returned.

## 1. Candidates

Four hero fragments, authored across distinct style cells, named by the
convention servo parses (`<part>.<family>.<k>.html`):

| File | Shape | Intent |
|---|---|---|
| `hero.claude.0.html` | eyebrow → display → lede | conventional editorial opening |
| `hero.claude.1.html` | bordered card on a surface | the SaaS-shaped control |
| `hero.claude.2.html` | display → oversized accent line | editorial, no eyebrow |
| `hero.claude.3.html` | **deliberately broken** | off-allowlist class `hero-shout`, 3000px inline width |

All four are labelled `claude` because one family wrote all four. That is not
incidental — see *Staffing the panel* below.

## 2. The round

```bash
uv run python -m ui_servo.control.servo \
  --candidates <dir> --part hero --round 1 --out evidence/rounds/1
```

```
round 1 / part hero: 3 ranked, 1 rejected by gates, 3 comparison(s), 0 escalated
  REJECTED hero.claude.3: sanitizer-accepted, no-runtime-errors, no-unknown-class,
                          no-overflow, no-motion-violation, axe-clean,
                          reduced-motion-honoured
  #1 hero.claude.2 (2 pts, 2W/0D/0L)
  #2 hero.claude.0 (1 pts, 1W/0D/1L)
  #3 hero.claude.1 (0 pts, 0W/0D/2L)
  frontier report: evidence/rounds/1/frontier.html
```

**The broken candidate never reached a judge.** It failed the deterministic
gates and left the round carrying the gate that stopped it — a work item a
builder can act on for zero model tokens. That is the loop's central economy,
and it is visible in the evidence: no judge signal references its span.

## 3. What the critics said

Two families judged every comparison (the third built the candidates and was
disqualified from judging them). Both had to cite selectors; a finding without
one is rejected and re-asked.

> **codex** · winner A · confidence 0.96
> `[hierarchy] section[data-span-id="hero-v1"]` — *"collapses the hero into
> three similarly weighted lines; the undersized display head…"*

> **gemini** · winner A · confidence 0.95
> `[direction_conformance] section[data-span-id='hero-v1']` — *"Uses surface
> background and border classes to create a card-like container, violating the
> requirement…"*
> `[distinctiveness]` — *"The containerized approach feels like a default
> component library or SaaS landing page…"*

Both families, independently and blind, identified the bordered card as
resembling a **named anti-reference** in `direction/direction.toml`. That is the
taste machinery doing the one thing a generic "is this good UI?" prompt cannot:
scoring against a written direction instead of the corpus median.

They also both flagged that the display face renders as browser-default serif —
a genuine defect in the site, not in the candidates (Instrument Serif is named
in the contract but no web font is vendored). The panel found a real bug while
judging something else.

## 4. Promotion

```bash
uv run python -m ui_servo.control.promote --pick hero.claude.2.html --part hero --round 1
# promoted hero from round 1 -> site/assets/fragments/hero.html
#   sha256 32bf4535ef0f7cb83ae606c514cf41bce67bfbdba64d03a94a2e62e3f830831a
```

Promotion re-runs the class-0 sanitiser over the pick and writes it under a
provenance comment:

```html
<!-- ui-servo: gated round=1 sha256=32bf4535… -->
```

The Rust server verifies that comment **and** the hash on every request. Proof
it is not decoration:

| Action | Result |
|---|---|
| Serve the promoted hero | `200`, wrapped in a fresh `data-span-id` |
| Append one line to the file by hand | `500` on `/fragments/promoted/hero`; the home page falls back to the placeholder and the smuggled markup never renders |
| Drop in a fragment with no provenance | refused — "it was never gated" |

## 5. Staffing the panel

The first live round escalated **all three** comparisons. Not a bug: with three
judging families and a self-preference guard, a comparison between candidates
from two different families excludes two judges and leaves one, and one vote can
never satisfy "at least two decorrelated critics".

Eligible judges = `panel_families − families({A, B})`. For a three-family panel,
both candidates in a comparison must come from at most one family. Rerun with a
single builder family: **0 escalations, decisive ordering.** The constraint is
recorded in `REVIEW_LOG.md`; it governs how a round is *staffed*, which is not
obvious until a round comes back unanimously escalated.

## 6. The site

![The home page after promotion](site-home.png)

The hero at the top of that page is `hero.claude.2` — chosen by the panel,
gated at promotion, and served with its provenance verified. Below it, an htmx
fragment swap and a Rust→WASM island, both instrumented by the same probe that
measured the candidates.
