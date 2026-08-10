"""Permanent regression for the font-dedup (batch, 2026-08-09) -- see the
header comment in `site/assets/fonts/fonts.css` for what changed and why.

What this file guards specifically: fonts.css now declares `font-weight`
*ranges* (`400 700`, `300 800`) against one variable-font src per family,
instead of one src per weight. If that range resolution were ever wrong --
clamped to one end, or snapped to the default instance -- every weight but
one would render identically, and nothing about the markup or the file list
would say so.

Two different signals, not one: `offsetWidth` cannot see a weight
substitution on JetBrains Mono, because monospace fonts hold the same
advance width across weight and style by design (so bolding text never
reshuffles a column) -- checked directly against the shipped file, 400/500/700
measure bit-identical even at `getBoundingClientRect()` sub-pixel precision.
Public Sans is checked by `offsetWidth` (its variable font does widen
slightly with weight); JetBrains Mono is checked by rendered ink coverage on
an offscreen canvas instead -- a bolder stroke covers strictly more pixels at
the same advance width. Both run against the actual served home page, through
the same fonts.css a visitor's browser parses, not against the font files
directly.

Requires a Rust toolchain (to build and run the real server) and a Playwright
Chromium install; skipped rather than failed when either is absent, same as
`test_island.py`.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"
SERVER_BINARY = SITE / "target" / "debug" / "ui-servo-site"
BOOT_TIMEOUT_S = 30.0

HOME = "/"
SAMPLE_TEXT = "Hamburgefonstiv 0123 —"

# The weights this file asserts render distinguishably from one another --
# the mission's own list, plus 500 in each so the probe covers a range
# interior and not just its endpoints.
PUBLIC_SANS_WEIGHTS = [300, 400, 500, 700, 800]
JETBRAINS_MONO_WEIGHTS = [400, 500, 700]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    """A real `cargo`-built server, launched directly so killing it cannot
    leave `cargo` itself holding the port."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    subprocess.run(["cargo", "build"], cwd=SITE, check=True)

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ | {"UI_SERVO_PORT": str(port), "RUST_LOG": "warn"}
    server = subprocess.Popen(
        [str(SERVER_BINARY)],
        cwd=SITE,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"server exited early with {server.returncode}")
            try:
                with urllib.request.urlopen(f"{url}/", timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        else:
            raise RuntimeError(f"server never answered on {url}")
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as error:  # pragma: no cover - environment, not logic
            pytest.skip(f"chromium is not available: {error}")
        try:
            yield instance
        finally:
            instance.close()


# One browser round trip per test: every weight for one family is
# `document.fonts.load()`-ed and awaited explicitly (a weight nothing else on
# the page renders yet has never been requested before this call, so reading
# it a tick early would land mid-swap, against the fallback font rather than
# the real face), then measured by whichever metric the test asked for --
# only that metric, not both computed and one discarded.
#
# `load()`'s own `.catch` only pre-warms -- it must not swallow a real
# failure, or a 404'd woff2 would silently fall back to a system font and
# this test would pass against the wrong typeface, which is the exact
# regression it exists to catch. `document.fonts.ready` resolving is not
# proof either (it resolves once nothing is *loading*, including "gave up").
# `document.fonts.check()` after the await is the actual gate: it is false
# unless a face matching that shorthand is loaded and ready to paint, so a
# missing/failed face is caught here, loudly, before any width or ink is read.
MEASURE_JS = """
async ({ spec, metric }) => {
  const text = %s;
  await Promise.all(spec.map(([family, weight]) =>
    document.fonts.load(weight + ' 32px "' + family + '"', text).catch(() => {})
  ));
  await document.fonts.ready;

  const missing = spec.filter(
    ([family, weight]) => !document.fonts.check(weight + ' 32px "' + family + '"')
  );
  if (missing.length) {
    throw new Error('faces not loaded: ' + JSON.stringify(missing));
  }

  function width(family, weight) {
    const span = document.createElement('span');
    span.textContent = text;
    span.style.position = 'absolute';
    span.style.visibility = 'hidden';
    span.style.whiteSpace = 'pre';
    span.style.fontSize = '32px';
    span.style.fontStyle = 'normal';
    span.style.fontFamily = "'" + family + "'";
    span.style.fontWeight = String(weight);
    document.body.appendChild(span);
    const value = span.offsetWidth;
    span.remove();
    return value;
  }

  function ink(family, weight) {
    const size = 64;
    const canvas = document.createElement('canvas');
    canvas.width = 900; canvas.height = 110;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#fff';
    ctx.font = weight + ' ' + size + 'px "' + family + '"';
    ctx.textBaseline = 'top';
    ctx.fillText(text, 4, 4);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let lit = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] > 10) lit++;
    }
    return lit;
  }

  const measure = metric === 'ink' ? ink : width;
  const out = {};
  for (const [family, weight] of spec) {
    out[family + ':' + weight] = measure(family, weight);
  }
  return out;
}
""" % json.dumps(SAMPLE_TEXT)


def measure(
    page: Page, base_url: str, spec: list[tuple[str, int]], metric: str
) -> dict[str, int]:
    page.goto(f"{base_url}{HOME}", wait_until="load")
    page.evaluate("() => document.fonts.ready")
    return page.evaluate(MEASURE_JS, {"spec": spec, "metric": metric})


@pytest.mark.playwright
@pytest.mark.parametrize(
    ("family", "weights", "metric"),
    [
        pytest.param("Public Sans", PUBLIC_SANS_WEIGHTS, "width", id="public-sans-width"),
        pytest.param("JetBrains Mono", JETBRAINS_MONO_WEIGHTS, "ink", id="jetbrains-mono-ink"),
    ],
)
def test_weights_render_pairwise_distinct(
    family: str, weights: list[int], metric: str, browser: Browser, base_url: str
) -> None:
    """A weight collapsed onto its neighbour by a bad range resolution shows
    up here as two equal values where the mission's weight list expects
    distinct ones. `metric` is `width` for Public Sans and `ink` for
    JetBrains Mono -- see the module docstring for why the metric differs
    per family (offsetWidth is blind to weight on a monospace font)."""
    page = browser.new_page()
    try:
        spec = [(family, w) for w in weights]
        measured = measure(page, base_url, spec, metric)
        values = [measured[f"{family}:{w}"] for w in weights]
    finally:
        page.close()

    assert len(set(values)) == len(values), (
        f"{family} weights {weights} did not render pairwise-distinct {metric}: "
        f"{dict(zip(weights, values))}"
    )
