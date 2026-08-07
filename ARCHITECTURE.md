# Architecture

ui-servo is a **control system** wearing **hexagonal ports and adapters**. The hexagon
keeps the definition of "good" independent of whatever browser, model CLI or transport
happens to be observing it this week; the control framing says what each piece is *for*.

## The control-systems mapping

```
                 direction/direction.toml
                   (reference signal r)
                            │
                            ▼
   ┌─────────── comparators ────────────┐
   │  Tier 0 gates      deterministic   │        error e = r − y
   │  Tier 1 probe      deterministic   │◄──────────────┐
   │  Tier 2 harness    deterministic   │               │
   │  Tier 3 panel      judgement       │               │ y  (observation)
   └──────────────┬─────────────────────┘               │
                  │ findings (flow)                     │
                  ▼                                 ┌───┴────────────────┐
        control/regulator.py  (fast loop)            │  sensors           │
        control/critique.py + explore.py (slow) ────►│  probe (in-page)   │
                  │ actions                          │  shadow browser    │
                  ▼                                  └───▲────────────────┘
            builders (actuators)  ──────────────────────►│
                  │                                   site/ (plant)
                  ▼
        evidence store (stock, append-only)
```

| Control concept | ui-servo |
|---|---|
| Reference signal | `direction/direction.toml`, parsed by `ui_servo/domain/contract.py` |
| Plant (the thing under control) | `site/` — the artefact being built |
| Sensors | Tier 1 in-browser probe; Tier 2 shadow-browser harness |
| Comparators | Tier 0–2 gates (deterministic) and the Tier 3 critic panel (judgement) |
| Error signal | findings: a gap against the contract, with a citation |
| Actuators | builder subagents applying gap-driven revisions |
| Stock | the evidence store — everything observed, append-only, span-joined |
| Flow | findings and revisions moving between turns |
| Brake | the human, on every merge |

**Stock and flow matter here.** Evidence accumulates and is never rewritten, so a regression
is visible as a *change in the stock* rather than as a claim in a chat log. Findings are the
flow; they are consumed by a revision and then closed. Confusing the two — treating a
finding as durable truth, or evidence as disposable — is how these loops rot.

## Two loops at two speeds

**Fast homeostatic loop — `ui_servo/control/regulator.py`.**
Deterministic correctness. Sensor readings in, contract comparison, pass/fail out, no model
involved, no negotiation. Its job is to hold the system at setpoint: sanitised markup, a
class vocabulary derived from tokens, motion inside `MotionTable`, no overflow, no axe
violations, a working reduced-motion branch. A homeostat has no opinions, which is precisely
what makes it trustworthy at high frequency.

**Slow exploratory loop — `ui_servo/control/critique.py` + `ui_servo/control/explore.py`.**
Taste. `explore.py` samples variants across the style and density axes into a MAP-Elites
archive; `critique.py` runs the blind, rotated, cross-family panel over the survivors and
returns per-axis, citation-bearing findings. This loop *moves* the setpoint's neighbourhood
rather than merely holding it, so it runs at a fraction of the frequency and always behind
the deterministic prune.

The separation is the point: **correctness is homeostasis, taste is exploration**, and they
must not share a scalar. Fast-loop failures are gates. Slow-loop verdicts are rankings.

## Conant–Ashby: the regulator contains a model

*Every good regulator of a system must be a model of that system.* For correctness, the
model is the contract's machine-checkable projections — `MotionTable`, the class allowlist
seed, the token sheet. For taste, the model is **the direction contract plus the exemplars
the human has picked**, and nothing else. It is data, versioned in git, diffable. Taste that
lives only inside a prompt is a model nobody can inspect or roll back.

This is why `to_css_custom_properties()` exists: emitting the contract as the site's tokens
makes the regulator's internal model *literally the material the plant is built from*, which
collapses most of the error before any measurement happens.

## Requisite variety

*Only variety can absorb variety.* A generator that can produce an unbounded space of UI
cannot be regulated by a single critic with a single rubric — the controller would have less
variety than the disturbance and would systematically miss.

Two amplifiers:

- **Cross-family panel.** Independent model families (via locally-authenticated `claude`,
  `codex`, `agy` CLIs) with rotated assignment and randomised A/B order. Correlated blind
  spots are the failure mode; diversity of family is the cheapest decorrelation available.
- **Quality-diversity sampling.** MAP-Elites over the style/density axes keeps an archive of
  *different kinds* of good instead of K near-copies of one optimum, so the search itself
  carries variety rather than collapsing early.

## Goodhart governance

Any taste metric under optimisation pressure stops being a measure. The countermeasures are
structural, not exhortative:

