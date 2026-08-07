---
name: gauntlet
description: Run the Gauntlet Loop for a UI mission in ui-servo — invoked as "/gauntlet <mission>" (e.g. "/gauntlet rebuild the home hero"). Use whenever the request is to build, rebuild or improve UI in site/ to the standard of direction/direction.toml. Decomposes the mission into parts, spawns isolated builder subagents per part, gates every variant deterministically with the servo CLI before any model token is spent, then runs the blind rotated critic panel, drives gap-driven revision, and hands the human a frontier report to pick from.
---

# The Gauntlet Loop

You are the **lead**. You decompose, dispatch, gate, and report. You do **not** build
fragments yourself, and you do **not** judge them yourself. Both of those are delegated,
because an agent that builds and grades the same artefact is measuring its own confidence.

Your loop, once:

```
intake → decompose → build (parallel, isolated) → deterministic gauntlet → panel → revise
                          ▲                                                        │
                          └────────────────────────────────────────────────────────┘
                                        human is the brake
```

## 1. Mission intake

Restate the mission in one paragraph before doing anything: what artefact, which routes or
fragments in `site/`, what "done" looks like. If the mission is ambiguous about scope, ask
once and stop — a mission you guessed at costs a full round to discover.

**The bar is these three things and nothing else:**

| Source | What it supplies |
|---|---|
| `direction/direction.toml` | the reference signal — palette, type scale, spacing scale, motion table, density bounds, named references *and anti-references* |
| `direction/references/` | captured screenshots of the named references, when present (populate on demand; absence means cite the reference by name and URL from the contract instead) |
| `exemplars/` | past human picks, written only by `explore.record_pick` — the sole injection of real taste |

Never substitute your own adjectives for the bar. "Make it cleaner" is not a specification;
"conform to `[type].ratio = 1.25` and match the measure discipline of the
`editorial-serif-longform` reference" is.

Read the contract at the start of every mission:

```bash
uv run python -m ui_servo.domain.contract --check
```

## 2. Decomposition — you do this, not a subagent

Cut the mission into **parts**: the smallest units that are independently *buildable* and
independently *judgeable*. A part that cannot be judged on its own is too big; a part whose
quality only exists in relation to its neighbour is too small and belongs to integration.

For each part record a **part spec** — the design brief, and the only part of the dispatch
payload that carries judgement:

- `part` id (kebab-case, becomes the `--part` value: `hero`, `project-card`, `writing-index`)
- purpose, in one sentence, in terms of what the reader gets
- the fragment root and its route in `site/`
- content it must carry (real copy, not lorem)
- direction axes that matter most for this part (`hierarchy`, `typography`, `color`,
  `motion`, `density`, `direction_conformance`, `distinctiveness`)
- explicit non-goals

Write part specs where builders can read them, and dispatch them by value in the builder
prompt. Do not write your reasoning about the parts into the spec — builders receive the
brief, not your deliberation.

## 3. Build — isolated builder subagents

Spawn one `builder` subagent per part (`.claude/agents/builder.md`). Parts that do not touch
the same files are independent: **launch those builders in a single message so they run in
parallel.**

### The builder dispatch payload

A builder cannot write a file without knowing where it goes, and cannot aim at a QD cell it
was never told about. The payload is exactly these seven fields — no more, and no fewer:

| Field | Example | Why the builder needs it |
|---|---|---|
| `part_spec` | the §2 brief, by value | what to build |
| `contract_path` | `direction/direction.toml` | the reference signal |
| `exemplars_path` | `exemplars/` | past human picks — real taste |
| `round_id` | `2` | it appears in the output path |
| `target_cell` | `density=high, expressiveness=med, motion=low` | which *kind* of good this variant aims at, so K variants spread across the archive instead of clustering |
| `variant_index` | `k = 0…K-1` | the third token of the filename |
| `output_path` | `evidence/rounds/2/candidates/hero.claude.0.html` | where to write |

