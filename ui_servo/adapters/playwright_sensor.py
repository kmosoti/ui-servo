"""Chromium under the loop's own control: the shadow-browser sensor, implemented.

This is the adapter side of :mod:`ui_servo.ports.sensor` and the only place in the
package that knows a browser exists. It renders a page the loop just built,
photographs it whole and per ``data-span-id``, walks the accessibility tree, runs a
vendored axe-core over it, compares the pixels against a kept baseline, drives a
short interaction script while sampling frame timing, and finally reloads the same
page under ``prefers-reduced-motion: reduce`` to see whether the motion actually
stops. Every one of those observations also leaves as a
:class:`~ui_servo.domain.evidence.Signal`, attributed to the nearest span that owns
it, so the evidence stock holds one uniform, joinable record of the visit.

Six implementation choices are load-bearing and worth stating:

**Frame drops are sampled with rAF, not parsed out of a CDP trace.** The obvious
route -- ``Tracing.start`` over the CDP session and count ``Graphics``/
``DrawFrame`` events -- is brittle in exactly the way a sensor must not be: the
event names, categories and the frame-lifecycle model change between Chromium
builds, headless emits a different subset than headful, and a rename turns a
regression detector into a silent zero. A ``requestAnimationFrame`` gap sampler
installed in the page instead measures the thing we actually care about (did the
main thread stop serving frames while the user was interacting?) using an API that
is specified, stable and identical across browsers. A gap wider than
:data:`FRAME_BUDGET_MS` -- two refresh intervals at 60Hz -- counts as one dropped
frame. Long tasks come from ``PerformanceObserver`` and are filtered to those that
*started inside* the interaction window: the observer is created with
``buffered: true`` so nothing is missed, which means it replays the long tasks of
page load, and charging those to a hover would make every observation look janky.

**Every observation gets its own directory**, ``<artifacts>/<turn>/<span>/obs-NNNN/``.
Two observations of the same span in the same turn are two measurements -- the
before and after of a fix -- and a sensor whose second run overwrote the first
would destroy the pair a pixel diff is made of. For the same reason the caller's
baseline is copied into the observation directory *before* anything is captured,
so the image compared against is one nothing in this run can touch.

**A reduced-motion render is inspected twice and two ways.** Asking
``document.getAnimations()`` once after the page settles misses every finite
animation short enough to have finished -- which is most of the offending ones --
so the timeline is queried immediately after load *and* after settling, and the
union is taken. Both are then backstopped by a computed-style sweep: a non-zero
``animation-duration`` or ``transition-duration`` under ``reduce`` is a violation
whether or not anything happened to be running at the instant we looked.

**axe-core is vendored, pinned and integrity-checked** (see
``ui_servo/adapters/vendor/axe.min.js`` and :data:`AXE_VERSION` /
:data:`AXE_SHA256`). An audit engine fetched from a CDN at observation time would
make the loop's accessibility verdicts depend on the network and on whatever Deque
shipped this morning, so a turn-over-turn comparison of the evidence stock would
be measuring the audit engine as much as the UI. The digest is verified on first
use and a mismatch is an error, not a warning.

**The harness context bypasses the page's CSP** (:data:`DEFAULT_BYPASS_CSP`).
Injecting the audit engine is a measurement, and a page whose ``script-src`` is
correctly locked down would otherwise refuse it -- turning "this page has good CSP"
into "this page could not be audited", which is precisely backwards. Bypassing is
safe here because the shadow browser is not a user session: it renders a page the
loop just built, on a throwaway context, and nothing it loads is trusted with a
credential. Whether the page's *own* CSP holds up in a real session is a different
question measured by a different sensor -- the in-page probe (source ``probe``)
runs unprivileged in the served page and reports ``csp`` signals from there. One
sensor bypassing CSP and another observing it is the decorrelation working.

**Nothing here decides anything.** Violations are passed through verbatim, the
diff is a ratio, the trace is a pair of counts. Thresholds belong to the regulator.
"""

import hashlib
import math
import re
import shutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from PIL import Image, ImageChops
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)

from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.domain.variant import (
    COLOR_BINS,
    HUE_BINS,
    LIGHTNESS_BINS,
    LIGHTNESS_EDGES,
    StyleSample,
)
from ui_servo.ports.sensor import (
    FULL_PAGE_SCREENSHOT,
    AxeViolation,
    Interaction,
    PixelDiff,
    ScreenshotName,
    SensorReport,
    TraceSummary,
    span_screenshot_key,
)

VENDOR_DIR: Final[Path] = Path(__file__).resolve().parent / "vendor"
AXE_PATH: Final[Path] = VENDOR_DIR / "axe.min.js"
AXE_VERSION: Final[str] = "4.10.2"
AXE_SHA256: Final[str] = "b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3"
"""Pin of the vendored engine: axe-core 4.10.2, as published on npm/jsDelivr.

Refreshing it is a deliberate act -- ``curl -sSL
https://cdn.jsdelivr.net/npm/axe-core@<version>/axe.min.js -o
ui_servo/adapters/vendor/axe.min.js`` -- and both constants move with the file, so
a silent swap of the audit engine cannot happen underneath a run.
"""

AXE_TAGS: Final[tuple[str, ...]] = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
"""AA conformance, which by definition includes the A rules beneath it."""

FRAME_BUDGET_MS: Final[float] = 32.0
"""Two 60Hz refresh intervals: the gap at which a user reads motion as a stutter."""

