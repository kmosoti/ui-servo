# The golden path (owner rulings, 2026-08-09)

**The canonical reference is the served site itself** — the version the
owner signed off with "capture this as the golden path" (margins since
re-ruled to the warp starfield, and the BlackCell black hole added, both
2026-08-09) — and the three PNGs here are its renders (1920px,
devicePixelRatio 2):

| Capture | Route |
| --- | --- |
| `golden-home.png` | `/` |
| `golden-bc.png` | `/projects/blackcell` |
| `golden-sp.png` | `/projects/splunk-dashboard-studio` |

`Kennedy Mosoti - Portfolio.dc.html` + `support.js` is the **ancestor**:
the owner's own build that the site reproduces. It stays here as
provenance, but where it and the captures disagree, **the captures win**
— they include the owner tunings ruled after the port.

## Owner rulings on top of the ancestor file

1. **Hero = direction 1a** (the terminal window). The ancestor carries a
   second direction (1b) behind a `1a`/`1b` header toggle; both are
   retired. The site serves 1a alone.
2. **Margin ambience = the warp starfield** (owner ruling, 2026-08-09,
   superseding the ancestor's node graph + glyph rain — including all
   the constellation retunings ruled earlier the same day). Chosen from
   a three-way bake-off (orbital traffic chart, signal waterfall, warp
   starfield); the losing candidates and the retired constellation were
   removed from `site/assets/portfolio.js` rather than kept behind a
   flag. `makeWarp` dials:

   | Dial | Value |
   | --- | --- |
   | Depth layers | 3 — depth 0.25 / 0.55 / 1.0 |
   | Star radius / alpha / drift | 0.7px @ .35 / 7px·s⁻¹ → 1.7px @ .90 / 30px·s⁻¹ (by layer) |
   | Star count per layer | `max(14, W·H / 16000 × depth)` |
   | Twinkle | ±25% alpha, per-star phase |
   | Parallax | cursor offset × 0.02 × depth × 10, smoothed at 3·dt |
   | Warp streaks | ~0.5/s, 40–90px tails, 500–900px·s⁻¹ |
   | Debris | one tumbling wireframe tetrahedron every ~14–32s, ember @ .4 |
   | Canvas opacity / width | 0.85 / fills the margin to 24px shy of the 1080px column (unchanged from the constellation ruling) |

   The correctness fix carries over: the canvas backing store re-seeds
   whenever CSS size disagrees with it (`display:none` at boot and
   mid-session resizes otherwise render stretched at stale resolution).
3. **BlackCell hero black hole** (owner, 2026-08-09): a 430px
   gravitationally-lensed accretion disk (`makeBlackhole`) floats in
   the empty space right of the deep-dive title on ≥1150px viewports —
   Doppler-beamed, photon ring, lensed far-side arches, fluid sheared
   gas. Not part of the ancestor at all; owner addition.

## What this means for the machinery

- **Fidelity is deterministic.** Changes to the three portfolio pages
  are judged by `cargo test`, the export gate, and comparison against
  these captures — no critic panel. (The margin physics is seeded
  randomly; compare structure and values, not pixels, in the strips.)
- **The v2 contract is superseded in taste.** `direction/direction.toml`
  v2 cannot express this design (Public Sans + JetBrains Mono, larger
  display scale, radii, shadows, 300–1000ms motion). Derive a **v3
  contract from this reference** before running the gauntlet on these
  pages again; until then the gauntlet is the wrong tool for them.
- **Part-specs** in `direction/parts/` keep their copy and facts, but
  where any spec or the stitched experiment disagrees with this
  reference, this reference wins.

**Mechanical change (batch, 2026-08-09):** fonts vendored as 10 woff2
were 3 distinct payloads — the shared files were already variable
fonts served under per-weight rules; fonts.css now declares 3 variable
@font-face rules. Rendering parity proven by weight-width and crop
comparison (see PR batch/font-dedup).