**Six of these seven are operational metadata, not critique context, and they do not
compromise isolation.** A round number and a filename tell a builder *where it is standing*;
they say nothing about how anyone judged anything. Isolation is a claim about **judgement
and peer work**, and that claim is unchanged: builders still never see another candidate,
another builder's reasoning, a critic's identity, a prior verdict, or the panel's ranking.
Withholding the output path does not make a builder more isolated — it makes it unable to do
its job, and you will spend a round discovering that.

The fragment-authoring contract is already embedded in `.claude/agents/builder.md` — do not
re-state it in the prompt.

Each builder receives **none of**: another builder's output or reasoning, any critic's
identity or prior verdict, or your assessment of its previous attempt beyond the findings
themselves. Isolation is what stops K builders converging on one shared bad assumption.

For a taste-bearing part, ask for **K variants** across the style axes (K = 3–4 is the
working default) rather than one, so the archive has something to be diverse about. One
builder may produce several variants; different builders should not be told about each
other's.

Builders write candidates to:

```
evidence/rounds/<round>/candidates/<part>.<family>.<k>.html
```

Three tokens, in this order, and the grammar is load-bearing:

| Token | Meaning |
|---|---|
| `<part>` | the part id, matching the `--part` value |
| `<family>` | the **builder family** — `claude`, `codex`, `gemini`. The servo CLI derives `builder_family` from exactly this token, and `ui_servo/domain/policy.py`'s `eligible_judges` uses it to exclude a judge from grading its own family's output. |
| `<k>` | the **variant index** within that builder — `0`, `1`, `2`, … |

So a builder emitting three variants writes `hero.claude.0.html`, `hero.claude.1.html`,
`hero.claude.2.html`. The legacy two-token form `hero.claude.html` is still accepted and is
read as **k = 0**.

**Never encode variant identity into the family token.** `hero.claude2.html` or
`hero.claude-b.html` names a family that does not exist, which silently defeats the
self-preference guard — the judge from that family is no longer excluded, because the
policy layer cannot see that `claude2` is `claude`. The variant index goes in its own token
or nowhere.

## 4. The deterministic gauntlet — always before any critic

Latency classes 0–2 are machines. They are free, they are not negotiable, and they run
**before a single critic token is spent**. This is the whole economic argument of the
system: models are only ever asked questions machines genuinely cannot answer.

**Command contract** (composition root and CLI land in U13,
`ui_servo/control/servo.py` — this exact invocation is the contract):

```bash
uv run python -m ui_servo.control.servo \
  --candidates evidence/rounds/<round>/candidates \
  --part <part> \
  --round <round> \
  --out evidence/rounds/<round>/
```

Add `--dry-judges` to exercise the pipeline with stub judges (deterministic ranking, no live
model calls) when you are validating plumbing rather than taste.

Read `RoundResult` from stdout and from the evidence store:

- **`rejected`** — variants that failed a gate, each with the named gate and its evidence
  (sanitizer violation, unknown class, overflow, motion violation, axe violation, failed
  reduced-motion branch, swap/js/CSP error). These **never reach a judge**.
- **`ranked`** — survivors, ordered by the panel.
- **`escalations`** — pairs the panel could not resolve.
- **`report_path`** — the frontier report for the human.

Send every rejected variant straight back to its builder with the gate name and the
evidence, and re-run the gauntlet. Do not spend a critic on a broken variant, and do not
argue with a gate: gates are correctness, and **correctness never trades against taste**.

## 5. Critique — the blind panel

### 5a. Blind staging comes first — no exceptions

The candidate filenames encode the builder family. Handing a critic
`hero.claude.2.html` tells it exactly who it is grading, and a critic that knows the author
flatters the author. So **before any critic contact**, survivors are re-staged under opaque
names — and staged **one directory per pairwise comparison**:

```
evidence/rounds/<round>/blind/<comparison-id>/<part>.<A|B>.<hash>.html
evidence/rounds/<round>/blind/<comparison-id>/<part>.<A|B>.<hash>.png
```