PIXEL_CHANNEL_TOLERANCE: Final[int] = 8
"""Per-channel delta below which a pixel counts as unchanged (antialiasing noise)."""

MOTION_DURATION_TOLERANCE_MS: Final[float] = 1.0
"""Durations at or under this count as "none".

The universal reduced-motion reset collapses animations to ``0.01ms`` rather than
``none`` (a true zero would skip ``animationend`` and break scripts that wait for
it), so a strict ``> 0`` test would report the correct idiom as a violation.
"""

DEFAULT_BYPASS_CSP: Final[bool] = True
"""Whether the harness context ignores the page's Content-Security-Policy.

True by default because the audit engine is injected as an inline script and a
well-configured ``script-src`` would refuse it, converting a page's good security
posture into an unauditable page. See the module docstring for why this does not
blind the loop to CSP problems.
"""

SETTLE_MS: Final[int] = 150
DEFAULT_VIEWPORT: Final[tuple[int, int]] = (1280, 800)
STEP_TIMEOUT_MS: Final[float] = 5_000.0
PROBE_TIMEOUT_MS: Final[float] = 1_000.0
MIN_CAPTURE_EDGE_PX: Final[float] = 1.0

FULL_PAGE_FILENAME: Final[str] = "full-page.png"
BASELINE_FILENAME: Final[str] = "baseline.png"
DIFF_FILENAME: Final[str] = "diff.png"
ELEMENTS_DIRNAME: Final[str] = "elements"
OBSERVATION_PREFIX: Final[str] = "obs"

STYLE_ELEMENT_LIMIT: Final[int] = 250
"""Cap on sampled elements: the vector is a coarse distribution, not an inventory."""

HISTOGRAM_SAMPLE_SIZE: Final[tuple[int, int]] = (160, 100)
"""Resolution the screenshot is box-averaged to before the pixel histogram.

Sixteen thousand samples is far more than 24 bins need, and box-averaging down to
it is what makes the histogram a measure of *area* -- a full-bleed background wins
in proportion to how much of the render it actually covered.
"""

SCREENSHOT_KIND: Final[str] = "screenshot"
CAPTURE_ERROR_KIND: Final[str] = "capture-error"
ARIA_KIND: Final[str] = "aria-snapshot"
AXE_KIND: Final[str] = "axe-violation"
PIXEL_KIND: Final[str] = "pixel-diff"
FRAME_KIND: Final[str] = "frame-timing"
MOTION_KIND: Final[str] = "reduced-motion"
MOTION_FAILURE_KIND: Final[str] = "motion-violation"
STYLE_KIND: Final[str] = "style-sample"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_AXE_RUN_JS: Final[str] = """
async (tags) => {
  const results = await window.axe.run(document, {
    runOnly: { type: 'tag', values: tags },
    resultTypes: ['violations'],
  });
  const flatten = (target) => {
    if (Array.isArray(target)) { return flatten(target[target.length - 1]); }
    return typeof target === 'string' ? target : '';
  };
  const owner = (node) => {
    const selector = flatten(node.target);
    if (!selector) { return ''; }
    let element = null;
    try { element = document.querySelector(selector); } catch (error) { element = null; }
    if (!element || !element.closest) { return ''; }
    const holder = element.closest('[data-span-id]');
    return holder ? holder.getAttribute('data-span-id') || '' : '';
  };
  return {
    violations: results.violations,
    owners: results.violations.map((violation) => violation.nodes.map(owner)),
  };
}
"""

_STYLE_JS: Final[str] = """
(options) => {
  const root = (options.spanId
    && document.querySelector('[data-span-id="' + options.spanId.replace(/"/g, '\\\\"') + '"]'))
    || document.body;
  const parseColor = (value) => {
    const match = (value || '').match(/rgba?\\(([^)]+)\\)/);
    if (!match) { return null; }
    const parts = match[1].split(/[,\\s/]+/).filter((part) => part.length).map(Number);
    if (parts.length < 3 || parts.some((part) => !Number.isFinite(part))) { return null; }
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const backgroundOf = (element) => {
    let current = element;
    while (current) {
      const color = parseColor(getComputedStyle(current).backgroundColor);
      if (color && color.a > 0) { return color; }
      current = current.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const px = (value) => {
    const parsed = parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const label = (element) => {
    const tag = element.tagName ? element.tagName.toLowerCase() : 'node';
    const classes = element.classList && element.classList.length ? '.' + element.classList[0] : '';
    return tag + classes;
  };
  const selectorOf = (element) => {
    const parts = [];
    let current = element;
    for (let depth = 0; current && depth < 3; depth += 1) {
      parts.unshift(label(current));
      if (current === root || !current.parentElement) { break; }
      current = current.parentElement;
    }
    return parts.join(' > ');
  };
  const spacingProperties = [
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'marginTop', 'marginRight', 'marginBottom', 'marginLeft',
    'rowGap', 'columnGap',
  ];
  const elements = [];
  for (const element of [root, ...root.querySelectorAll('*')]) {
    if (elements.length >= options.limit) { break; }
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') { continue; }
    if (Number(style.opacity) === 0) { continue; }
    const rect = element.getBoundingClientRect();
    const spacing = [];
    for (const property of spacingProperties) {
      const value = px(style[property]);
      if (value > 0) { spacing.push(value); }
    }
    elements.push({
      selector: selectorOf(element),
      area_px: Math.max(0, rect.width) * Math.max(0, rect.height),
      color: parseColor(style.color),
      background: backgroundOf(element),
      font_size_px: px(style.fontSize),
      border_radius_px: Math.max(
        px(style.borderTopLeftRadius), px(style.borderTopRightRadius),
        px(style.borderBottomRightRadius), px(style.borderBottomLeftRadius),
      ),
      spacing_px: spacing,
    });
  }
  return {
    span_id: options.spanId || '',
    viewport: { width: window.innerWidth, height: window.innerHeight },
    elements: elements,
  };
}
"""

