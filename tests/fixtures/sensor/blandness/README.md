# Stylesheets for the sensor's end-to-end blandness test

`tests/fixtures/blandness/*.html` are fragments as a builder emits them: markup and
class names, no stylesheet. U10's unit test never needs one, because its `*.json`
samples are hand-derived. The sensor's test does: a browser cannot render a look
that was never declared, and `StyleVector` measures the look.

So these two stylesheets supply what each candidate fragment presumes, and the test
composes `<style>` + the untouched fragment markup into a page it renders.

* `generic.css` — how a stock component library would set `candidate_generic.html`:
  white ground, zinc text, 8px radii, 16/32px gutters, 14/16/24px type, primary
  blue. Deliberately the same idiom as the three anti-corpus members.
* `styled.css` — how `direction/direction.toml` sets `candidate_styled.html`: the
  contract's own palette (converted to sRGB so `getComputedStyle` returns `rgb()`),
  sharp corners, and spacing/type values taken from the contract's modular scales
  (8 x 1.5^n and 17 x 1.25^n).

Keeping them here rather than in `tests/fixtures/blandness/` is deliberate: that
directory belongs to U10 and its numbers are hand-derived on purpose, while these
files are an input to the *sensor's* test and must be free to change when the
extraction does.
