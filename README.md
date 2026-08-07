# ui-servo

A **Gauntlet Loop** for agent-generated UI, wired to a **tiered feedback loop**, aimed at
one concrete target: building and continuously improving the owner's personal website in
`site/` (axum + htmx, with WASM islands where an island earns its weight).

Two ideas, fused:

- The **Gauntlet Loop** (Shumer, July 2026) is a process for getting good work out of
  agents: decompose, build in isolation, then judge blind against a real bar.
- The **tiered feedback loop** is a control system: split feedback by latency class so the
  cheap deterministic checks prune everything they can *before* a single model token is
  spent, and so no model ever sits in the hot path.

The direction contract in `direction/direction.toml` is the reference signal both loops
steer toward. Your taste is the setpoint; the agents are the servo.

## The Gauntlet Loop

```
decompose  →  isolated builders  →  blind rotated critics  →  gap-driven revision  →  integration
   ▲                                       │
   └───────────────  human is the brake  ───┘
```

1. **Agent-led decomposition.** The work is cut into units with explicit owned paths and a
   spec each (`tools/specs/U*.md`). Units are sized so a builder can hold one whole.
2. **Isolated builder subagents.** One unit, one fresh context, its own paths. Builders do
   not see each other's reasoning, so they cannot converge on a shared bad assumption.
3. **Blind, rotated, fresh-context critics.** A critic sees the spec, the diff, and the
   working tree — never the builder's reasoning, never who built it. Critics judge
   **side-by-side against a real reference bar**, not against an adjective.
4. **Gap-driven revision.** Findings come back as gaps against the bar with a citation
   (file, selector, line). Vibes are not actionable and are rejected as findings.
5. **Integration pass.** A final unit reconciles the seams the isolated units could not see.

Invariants, and they are not negotiable:

| Invariant | Why it exists |
|---|---|
| The implementer never grades its own work | Self-assessment measures confidence, not quality |
| Critics are context-blind and **rotated** | A fixed critic gets modelled and gamed; a blind one cannot flatter an author it does not know |
| The bar is **concrete** | Named references and anti-references beat "make it beautiful" |
| The human is the brake | The loop proposes; the owner disposes, and every merge is a human act |

## The tiered feedback loop

Feedback is split by latency class. The three deterministic tiers (0–2) run concurrently
with each other over the same change; Tier 3 does not run alongside them — it runs
afterwards, asynchronously, and only on what survives the deterministic prune.

| Tier | Latency | What it checks | Verdict is |
|---|---|---|---|
| **0** | pre-ship, 0 ms | HTML sanitiser, class allowlist (seeded from the contract), attribute schema, motion-token conformance in the emitted CSS | deterministic |
| **1** | same frame – 2 frames | in-browser probe: swap outcomes, custom-element/WASM errors, `PerformanceObserver`, `getAnimations()` vs the motion table, CSSOM class check, overflow probe, CSP/error handlers | deterministic |
| **2** | 50–500 ms | shadow-browser harness: screenshot, ARIA snapshot, axe-core, pixel diff against baseline, CDP trace | deterministic |
| **3** | 1–10 s, async | cross-family critic panel: per-axis rubric, pairwise randomised comparison, verdicts must cite selectors | judgement |

Every swap is stamped with a `data-span-id`; evidence from all four tiers joins on that id.
Evidence is *task* evidence, not system telemetry — it lives in its own append-only store.

**Deterministic tiers prune first.** Tiers 0–2 are cheap enough to run in parallel on every
variant. Anything they can prove broken is discarded there and then, so a model is only ever
asked the questions machines genuinely cannot answer. Tier 3 is then dispatched
asynchronously over the survivors, which is what keeps **no model in the hot path**: the
builder never blocks on a critic.

**Critics ride locally-authenticated CLIs.** The panel shells out to `claude`, `codex` and
`agy` — the same CLIs the owner is already signed into. **No API keys**, no per-token
billing surface, no secret to leak from a config file. Cross-family is the point: three
model families disagreeing is signal; one family agreeing with itself is not.

## The taste problem

Correctness has an oracle. Taste does not, and that asymmetry is the whole design problem.
A critic trained on the corpus steers toward the corpus median — and the corpus median *is*
the generic SaaS template. Five mechanisms break that:

1. **Direction before generation.** `direction/direction.toml` is a versioned contract:
   OKLCH palette, type scale, spacing scale, motion tokens, density bounds, named
   references *and anti-references*. Critics score conformance-to-direction and
   distinctiveness. Nobody is ever asked "is this good UI" in the abstract.
2. **Measured blandness.** Style-vector distance from a reference corpus of generic
   templates. Too close to the median is a penalty. Boring becomes a number.
3. **Quality-diversity sampling.** K variants across the style axes, kept in a MAP-Elites
   grid so the archive holds *different kinds* of good rather than K copies of one local
   optimum. Deterministic gates prune the broken; the critic ranks within direction; the
   human picks from the frontier, and picks become few-shot exemplars.
4. **Correctness ⊥ taste.** Accessibility, overflow, motion conformance and the
   reduced-motion branch are gates, not scores. A taste win never buys a correctness loss;
   the two axes cannot trade.
5. **Goodhart guards.** Multi-axis rubric instead of one number, randomised A/B order,
   rotated critics, low-frequency human sampling of the panel's verdicts. Any single taste
   metric under optimisation pressure becomes a target and stops being a measure.

## Quickstart

```bash
uv sync
uv run python -m ui_servo.domain.contract --check          # validate the reference signal
uv run python -m ui_servo.domain.contract --emit-css site/assets/tokens.css
uv run pytest -q
```

`--check` parses the contract, round-trips it, and prints what the loop is currently
steering toward. `--emit-css` derives `site/assets/tokens.css`, the token sheet the site is
built from, so staying on-contract is the path of least resistance for a builder.

The contract is located in this order: `--contract PATH`, then `$UI_SERVO_CONTRACT` (when
set and non-empty), then the copy shipped inside the installed package at
`ui_servo/domain/data/direction.toml` (build-time copy of the same file, so the two cannot
drift), then `direction/direction.toml` in a source checkout. With none of those, the CLI
exits 2 and asks for `--contract PATH`.

An explicit choice is authoritative: if `--contract` or `$UI_SERVO_CONTRACT` names a file
that does not exist or cannot be read, the CLI exits 2 naming that path. It never falls
through to a different contract — reporting `ok` for a document the operator did not name
is worse than failing.

## Layout

```
direction/direction.toml   the versioned reference signal
ui_servo/domain/           pure domain: contract, rubric, blandness, QD archive
ui_servo/ports/            interfaces the loop needs from the world
ui_servo/control/          the loops: regulator (fast), critique + explore (slow)
ui_servo/adapters/         browsers, model CLIs, evidence store, beacon ingest
probe/                     Tier 1 in-browser runtime
site/                      the artefact under control: axum + htmx + WASM islands
tools/                     unit specs and the adversarial review harness
tests/                     deterministic suite, including the dependency-rule guard
REVIEW_LOG.md              critic verdicts per unit, kept for audit
```

See `ARCHITECTURE.md` for the ports-and-adapters structure and the control-systems mapping.