_SAMPLER_START_JS: Final[str] = """
() => {
  const state = {
    gaps: [],
    longtasks: 0,
    running: true,
    observer: null,
    start: performance.now(),
  };
  window.__uiServoSampler = state;
  let previous = state.start;
  const tick = (now) => {
    state.gaps.push(now - previous);
    previous = now;
    if (state.running) { requestAnimationFrame(tick); }
  };
  requestAnimationFrame(tick);
  try {
    state.observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.startTime >= state.start) { state.longtasks += 1; }
      }
    });
    state.observer.observe({ type: 'longtask', buffered: true });
  } catch (error) {
    state.observer = null;
  }
  return state.start;
}
"""

_SAMPLER_STOP_JS: Final[str] = """
() => {
  const state = window.__uiServoSampler;
  if (!state) { return { gaps: [], longtasks: 0, start: 0 }; }
  state.running = false;
  if (state.observer) { state.observer.disconnect(); }
  delete window.__uiServoSampler;
  return { gaps: state.gaps, longtasks: state.longtasks, start: state.start };
}
"""

_MOTION_JS: Final[str] = """
(tolerance) => {
  const owner = (element) => {
    if (!element || !element.closest) { return ''; }
    const holder = element.closest('[data-span-id]');
    return holder ? holder.getAttribute('data-span-id') || '' : '';
  };
  const name = (element) => {
    if (!element) { return ''; }
    return element.id || (element.tagName ? element.tagName.toLowerCase() : '');
  };
  const durations = (value) => (value || '').split(',').map((part) => {
    const trimmed = part.trim();
    if (trimmed.endsWith('ms')) { return parseFloat(trimmed) || 0; }
    if (trimmed.endsWith('s')) { return (parseFloat(trimmed) || 0) * 1000; }
    return parseFloat(trimmed) || 0;
  });

  const offenders = [];
  const animations = typeof document.getAnimations === 'function' ? document.getAnimations() : [];
  for (const animation of animations) {
    const effect = animation.effect;
    const timing = effect && effect.getComputedTiming ? effect.getComputedTiming() : null;
    const duration = timing ? Number(timing.duration) || 0 : 0;
    if (duration <= tolerance) { continue; }
    const target = effect && effect.target ? effect.target : null;
    offenders.push({
      source: 'animation-timeline',
      property: '',
      duration_ms: duration,
      iterations: timing ? String(timing.iterations) : '',
      name: animation.animationName || animation.transitionProperty || animation.id || '',
      span_id: owner(target),
      target: name(target),
    });
  }

  for (const element of document.querySelectorAll('*')) {
    const style = getComputedStyle(element);
    const animationName = style.animationName || 'none';
    if (animationName !== 'none') {
      for (const duration of durations(style.animationDuration)) {
        if (duration > tolerance) {
          offenders.push({
            source: 'computed-style',
            property: 'animation-duration',
            duration_ms: duration,
            iterations: String(style.animationIterationCount || ''),
            name: animationName,
            span_id: owner(element),
            target: name(element),
          });
          break;
        }
      }
    }
    const transitionProperty = style.transitionProperty || 'none';
    if (transitionProperty !== 'none') {
      for (const duration of durations(style.transitionDuration)) {
        if (duration > tolerance) {
          offenders.push({
            source: 'computed-style',
            property: 'transition-duration',
            duration_ms: duration,
            iterations: '',
            name: transitionProperty,
            span_id: owner(element),
            target: name(element),
          });
          break;
        }
      }
    }
  }
  return { total: animations.length, offenders: offenders };
}
"""


class SensorError(RuntimeError):
    """The shadow browser could not produce an observation.

    Raised instead of leaking ``playwright.sync_api.Error``, ``OSError`` or a
    Pillow exception, so the control loop can tell "the sensor failed" apart from
    "the sensor saw something wrong" -- which are opposite facts about the UI.
    """


def axe_source() -> str:
    """The vendored engine, verified against :data:`AXE_SHA256` before use."""
    try:
        source = AXE_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise SensorError(f"vendored axe-core missing at {AXE_PATH}: {error}") from error
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if digest != AXE_SHA256:
        raise SensorError(
            f"vendored axe-core at {AXE_PATH} has digest {digest}, expected {AXE_SHA256} "
            f"(pinned axe-core {AXE_VERSION})"
        )
    return source


@dataclass(frozen=True, slots=True)
class _Capture:
    """What the camera pass produced, including what it could not photograph."""

    screenshots: dict[ScreenshotName, Path]
    failures: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class _Audit:
    """Violations verbatim, plus the span each offending node belongs to."""

    violations: tuple[AxeViolation, ...] = ()
    owners: tuple[tuple[SpanId, ...], ...] = ()


