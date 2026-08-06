# ui-servo

A tiered feedback loop for agent-generated UI. Deterministic tiers give the agent
*instant* feedback; models give it *insight*; quality-diversity sampling plus a
versioned design-direction contract keep it from converging on the corpus-median
template. Your taste is the target model; the agents are the servo.

## Architecture

Feedback is split by latency class and every tier runs concurrently. No model
is ever in the hot path.

| Tier | Latency | What | Where |
|------|---------|------|-------|
| 0 | 0 ms (pre-ship) | nh3 sanitizer, class allowlist, attribute schema | `ui_servo/tier0/` |
| 1 | same frame – 2 frames | in-browser probe: swap outcomes, custom-element/WASM errors, PerformanceObserver, `getAnimations()` motion conformance, CSSOM class check, overflow probe, error/CSP handlers | `probe/probe.js` → beacon → `ui_servo/server/` |
| 2 | 50–500 ms | shadow-browser harness: screenshot, aria snapshot, axe-core, pixel diff, CDP trace | `ui_servo/tier2/` |
| 3 | 1–10 s (async) | cross-family model panel: per-axis rubric, pairwise randomized comparison, verdicts must cite selectors | `ui_servo/tier3/` |

Every swap is stamped with a `data-span-id`; all evidence from all tiers joins
on that span id. Evidence is **task evidence**, not system telemetry — it lives
in its own store (`evidence/`, JSONL per turn).

## The taste problem (Goodhart, not prompting)

A corpus-median critic steers toward the corpus median — which *is* the boring
SaaS template. Mechanisms that break this, in `ui_servo/taste/`:

1. **Direction before generation** — `direction/direction.toml`: versioned LCH
   palette, type scale, spacing, motion tokens, references and anti-references.
   The critic scores conformance-to-direction + distinctiveness, never "is this
   good UI" in the abstract.
2. **Measured blandness** — style vector distance from a reference corpus of
   generic templates. Too close → penalty. Boring becomes a number.
3. **Quality-diversity sampling** — K variants across style axes, MAP-Elites
   grid; deterministic gates prune broken ones; critic ranks within direction;
   the human picks from the frontier and picks become few-shot exemplars.
4. **Correctness ⊥ taste** — deterministic gates (a11y, overflow, motion-token
   conformance, reduced-motion branch) never trade off against taste scores.
5. **Guarded metric** — multi-axis rubric + low-frequency human sampling; any
   single taste number gets gamed.

## Review protocol (matters more than model choice)

- Pairwise comparison with randomized A/B order, never absolute scores alone.
- Per-axis rubric (hierarchy, type, color, motion, density) over holistic.
- The generator's model family never judges its own output.
- Cross-family panel (Gemini + Claude + GPT); disagreement escalates to the human.
- Findings must cite selectors or they're vibes the generator can't act on.

## Layout

```
direction/direction.toml      versioned design-direction contract
probe/probe.js                Tier 1 runtime (~2 kB, vanilla JS)
ui_servo/tier0/               sanitizer + allowlist + attribute schema
ui_servo/server/              FastAPI: render gate, beacon ingest, evidence API
ui_servo/tier2/               Playwright harness
ui_servo/tier3/               rubric, panel, model adapters
ui_servo/taste/               blandness metric, MAP-Elites, gates
ui_servo/evidence.py          span-joined JSONL evidence store
demo/                         htmx demo app wired to the probe
tests/                        pytest suite (deterministic tiers)
```

## Run

```bash
uv sync && uv run playwright install chromium
uv run uvicorn ui_servo.server.app:app --port 8700   # serves demo + ingest
uv run python -m ui_servo.tier2.run --url http://localhost:8700/demo
uv run pytest
```

Tier 3 needs at least one of `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY`; the panel degrades gracefully to whichever families have keys
(and still refuses to let the generator family judge itself).
