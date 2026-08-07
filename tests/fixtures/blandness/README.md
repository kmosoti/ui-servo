# Blandness fixtures

The anti-corpus the style vector is measured against, plus two candidates that sit at
opposite ends of it.

Each fixture is a pair:

* `<name>.html` — the fragment as a builder would emit it, carrying a `data-span-id`.
* `<name>.json` — the `StyleSample` the sensor adapter would return for that fragment
  (see the documented shape at the top of `ui_servo/domain/variant.py`). The numbers
  are hand-derived from the fragment's own stylesheet rather than captured from a
  browser, so the corpus is stable across Chromium versions and the unit test needs
  no renderer.

| fixture | role |
| --- | --- |
| `generic_shadcn` | anti-reference: unmodified default component-library theme, zinc on white, 8px radii |
| `generic_bootstrap` | anti-reference: primary-blue buttons, 1rem gutters, jumbotron hierarchy |
| `generic_saas_landing` | anti-reference: centred hero, indigo accent, three feature cards, pill CTA |
| `candidate_generic` | a candidate that landed on the corpus median — should score LOW blandness |
| `candidate_styled` | a candidate set on `direction/direction.toml` — should score HIGH blandness |

`blandness()` returns the distance to the *nearest* corpus member, so low means bland.
The three anti-references correspond to entries in `[[anti_references]]` of the
direction contract; keeping them in sync is deliberate — the corpus is the contract's
opinion about what "generic" means, not this test's.

OKLCH channels are the converted values of the hex colours the fragments actually
declare (`l` in [0, 1], `c` >= 0, `h` in degrees). Spacing and font sizes are in CSS
pixels; the vector quantises them against the contract's own modular scales, which is
why the styled candidate lands on step buckets and the generic ones land off-scale.