@dataclass(slots=True)
class PlaywrightSensor:
    """A :class:`~ui_servo.ports.sensor.SensorPort` backed by headless Chromium.

    The browser is started on first use and kept for the object's life, because
    launching Chromium costs more than every observation in a round put together;
    the sensor is therefore a context manager and a round owns one. Each
    ``observe`` still gets a fresh browser *context* and a fresh artefact
    directory, so neither state nor files from one variant can colour the
    observation of the next.
    """

    artifacts_dir: Path
    headless: bool = True
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    device_scale_factor: float = 1.0
    axe_tags: tuple[str, ...] = AXE_TAGS
    frame_budget_ms: float = FRAME_BUDGET_MS
    pixel_tolerance: int = PIXEL_CHANNEL_TOLERANCE
    motion_tolerance_ms: float = MOTION_DURATION_TOLERANCE_MS
    settle_ms: int = SETTLE_MS
    bypass_csp: bool = DEFAULT_BYPASS_CSP
    _playwright: Playwright | None = field(default=None, init=False, repr=False)
    _browser: Browser | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> Self:
        self._start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release Chromium. Idempotent, so a failed round still tears down."""
        browser, driver = self._browser, self._playwright
        self._browser, self._playwright = None, None
        for shutdown in (getattr(browser, "close", None), getattr(driver, "stop", None)):
            if shutdown is None:
                continue
            try:
                shutdown()
            except PlaywrightError:
                continue

    def observe(
        self,
        url: str,
        *,
        span_id: SpanId,
        turn_id: TurnId,
        baseline: Path | None = None,
        interactions: Sequence[Interaction] | None = None,
    ) -> SensorReport:
        """Visit *url* and report everything a machine can see, rooted at *span_id*.

        The order is not arbitrary. The baseline is copied out of the caller's
        hands first, so the image compared against cannot be the image this run is
        about to write. Static capture and audit then happen before the interaction
        script runs, so the pixel baseline is always the page at rest and a diff
        never mixes "the design changed" with "the script left a menu open".
        """
        browser = self._start()
        observation_dir = self._observation_dir(turn_id, span_id)
        kept_baseline = self._keep_baseline(baseline, observation_dir)
        context = self._new_context(browser)
        try:
            page = context.new_page()
            self._goto(page, url)
            capture = self._capture(page, observation_dir)
            aria_snapshot = self._aria_snapshot(page)
            style_sample = self._style_sample(
                page, span_id, capture.screenshots.get(FULL_PAGE_SCREENSHOT)
            )
            audit = self._run_axe(page)
            trace = self._drive(page, interactions) if interactions else None
        except PlaywrightError as error:
            raise SensorError(f"observation of {url} failed: {error}") from error
        finally:
            context.close()

        pixel_diff = self._pixel_diff(
            capture.screenshots.get(FULL_PAGE_SCREENSHOT), kept_baseline, observation_dir
        )
        reduced_motion_ok, offenders = self._reduced_motion(browser, url)

        report = SensorReport(
            span_id=span_id,
            turn_id=turn_id,
            url=url,
            screenshots=capture.screenshots,
            aria_snapshot=aria_snapshot,
            axe_violations=audit.violations,
            pixel_diff=pixel_diff,
            trace=trace,
            reduced_motion_ok=reduced_motion_ok,
            style_sample=style_sample,
        )
        return replace(
            report,
            signals=tuple(
                self._signals(
                    report=report, audit=audit, capture=capture, motion_offenders=offenders
                )
            ),
        )

    def _start(self) -> Browser:
        if self._browser is not None:
            return self._browser
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
        except PlaywrightError as error:
            self.close()
            raise SensorError(
                f"could not launch chromium: {error} (try `uv run playwright install chromium`)"
            ) from error
        return self._browser

    def _new_context(self, browser: Browser, **overrides: Any) -> BrowserContext:
        try:
            return browser.new_context(
                viewport={"width": self.viewport[0], "height": self.viewport[1]},
                device_scale_factor=self.device_scale_factor,
                bypass_csp=self.bypass_csp,
                **overrides,
            )
        except PlaywrightError as error:
            raise SensorError(f"could not open a browser context: {error}") from error

    def _observation_dir(self, turn_id: TurnId, span_id: SpanId) -> Path:
        """A directory this observation alone owns.

        Namespaced by turn and span so two spans never share a filename, and
        numbered so a re-observation of the same span in the same turn -- the
        before and after of a fix -- is kept beside its predecessor instead of on
        top of it. The directory is created exclusively, so two sensors writing
        into one artefacts root cannot be handed the same one.
        """
        base = self.artifacts_dir / _safe(turn_id) / _safe(span_id)
        try:
            base.mkdir(parents=True, exist_ok=True)
            for attempt in range(1, 10_000):
                candidate = base / f"{OBSERVATION_PREFIX}-{attempt:04d}"
                try:
                    candidate.mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                return candidate
        except OSError as error:
            raise SensorError(f"could not create an artefact directory under {base}: {error}")
        raise SensorError(f"more than 9999 observations of {span_id!r} in turn {turn_id!r}")

    def _keep_baseline(self, baseline: Path | None, observation_dir: Path) -> Path | None:
        """Copy the caller's baseline in before anything is captured.

        A baseline is usually a previous observation's full-page screenshot, and
        the comparison has to be against the bytes as they were when the caller
        chose them. Copying makes the observation self-contained: the diff, the
        image it was taken against and the render that produced it all sit in one
        directory that nothing else writes to.
        """
        if baseline is None:
            return None
        source = Path(baseline)
        if not source.exists():
            raise SensorError(f"baseline screenshot {source} does not exist")
        kept = observation_dir / BASELINE_FILENAME
        try:
            shutil.copy2(source, kept)
        except OSError as error:
            raise SensorError(f"could not keep baseline {source}: {error}") from error
        return kept

    def _goto(self, page: Page, url: str) -> None:
        page.goto(url, wait_until="load")
        page.wait_for_timeout(self.settle_ms)

    def _capture(self, page: Page, observation_dir: Path) -> _Capture:
        """Whole page plus one image per ``data-span-id`` element.

        The per-span images are what make a diff attributable: a full-page ratio
        says the page moved, and only the element crops say which fragment did. An
        element that cannot be photographed -- hidden, zero-area, detached between
        the query and the shutter -- is reported as a failure rather than dropped:
        a silently missing screenshot reads downstream as a span that was never
        rendered, which is a different and much more alarming fact.
        """
        full = observation_dir / FULL_PAGE_FILENAME
        page.screenshot(path=str(full), full_page=True)
        screenshots: dict[ScreenshotName, Path] = {FULL_PAGE_SCREENSHOT: full}
        failures: list[dict[str, Any]] = []
        elements_dir = observation_dir / ELEMENTS_DIRNAME
        for index, element in enumerate(page.locator("[data-span-id]").all()):
            name = self._element_span_id(element, index)
            reason = self._unphotographable(element)
            if reason is not None:
                failures.append({"span_id": name, "reason": reason})
                continue
            elements_dir.mkdir(parents=True, exist_ok=True)
            path = elements_dir / f"{_safe(name)}.png"
            try:
                element.screenshot(path=str(path), timeout=STEP_TIMEOUT_MS)
            except PlaywrightError as error:
                failures.append({"span_id": name, "reason": _first_line(str(error))})
                continue
            screenshots[span_screenshot_key(name)] = path
        return _Capture(screenshots=screenshots, failures=tuple(failures))

    def _element_span_id(self, element: Locator, index: int) -> SpanId:
        try:
            return element.get_attribute("data-span-id", timeout=PROBE_TIMEOUT_MS) or f"span-{index}"
        except PlaywrightError:
            return f"span-{index}"

    def _unphotographable(self, element: Locator) -> str | None:
        """Why this element cannot be captured, decided without waiting on it.

        Checked up front rather than by letting ``screenshot`` time out: a page
        with three hidden spans would otherwise cost fifteen seconds per
        observation, and a sensor slow enough to be skipped is a sensor that does
        not run.
        """
        try:
            box = element.bounding_box(timeout=PROBE_TIMEOUT_MS)
            if box is None:
                return "element has no box (detached, display:none, or never laid out)"
            if box["width"] < MIN_CAPTURE_EDGE_PX or box["height"] < MIN_CAPTURE_EDGE_PX:
                return f"element is zero-area ({box['width']}x{box['height']})"
            if not element.is_visible(timeout=PROBE_TIMEOUT_MS):
                return "element is laid out but not visible (visibility:hidden or opacity:0)"
        except PlaywrightError as error:
            return _first_line(str(error))
        return None

    def _aria_snapshot(self, page: Page) -> str:
        """The accessibility tree as YAML -- the page as a screen reader receives it."""
        try:
            return page.locator("body").aria_snapshot(timeout=STEP_TIMEOUT_MS)
        except (PlaywrightError, AttributeError):
            return str(page.accessibility.snapshot() or "")

    def _style_sample(
        self, page: Page, span_id: SpanId, screenshot: Path | None
    ) -> StyleSample | None:
        """The render reduced to the numbers :class:`StyleVector` embeds.

        Two independent views of the same look, because they are wrong in
        different directions. Computed styles know the *intent* of every element,
        including ones the eye barely registers; the pixel histogram knows what
        the viewport actually became. The domain merges them; the adapter's job is
        to produce both honestly.

        Everything impure lives here by design: ``getComputedStyle``, the
        sRGB-to-OKLCH conversion, the WCAG contrast maths and the PNG decode. The
        domain receives numbers and only ever bins them, which is what lets a
        second sensor family -- another browser, a static renderer, a fixture file
        -- feed the same vector without the domain learning anything new.
        """
        raw = page.evaluate(_STYLE_JS, {"spanId": span_id, "limit": STYLE_ELEMENT_LIMIT})
        if not raw:
            return None
        elements: list[dict[str, Any]] = []
        for element in raw.get("elements", ()):
            foreground = _rgb(element.get("color"))
            background = _rgb(element.get("background"))
            entry: dict[str, Any] = {
                "selector": str(element.get("selector", "")),
                "area_px": max(0.0, float(element.get("area_px", 0.0))),
                "spacing_px": [
                    float(value) for value in element.get("spacing_px", ()) if float(value) > 0.0
                ],
                "border_radius_px": max(0.0, float(element.get("border_radius_px", 0.0))),
            }
            if foreground is not None:
                entry["color"] = _oklch(foreground)
            if background is not None:
                entry["background"] = _oklch(background)
            if foreground is not None and background is not None:
                entry["contrast_ratio"] = _contrast_ratio(foreground, background)
            font_size = float(element.get("font_size_px", 0.0))
            if font_size > 0.0:
                entry["font_size_px"] = font_size
            elements.append(entry)
        payload: dict[str, Any] = {
            "span_id": str(raw.get("span_id", "")) or span_id,
            "viewport": raw.get("viewport"),
            "elements": elements,
        }
        histogram = self._screenshot_histogram(screenshot)
        if histogram is not None:
            payload["screenshot"] = {"oklch_bins": histogram}
        try:
            return StyleSample.parse(payload)
        except ValueError as error:
            raise SensorError(f"style sample for {span_id!r} is malformed: {error}") from error

    def _screenshot_histogram(self, screenshot: Path | None) -> list[float] | None:
        """Pixel counts per OKLCH bin, in the domain's own bin order."""
        if screenshot is None:
            return None
        try:
            with Image.open(screenshot) as image:
                thumbnail = image.convert("RGB").resize(
                    HISTOGRAM_SAMPLE_SIZE, Image.Resampling.BOX
                )
        except OSError as error:
            raise SensorError(f"could not read {screenshot} for a histogram: {error}") from error
        bins = [0.0] * COLOR_BINS
        raw = thumbnail.tobytes()
        for offset in range(0, len(raw) - 2, 3):
            lightness, _chroma, hue = _srgb_to_oklch(
                (raw[offset], raw[offset + 1], raw[offset + 2])
            )
            bins[_color_bin(lightness, hue)] += 1.0
        return bins

    def _run_axe(self, page: Page) -> _Audit:
        """Inject the pinned engine and hand back its violations untouched.

        Verbatim matters: axe's node entries carry the failure summary, the target
        selector and the offending markup, which is precisely what a builder needs
        to fix the thing without a second round trip to the browser. The owning
        span of each node is resolved in the page, while the DOM is still there --
        the selector is only meaningful in the document it came from.
        """
        page.add_script_tag(content=axe_source())
        result = page.evaluate(_AXE_RUN_JS, list(self.axe_tags)) or {}
        violations = tuple(dict(violation) for violation in result.get("violations", ()))
        owners = tuple(
            tuple(str(owner) for owner in per_violation)
            for per_violation in result.get("owners", ())
        )
        return _Audit(violations=violations, owners=owners)

    def _drive(self, page: Page, interactions: Sequence[Interaction]) -> TraceSummary:
        page.evaluate(_SAMPLER_START_JS)
        for step in interactions:
            self._perform(page, step)
        page.wait_for_timeout(self.settle_ms)
        sample = page.evaluate(_SAMPLER_STOP_JS) or {}
        gaps = [float(gap) for gap in sample.get("gaps", [])]
        return TraceSummary(
            dropped_frames=sum(1 for gap in gaps if gap > self.frame_budget_ms),
            longtasks=int(sample.get("longtasks", 0)),
            sampled_frames=len(gaps),
            max_frame_gap_ms=max(gaps, default=0.0),
        )

    def _perform(self, page: Page, step: Interaction) -> None:
        """One step of the interaction script.

        The vocabulary is deliberately tiny and declarative. An interaction script
        is data that travels with a variant into the evidence stock, so it has to
        be serialisable and readable months later; an escape hatch to arbitrary
        Python here would make a recorded observation unreproducible.
        """
        match step:
            case {"action": "click", "selector": str(selector)}:
                page.click(selector, timeout=STEP_TIMEOUT_MS)
            case {"action": "hover", "selector": str(selector)}:
                page.hover(selector, timeout=STEP_TIMEOUT_MS)
            case {"action": "press", "selector": str(selector), "key": str(key)}:
                page.press(selector, key, timeout=STEP_TIMEOUT_MS)
            case {"action": "fill", "selector": str(selector), "value": str(value)}:
                page.fill(selector, value, timeout=STEP_TIMEOUT_MS)
            case {"action": "focus", "selector": str(selector)}:
                page.focus(selector, timeout=STEP_TIMEOUT_MS)
            case {"action": "wait", "selector": str(selector)}:
                page.wait_for_selector(selector, timeout=STEP_TIMEOUT_MS)
            case {"action": "wait", "ms": int() | float() as milliseconds}:
                page.wait_for_timeout(float(milliseconds))
            case {"action": "scroll", **rest}:
                page.mouse.wheel(float(rest.get("x", 0)), float(rest.get("y", 600)))
            case {"action": str(unknown)}:
                raise SensorError(f"unknown interaction action {unknown!r} in step {step!r}")
            case _:
                raise SensorError(f"interaction step {step!r} has no 'action'")

    def _pixel_diff(
        self, current: Path | None, baseline: Path | None, observation_dir: Path
    ) -> PixelDiff | None:
        """Fraction of pixels that moved, plus a heatmap of where.

        ``None`` when there is no baseline, because "nothing changed" and "nothing
        to compare against" are different facts and a zero would launder the second
        into the first. A size change counts as a change everywhere the two images
        do not overlap: the canvases are padded with opposite sentinels so that
        growth in page height cannot hide behind a same-coloured background.
        """
        if current is None or baseline is None:
            return None
        try:
            with Image.open(baseline) as before_file, Image.open(current) as after_file:
                before, after = before_file.convert("RGB"), after_file.convert("RGB")
                size = (max(before.width, after.width), max(before.height, after.height))
                before_canvas = _padded(before, size, fill=(0, 0, 0))
                after_canvas = _padded(after, size, fill=(255, 255, 255))
                channels = ImageChops.difference(before_canvas, after_canvas).split()
                delta = channels[0]
                for channel in channels[1:]:
                    delta = ImageChops.lighter(delta, channel)
                mask = delta.point(lambda value: 255 if value > self.pixel_tolerance else 0)
                changed = mask.histogram()[255]
                ratio = changed / float(size[0] * size[1])
                diff_path: Path | None = None
                if changed:
                    diff_path = observation_dir / DIFF_FILENAME
                    _heatmap(after_canvas, mask).save(diff_path)
        except OSError as error:
            raise SensorError(f"could not diff {current} against {baseline}: {error}") from error
        return PixelDiff(ratio=ratio, diff_path=diff_path, baseline_path=baseline)

    def _reduced_motion(self, browser: Browser, url: str) -> tuple[bool, tuple[dict[str, Any], ...]]:
        """Re-render under ``prefers-reduced-motion: reduce`` and look for movement.

        Asking the stylesheet whether it contains the media query would prove only
        that someone typed it. Asking the *animation timeline* whether anything is
        still scheduled to move proves the query is wired to the animations that
        exist, which is the property a vestibular-disorder user actually needs.

        The timeline alone is not enough, though, and the failure mode is a false
        pass: a 250ms entrance animation is over before any settle wait ends, so a
        single late query sees an empty timeline and calls an offending page clean.
        Hence three looks -- the timeline immediately after load, the timeline after
        settling, and a computed-style sweep that finds a declared duration whether
        or not it is currently running -- unioned into one offender list.
        """
        context = self._new_context(browser)
        try:
            page = context.new_page()
            page.emulate_media(reduced_motion="reduce")
            page.goto(url, wait_until="load")
            page.reload(wait_until="load")
            immediate = page.evaluate(_MOTION_JS, self.motion_tolerance_ms) or {}
            page.wait_for_timeout(self.settle_ms)
            settled = page.evaluate(_MOTION_JS, self.motion_tolerance_ms) or {}
        except PlaywrightError as error:
            raise SensorError(f"reduced-motion check of {url} failed: {error}") from error
        finally:
            context.close()
        offenders = _unique_offenders(
            (*immediate.get("offenders", ()), *settled.get("offenders", ()))
        )
        return not offenders, offenders

    def _signals(
        self,
        *,
        report: SensorReport,
        audit: _Audit,
        capture: _Capture,
        motion_offenders: Sequence[Mapping[str, Any]],
    ) -> Iterator[Signal]:
        """Restate the report as flows into the evidence stock.

        One signal per fact, never one per report: the stock is queried by kind,
        and a single fat ``harness-report`` signal would force every consumer to
        re-parse a payload to find the one number it cares about.

        Attribution is the other half. A finding is stamped with the span that owns
        it -- the fragment the axe node sits inside, the element that was
        photographed, the element that is still animating -- and falls back to the
        observation's root span only when the owner cannot be determined. Stamping
        everything with the root would make the join key useless at exactly the
        moment it matters: telling which of six fragments on a page is the broken
        one.
        """
        root = report.span_id
        observed_at = datetime.now(UTC).isoformat()

        def signal(kind: str, payload: dict[str, Any], *, span_id: SpanId | None = None) -> Signal:
            return Signal(
                span_id=span_id or root,
                turn_id=report.turn_id,
                source="harness",
                kind=kind,
                ts=observed_at,
                payload=payload,
            )

        full_page = report.full_page_screenshot
        if full_page is not None:
            yield signal(
                SCREENSHOT_KIND,
                {"url": report.url, "scope": "full-page", "path": str(full_page)},
            )
        for span_id, path in report.span_screenshots.items():
            yield signal(
                SCREENSHOT_KIND,
                {"url": report.url, "scope": "element", "path": str(path)},
                span_id=span_id,
            )
        for failure in capture.failures:
            yield signal(
                CAPTURE_ERROR_KIND,
                {"url": report.url, "scope": "element", "reason": failure["reason"]},
                span_id=str(failure["span_id"]) or root,
            )
        yield signal(ARIA_KIND, {"url": report.url, "aria_snapshot": report.aria_snapshot})
        if report.style_sample is not None:
            yield signal(
                STYLE_KIND,
                {
                    "url": report.url,
                    "element_count": len(report.style_sample.elements),
                    "sample": report.style_sample.model_dump(mode="json"),
                },
            )
        yield from self._axe_signals(audit=audit, signal=signal, root=root)
        if report.pixel_diff is not None:
            yield signal(
                PIXEL_KIND,
                {
                    "ratio": report.pixel_diff.ratio,
                    "diff_path": _maybe_str(report.pixel_diff.diff_path),
                    "baseline_path": _maybe_str(report.pixel_diff.baseline_path),
                },
            )
        if report.trace is not None:
            yield signal(
                FRAME_KIND,
                {
                    "dropped_frames": report.trace.dropped_frames,
                    "longtasks": report.trace.longtasks,
                    "sampled_frames": report.trace.sampled_frames,
                    "max_frame_gap_ms": report.trace.max_frame_gap_ms,
                    "budget_ms": self.frame_budget_ms,
                    "method": "raf-gap-sampler",
                },
            )
        yield signal(
            MOTION_KIND,
            {
                "ok": report.reduced_motion_ok,
                "offenders": [dict(offender) for offender in motion_offenders],
                "tolerance_ms": self.motion_tolerance_ms,
            },
        )
        for span_id, owned in _grouped_by_span(motion_offenders, root=root).items():
            yield signal(
                MOTION_FAILURE_KIND,
                {"offenders": owned, "tolerance_ms": self.motion_tolerance_ms},
                span_id=span_id,
            )

    def _axe_signals(
        self, *, audit: _Audit, signal: Any, root: SpanId
    ) -> Iterator[Signal]:
        for index, violation in enumerate(audit.violations):
            owners = audit.owners[index] if index < len(audit.owners) else ()
            attributed = tuple(dict.fromkeys(owner for owner in owners if owner)) or (root,)
            for span_id in attributed:
                yield signal(
                    AXE_KIND,
                    {
                        "rule": violation.get("id", ""),
                        "impact": violation.get("impact"),
                        "tags": list(violation.get("tags", ())),
                        "help": violation.get("help", ""),
                        "engine": f"axe-core {AXE_VERSION}",
                        "spans": list(attributed),
                        "violation": violation,
                    },
                    span_id=span_id,
                )