`<part>` is already known to the critic from its spec and leaks nothing. `<A|B>` is the
arbitrary position within *this* comparison. `<hash>` is a short digest of the file
contents. **No family token, no builder id, no variant index, no round-over-round continuity
in the label.**

This filename grammar is **U13's, and `ui_servo/control/servo.py` is authoritative for it**
— `<part>.<A|B>.<hash>.*`, nested under the per-comparison directory. It is restated here so
a hand-driven round matches the code; if the two ever diverge, servo wins and this file is
the bug.

**One directory per comparison, and the critic gets that directory only.** A single shared
blind directory is not blind enough: a critic able to list it sees every sibling comparison
in the round, can count the variants, can correlate one artefact across several pairings,
and can reconstruct groupings that amount to authorship. Each comparison is therefore
sealed in `<comparison-id>/` containing exactly two artefacts, and the critic is handed that
one path.

Rules you must uphold:

- Critics receive **only** the single per-comparison directory they are judging. Not its
  parent, not a sibling, not a listing of `blind/`.
- The original `evidence/rounds/<round>/candidates/` paths are **never** given to a critic,
  never mentioned in a critic prompt, and never reachable from what the critic is handed.
- The candidates → blind mapping stays with you and the evidence store. It is not part of
  any critic's context.
- `<comparison-id>` is opaque and carries no ordering information; `A`/`B` are re-assigned
  per comparison, and the order is randomised by the code.

`ui_servo/control/servo.py` (U13) performs this staging **in code** — the skill text here
describes the same guarantee so that a hand-driven round cannot quietly weaken it. When you
invoke the panel outside servo, you do the staging yourself, the same way.

### 5b. The panel

Survivors go to the cross-family panel via `ui_servo/control/critique.py`, which the servo
CLI drives for you. What you must uphold at the orchestration layer:

- Critics see the **part spec + the blind-staged artefact (screenshots, ARIA snapshot, gate
  evidence) + the bar**. Nothing else.
- Critics **never** see builder reasoning, builder identity, candidate filenames, or which
  family produced which variant. You never paste a builder transcript into a critic prompt.
- Judgement is **pairwise against the bar**, per-axis, with A/B order randomised by the
  code. There is no absolute score to drift.
- Every finding cites a **selector**. A finding without a citation is not actionable and the
  policy layer rejects it — do not launder one back in by paraphrasing it yourself.
- The panel is 2-of-3 across model families. Disagreement without a majority **escalates**.

When you need a critique outside the servo pipeline — a single artefact, no tournament —
spawn the `critic` subagent (`.claude/agents/critic.md`) with a fresh context and the same
three inputs. Never reuse a critic context across artefacts or rounds.

## 6. Revision loop

Take the **largest meaningful gap** per part — not the whole finding list — and send it to
**the same builder** that produced the variant. Same builder, because it holds the context
of its own construction; largest gap only, because a builder handed twelve findings
optimises for closing twelve tickets rather than for the one thing that is actually wrong.

Next round gets **fresh critics**. Rotation is enforced in `ui_servo/domain/policy.py`
(`rotate`, and a judge never re-judges a pair it has already seen), so your job is simply
not to defeat it: never carry a critic transcript forward, never summarise round N's
verdicts into round N+1's prompt, never tell a builder who judged it.

Increment `--round` and re-run from step 4.

## 7. The human picks, and the pick is promoted

Present `report_path` — the frontier report, one card per archive cell with screenshot,
axes, gate summary, panel findings and blandness. The owner picks.

A pick does two things, and only the owner can trigger either:

1. **It becomes an exemplar**, through `explore.record_pick` and nothing else. This is the
   only path that writes to `exemplars/`, and the only place real taste enters the system.
   Never write an exemplar because you liked a variant.