- multi-axis rubric (hierarchy, type, colour, motion, density) — never one score;
- pairwise randomised comparison, so absolute-score drift cannot accumulate;
- rotated, context-blind critics — a fixed judge gets modelled;
- the generator's family never judges its own output;
- low-frequency **human sampling** of panel verdicts, which is the only ground truth in the
  system and the reason the human stays the brake;
- gates never trade against scores, so nothing can be bought with taste.

## Dependency rule

Arrows are "may import". Every arrow points inward; nothing points at `control` or at
`adapters`, and there is no edge between them in either direction.

```
   control ──────────► ports ◄────────── adapters
      │                  │                  │
      │                  ▼                  │
      └────────────► domain ◄───────────────┘
```

| Layer | May import (`ui_servo`) | May import (third party) |
|---|---|---|
| `domain` | `domain` | pydantic |
| `ports` | `domain`, `ports` | pydantic |
| `control` | `domain`, `ports`, `control` | *none — stdlib only* |
| `adapters` | `domain`, `ports`, `adapters` | anything |

- `ui_servo/domain/` imports **only the standard library and pydantic**. No I/O, no browser,
  no model, no filesystem. Pure functions of parsed data.
- `ui_servo/ports/` defines the interfaces the loop needs (`Browser`, `CriticPanel`,
  `EvidenceStore`, `Clock`) in terms of domain types. Domain and pydantic only — an
  interface that imports FastAPI has stopped being an interface.
- `ui_servo/control/` imports `domain` and `ports` — **never** `adapters`, and **no
  third-party libraries at all**. The loops must be runnable end-to-end against fakes; if a
  loop needs a wire, a browser or a vendor SDK, that need belongs behind a port.
- `ui_servo/adapters/` implements `ports` and is the only layer allowed to touch the world:
  Playwright, the model CLIs, the filesystem, HTTP. Any distribution may be imported here —
  but still only `domain`, `ports` and sibling adapters from inside `ui_servo`. An adapter
  reaching for `control` would invert the hexagon, so that edge is forbidden too.

Enforced by `tests/test_architecture.py`, which walks every module with `ast` (no imports
executed) and checks both halves of the table: which `ui_servo` packages a layer reaches and
which distributions it reaches. The guard's own detection logic is tested against synthetic
sources — control importing Playwright, ports importing FastAPI, domain importing a
non-domain `ui_servo` package — because a guard that has never been shown to fire is not a
guard.

The one sanctioned exception: `ui_servo/domain/contract.py` has a CLI under
`if __name__ == "__main__"`, which reads a file. The model API itself takes parsed mappings
or TOML *text* — obtaining that text is an adapter's problem. The contract it reads by
default comes from `$UI_SERVO_CONTRACT`, else the build-time copy at
`ui_servo/domain/data/direction.toml` (hatch `force-include`, so an installed wheel has a
default), else `direction/direction.toml` in a source checkout.

## Package tree

```
ui-servo/
├── direction/
│   └── direction.toml            reference signal, v1, versioned taste
├── ui_servo/
│   ├── __init__.py
│   ├── domain/                   pure: no I/O, stdlib + pydantic only
│   │   ├── __init__.py
│   │   ├── contract.py           DirectionContract, MotionTable, token/class derivations
│   │   ├── evidence.py           span-joined evidence signals
│   │   ├── data/direction.toml   (wheel only) build-time copy of the contract
│   │   ├── rubric.py             (planned) per-axis rubric, finding schema
│   │   ├── blandness.py          (planned) style vector, distance-from-median metric
│   │   └── archive.py            (planned) MAP-Elites grid over style/density axes
│   ├── ports/
│   │   ├── sanitizer.py          Tier 0 sanitiser interface
│   │   ├── store.py              evidence store interface
│   │   └── browser.py            (planned) Browser, CriticPanel, Clock
│   ├── control/
│   │   ├── regulator.py          (planned) fast loop: deterministic correctness
│   │   ├── critique.py           (planned) slow loop: blind rotated panel
│   │   └── explore.py            (planned) slow loop: QD variant sampling
│   └── adapters/
│       ├── nh3_sanitizer.py      Tier 0 sanitiser over nh3 + tinycss2
│       ├── jsonl_store.py        append-only span-joined evidence store
│       └── …                     (planned) playwright browser, CLI critics, beacon ingest
├── probe/                        Tier 1 in-browser runtime
├── site/                         axum + htmx + WASM islands — the plant
├── tools/
│   ├── specs/                    unit specifications
│   └── review.sh                 adversarial review harness
├── tests/
│   ├── test_contract.py          contract parse, round-trip, derivations
│   ├── test_architecture.py      the dependency rule, as a test
│   └── …                         per-unit suites
├── ARCHITECTURE.md
├── README.md
├── REVIEW_LOG.md                 critic verdicts per unit
└── pyproject.toml
```

Entries marked `(planned)` are units not yet built; the dependency rule above applies to them
the moment they exist, and the architecture test will start checking them automatically.