def _rgb(color: Mapping[str, Any] | None) -> tuple[float, float, float] | None:
    if not color:
        return None
    try:
        return (float(color["r"]), float(color["g"]), float(color["b"]))
    except (KeyError, TypeError, ValueError):
        return None


def _linear(channel: float) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _srgb_to_oklch(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB bytes to OKLCH, the space the domain's colour bins are cut in.

    Perceptual rather than convenient: two colours that land in the same OKLCH bin
    look related to a person, which is false of an RGB or HSL histogram and is the
    whole reason the style vector can say "these two pages look the same".
    """
    red, green, blue = (_linear(channel) for channel in rgb)
    long = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    long_, medium_, short_ = (math.cbrt(value) for value in (long, medium, short))
    lightness = 0.2104542553 * long_ + 0.7936177850 * medium_ - 0.0040720468 * short_
    green_red = 1.9779984951 * long_ - 2.4285922050 * medium_ + 0.4505937099 * short_
    blue_yellow = 0.0259040371 * long_ + 0.7827717662 * medium_ - 0.8086757660 * short_
    chroma = math.hypot(green_red, blue_yellow)
    hue = math.degrees(math.atan2(blue_yellow, green_red)) % 360.0
    return (min(max(lightness, 0.0), 1.0), max(chroma, 0.0), hue)


def _oklch(rgb: tuple[float, float, float]) -> dict[str, float]:
    lightness, chroma, hue = _srgb_to_oklch(rgb)
    return {"l": lightness, "c": chroma, "h": hue}


def _color_bin(lightness: float, hue: float) -> int:
    """The domain's bin index, computed from the domain's own edges."""
    hue_bin = min(int(hue * HUE_BINS / 360.0), HUE_BINS - 1)
    low, high = LIGHTNESS_EDGES
    lightness_bin = 0 if lightness < low else (1 if lightness < high else 2)
    return hue_bin * LIGHTNESS_BINS + lightness_bin


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(
    foreground: tuple[float, float, float], background: tuple[float, float, float]
) -> float:
    """WCAG 2.x contrast, computed where the pixels are rather than in the domain."""
    lighter = max(_relative_luminance(foreground), _relative_luminance(background))
    darker = min(_relative_luminance(foreground), _relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def _safe(name: str) -> str:
    """A path segment that keeps the id readable but cannot escape the artefacts dir."""
    cleaned = _UNSAFE_NAME.sub("-", name).strip("-.")
    return cleaned or "span"


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else "unknown error"


def _unique_offenders(offenders: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """De-duplicate the union of the three reduced-motion looks, order preserved."""
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for offender in offenders:
        key = (
            offender.get("source"),
            offender.get("property"),
            offender.get("name"),
            offender.get("span_id"),
            offender.get("target"),
            round(float(offender.get("duration_ms", 0.0)), 3),
        )
        seen.setdefault(key, dict(offender))
    return tuple(seen.values())


def _grouped_by_span(
    offenders: Sequence[Mapping[str, Any]], *, root: SpanId
) -> dict[SpanId, list[dict[str, Any]]]:
    grouped: dict[SpanId, list[dict[str, Any]]] = {}
    for offender in offenders:
        span_id = str(offender.get("span_id") or "") or root
        grouped.setdefault(span_id, []).append(dict(offender))
    return grouped


def _padded(image: Image.Image, size: tuple[int, int], *, fill: tuple[int, int, int]) -> Image.Image:
    if image.size == size:
        return image
    canvas = Image.new("RGB", size, fill)
    canvas.paste(image, (0, 0))
    return canvas


def _heatmap(current: Image.Image, mask: Image.Image) -> Image.Image:
    """Changed pixels in alarm red over a washed-out copy of what was rendered.

    The context matters as much as the mask: a diff image showing only the delta
    tells a reviewer that 3% of pixels moved and nothing about whether the thing
    that moved was the nav or a shadow.
    """
    washed = Image.blend(current, Image.new("RGB", current.size, (255, 255, 255)), 0.7)
    alarm = Image.new("RGB", current.size, (222, 24, 76))
    return Image.composite(alarm, washed, mask.convert("1"))


def _maybe_str(path: Path | None) -> str | None:
    return None if path is None else str(path)