2. **It is promoted into the site.** Copy the picked candidate's HTML to:

   ```
   site/assets/fragments/<part>.html
   ```

   One file per part, named by part id only — the family and variant tokens are build-time
   bookkeeping and must not survive promotion.

   **Write the provenance comment as part of promoting.** The promoted file must carry:

   ```html
   <!-- ui-servo: gated round=<n> sha256=<hash> -->
   ```

   where `<n>` is the round that gated it and `<hash>` is the sha256 of the fragment
   content. This is not decoration: `site/src/fragments/promoted.rs` (U14) **verifies the
   comment is present at serve time** and refuses the file without it — a startup error in
   release, a 500 plus a logged violation in dev. The class-0 sanitizer itself runs in
   Python at promotion time, not in Rust, so this comment is the site's only evidence that
   the fragment it is about to serve ever passed a gate. A file dropped into
   `site/assets/fragments/` by hand has no provenance and will be refused, which is the
   intended behaviour.

   U14 owns the serving side: `fragments::promoted::render(part)` reads the file, wraps it
   in `frame()` so it gets a fresh `data-span-id` and `elementtiming`, and serves it at
   **`/fragments/promoted/{part}`**. The home page mounts the promoted `hero` when that file
   exists and falls back to the built-in placeholder when it does not — so a pick visibly
   changes the real site. An unknown or missing part is a 404.

Promotion is what makes the next step possible: until the picks are in
`site/assets/fragments/`, there is no assembled page, only a pile of candidates.

## 8. Integration — review the assembly, not the skeleton

Once every part's pick is promoted, spawn the `integrator` subagent
(`.claude/agents/integrator.md`) with fresh context against **the assembled page** — the
site *serving the promoted fragments*, not the untouched skeleton. Reviewing `site/` before
promotion reviews placeholder content and tells you nothing about the seams.

The integrator looks at exactly what the isolated builders could not: vertical rhythm across
parts, spacing coherence, type-scale consistency, colour distribution, page-level motion
budget, and voice drift in the copy.

**Integration findings re-enter the loop through the gates, never around them.** For each
finding:

1. route it to the builder that owns the part,
2. the builder emits a revised candidate under the normal
   `evidence/rounds/<round>/candidates/<part>.<family>.<k>.html` grammar,
3. **re-run the deterministic gauntlet for that part** (step 4) — an integration fix is
   still a fragment change and can still break a gate,
4. promote the survivor and re-integrate.

A fix that goes straight into `site/assets/fragments/` because "it is only spacing" is how
an ungated regression enters the plant.

## 9. Termination

Stop the loop when **any** of these is true — and say which one:

1. **Bar passed** — all three of these hold together:
   - every part survived the gauntlet and the panel found no major gap,
   - the integrator reports **no major findings** on the assembled page,
   - gates are **green on the final assembly**, not merely on the parts in isolation.

   Two out of three is not a pass.
2. **Round budget exhausted** — default **3** rounds. Report the frontier as it stands.
3. **No strategy change** — a part fails twice on the same axis with the same approach.
   Repeating a failing strategy is not iteration; stop and escalate to the human with both
   attempts.
4. **Human brake** — the owner says stop, at any point, for any reason.

**Escalate a 2-of-3 panel disagreement to the human immediately**, with **both screenshots**
side by side and the per-axis split. Do not break a tie yourself: a tie is the panel telling
you the question is a taste question, and taste is the owner's.

## Invariants — non-negotiable

| Invariant | Why |
|---|---|
| The implementer never grades its own work | self-assessment measures confidence, not quality |
| Deterministic gates run before every critic call | machines answer what machines can answer; models are expensive |
| Critics are context-blind and rotated | a fixed critic gets modelled and gamed |
| Critics never see builder reasoning or identity | a critic that knows the author flatters the author |
| Artefacts are blind-staged under opaque names before any critic contact | the candidate filename encodes the builder family; handing it over *is* the leak |
| Every change reaches the assembly through the gates | an ungated "small fix" is how a regression enters the plant |
| The bar is concrete | named references beat adjectives |
| Gates never trade against taste | nothing is buyable with a taste win |
| Every finding cites a selector | vibes are not actionable |
| The human is the brake | the loop proposes; the owner disposes |
