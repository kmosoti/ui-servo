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

Arrows are "may import". Inside the hexagon every arrow points inward; nothing points at
`control` or at `adapters`, and there is no edge between them in either direction. `cli`
sits outside all of it.

```
                        cli  (composition roots)
              ┌──────────┼──────────┬──────────┐
              ▼          ▼          ▼          ▼
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
| `cli` | anything | anything |

- `ui_servo/domain/` imports **only the standard library and pydantic**. No I/O, no browser,
  no model, no filesystem. Pure functions of parsed data.
- `ui_servo/ports/` defines the interfaces the loop needs — `SanitizerPort`,
  `SensorPort`, `JudgePort`, `EvidenceStorePort`, `ExemplarStorePort` — in terms of
  domain types. Domain and pydantic only: an interface that imports FastAPI has
  stopped being an interface.
- `ui_servo/control/` imports `domain` and `ports` — **never** `adapters`, and **no
  third-party libraries at all**. The loops must be runnable end-to-end against fakes; if a
  loop needs a wire, a browser or a vendor SDK, that need belongs behind a port.
- `ui_servo/adapters/` implements `ports` and is the only layer allowed to touch the world:
  Playwright, the model CLIs, the filesystem, HTTP. Any distribution may be imported here —
  but still only `domain`, `ports` and sibling adapters from inside `ui_servo`. An adapter
  reaching for `control` would invert the hexagon, so that edge is forbidden too.
- `ui_servo/cli/` holds the **composition roots** — `python -m ui_servo.cli.servo` and
  `python -m ui_servo.cli.promote`. Something has to name a concrete adapter or the hexagon
  is a diagram rather than a program, and this is that something: unconstrained in what it
  may import, imported by nothing, containing only entry points. No policy lives here. If a
  decision in one of these modules could change an outcome, it is in the wrong file.

  This layer exists because of a mistake worth recording. `main()` used to live inside
  `control` and reach its adapters via `importlib.import_module`, which kept the import
  graph genuinely cheap — and also made the guard below pass while the dependency was real,
  since a string argument is not an `import` statement.

  Three rounds of fixes tried to *resolve* dynamic imports — follow the alias, resolve the
  relative target — and a review then produced sixteen one-line bypasses of the result
  (`__import__`, `getattr(importlib, ...)`, walrus and annotated bindings, `exec`, an alias
  chain longer than the resolver's bound). So the rule no longer asks what a dynamic import
  imports: **inside the hexagon, holding a runtime importer is itself the violation.**
  `import_module`, `__import__`, `exec`, `eval` and `importlib` are refused outright, with
  `# arch: allow <reason>` as a visible escape hatch. `from importlib.resources import files`
  stays legal — it binds neither the module nor an importer.

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
│   └── direction.toml            reference signal, versioned taste
├── ui_servo/
│   ├── domain/                   pure: no I/O, stdlib + pydantic only
│   │   ├── contract.py           DirectionContract, MotionTable, token/class derivations
│   │   ├── evidence.py           span-joined evidence signals
│   │   ├── verdict.py            rubric axes, findings, pairwise verdicts
│   │   ├── policy.py             blindness, rotation, self-preference, escalation rules
│   │   ├── variant.py            StyleVector, blandness, MAP-Elites archive
│   │   └── data/direction.toml   (wheel only) build-time copy of the contract
│   ├── ports/
│   │   ├── sanitizer.py          class-0 sanitiser interface
│   │   ├── sensor.py             shadow-browser sensor interface
│   │   ├── judge.py              critic interface
│   │   └── store.py              evidence + exemplar store interfaces
│   ├── control/                  the loops — stdlib only, runnable against fakes
│   │   ├── regulator.py          fast loop: sense → compare → gate
│   │   ├── critique.py           slow loop: blind rotated cross-family panel
│   │   ├── explore.py            slow loop: QD sampling, frontier report
│   │   ├── servo.py              one full round, composed over ports
│   │   └── promote.py            gate a pick and write its provenance
│   ├── adapters/                 the only layer that touches the world
│   │   ├── nh3_sanitizer.py      class-0 sanitiser over nh3 + tinycss2
│   │   ├── playwright_sensor.py  screenshots, aria, axe, pixel diff, traces
│   │   ├── cli_judges.py         claude / codex / agy over the local CLIs
│   │   ├── jsonl_store.py        append-only span-joined evidence store
│   │   ├── preview_server.py     serves candidates in the contract's shell
│   │   ├── beacon_ingest/        probe beacon ingest → evidence store
│   │   └── vendor/               axe-core, vendored
│   └── cli/                      composition roots — the only layer naming adapters
│       ├── servo.py              python -m ui_servo.cli.servo
│       └── promote.py            python -m ui_servo.cli.promote
├── probe/probe.js                in-browser sensor runtime
├── site/                         axum + htmx + WASM islands — the plant
│   ├── src/fragments/            the gauntlet's unit of work
│   ├── promoted/                promoted picks, outside the static root
│   └── islands/                  wasm-bindgen crate
├── demo/                         round 4, end to end, with its evidence
├── tools/
│   ├── specs/                    unit specifications
│   └── review.sh                 adversarial review harness
├── tests/                        the suites, including the dependency rule itself
├── ARCHITECTURE.md
├── README.md
├── REVIEW_LOG.md                 critic verdicts per unit
└── pyproject.toml
```
