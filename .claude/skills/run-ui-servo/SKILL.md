---
name: run-ui-servo
description: Run, start, build, screenshot, and drive the ui-servo portfolio site — the axum server in site/, the static exporter and its gate, and the Python/pytest + cargo test suites. Use when asked to run the app, see a change live, take screenshots, or verify the site end-to-end.
---

# Run ui-servo

All paths are relative to the repo root. The app is a Rust axum server
(`site/`) rendering maud pages with vanilla-JS canvas ambience, plus a static
exporter. Python (uv-managed) is the control system and the browser-test
harness — Playwright is already installed via uv.

## Run (agent path) — the driver

One command boots the server, screenshots every page at golden-capture
settings (1920×950, deviceScaleFactor 2), asserts visible content, and shuts
down cleanly:

```bash
uv run python .claude/skills/run-ui-servo/driver.py --port 8199
```

Screenshots land in `/tmp/ui-servo-run/` (override with `--out`). Exit 0 =
every page served. First run may compile (~1–3 min); later runs boot in
seconds. **Check port 8080 before using it** — interactive sessions keep a
dev server there; the driver defaults to 8199 for that reason.

## Run (human path) — dev server

```bash
cd site && UI_SERVO_PORT=8080 cargo run
```

Serves http://127.0.0.1:8080 (loopback only; port is env-only, no CLI flag).
`UI_SERVO_DEV=1` additionally injects the probe sensor into every page.
Ctrl-C shuts down gracefully.

## Export (static site + gate)

```bash
cd site && cargo run --bin export
```

Writes `site/dist/` and runs the export gate (page renders, asset integrity,
résumé sha256, full link check). Pass a path argument to export elsewhere —
but the destination must be empty, nonexistent, or a previous export (it
refuses to delete a directory without its `.ui-servo-export` marker).

## Tests

```bash
cd site && cargo test                              # Rust suite (~91 tests)
uv run pytest -q -m "not live and not playwright"  # Python suite (~1100 tests)
uv run pytest -q tests/test_island.py              # wasm island browser tests
```

The `playwright` marker drives real Chromium; the `live` marker needs
locally-authenticated model CLIs — deselect both for a plain check. The
island tests rebuild `site/islands` via `site/islands/build.sh` (wasm-pack)
automatically when the committed artifacts are stale — that script is the
canonical island rebuild path.

## Gotchas (all hit for real)

- **Canvas scenes are randomly seeded and animated.** Screenshot comparisons
  must mask the margin strips / compare structure, never pixels. Wait 4–7s
  after load before shooting or the ambience hasn't settled.
- **Margin canvases only render at viewport ≥1280px**, and the BlackCell
  hero's black hole only at ≥1150px. A narrow viewport screenshot "missing"
  them is correct behavior, not a bug.
- **Canvases boot `display:none`** and re-seed their backing store when CSS
  size disagrees — if you ever see a stretched/blurry canvas in a screenshot,
  you captured before a frame ran; wait and reshoot.
- **Golden parity checks need deviceScaleFactor 2** — the reference captures
  in `direction/references/golden/` were taken at dpr2; dpr1 shots will never
  byte-match text rendering.
- **The motion toggle persists** (`localStorage["pf-reduced"]`) — a Playwright
  context that clicked `#pf-motion` will still be in reduced motion after a
  reload; use a fresh context for motion-dependent checks.
- **Export refuses non-empty destinations** without the byte-exact
  `.ui-servo-export` marker — that's the safety gate working, not a failure.

## Troubleshooting

- `error: UI_SERVO_PORT ...` at boot → the env var didn't parse as a u16.
- Driver prints `FAIL — server never answered` → check `cargo build` output
  in `site/`; a compile error upstream is the usual cause.
- `/about` island shows its no-JS fallback in a screenshot → the wasm module
  request failed; run `uv run pytest -q tests/test_island.py` to rebuild the
  island artifacts and pinpoint the phase (`data-island` attribute carries
  the state machine: loading/ready/error/detached/panicked).
