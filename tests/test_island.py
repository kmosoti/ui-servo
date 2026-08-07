"""Calibration for the WASM island: it must draw, it must hold still when asked,
and it must be able to fail *legibly*.

The first two are the island as a feature. The third is the island as a sensor
subject, and it is the reason this file drives a real browser instead of asserting
on markup: a panic inside wasm is a trap, and by the time JS sees it the message
is gone. Only the hook installed inside the module can still name what happened,
and only a browser can prove the hook is wired to the probe.

The chain under test, end to end:

    ?panic=1 -> loader.js -> provoke_panic() -> Rust panic hook
             -> ui-servo:wasm-panic CustomEvent (bubbling, from the element)
             -> probe.js document listener -> beacon POST, attributed to the
                fragment's data-span-id

Both halves of the last hop are asserted. The CustomEvent itself is checked with
a page-level listener installed before navigation, and the beacon is checked by
intercepting the POST with ``page.route`` -- the preview server is a separate
unit, so this file stands the beacon endpoint up itself rather than depending on
one being run alongside.

Requires a built island (``site/islands/build.sh``, i.e. ``wasm-pack``) and a
Rust toolchain. Everything is skipped rather than failed when those are absent:
a missing toolchain is not evidence of a broken island.
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
from playwright.sync_api import Browser, Page, Route, sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"
ISLAND_CRATE = SITE / "islands"
ISLAND_ASSETS = SITE / "assets" / "islands"
GLUE = ISLAND_ASSETS / "ui_servo_islands.js"
WASM = ISLAND_ASSETS / "ui_servo_islands_bg.wasm"
LOADER = ISLAND_ASSETS / "loader.js"
BUILD = ISLAND_CRATE / "build.sh"

SERVER_BINARY = SITE / "target" / "debug" / "ui-servo-site"
TURN_ID = "t-island-acceptance"
BOOT_TIMEOUT_S = 30.0
ACTION_TIMEOUT_MS = 15_000

# A fingerprint of what is on the canvas: how many pixels are painted at all,
# and a cheap order-sensitive checksum of them. Two frames of the same drift are
# never identical; two renders of a *static* island always are.
FINGERPRINT = """(selector) => {
  const canvas = document.querySelector(selector + ' canvas');
  if (!canvas) return null;
  const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  let painted = 0;
  let checksum = 0;
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] > 8) {
      painted += 1;
      checksum = (checksum + i * data[i]) % 2147483647;
    }
  }
  return { painted, checksum, width: canvas.width, height: canvas.height };
}"""

# Installed before navigation so the listeners beat the island's own upgrade.
#
# The counters are test scaffolding deliberately kept *out* of the component: an
# element that publishes its own listener bookkeeping is publishing it to
# production too. `ResizeObserver` and `MediaQueryList` are wrapped instead, and
# media listeners are recorded with their query so this counts only the
# island's, not any other page code's.
CAPTURE_EVENTS = """
  window.__ISLAND_PANICS__ = [];
  window.__ISLAND_CE_ERRORS__ = [];
  document.addEventListener('ui-servo:wasm-panic', (event) => {
    const host = event.target.closest ? event.target.closest('[data-span-id]') : null;
    window.__ISLAND_PANICS__.push({
      ...event.detail,
      spanId: host ? host.dataset.spanId : null,
    });
  }, true);
  document.addEventListener(
    'ui-servo:ce-error', (event) => window.__ISLAND_CE_ERRORS__.push(event.detail), true);

  window.__ISLAND_OBSERVERS__ = { created: 0, disconnected: 0 };
  if (typeof ResizeObserver === 'function') {
    const Native = ResizeObserver;
    window.ResizeObserver = class extends Native {
      constructor(callback) {
        super(callback);
        window.__ISLAND_OBSERVERS__.created += 1;
      }
      disconnect() {
        window.__ISLAND_OBSERVERS__.disconnected += 1;
        return super.disconnect();
      }
    };
  }

  window.__ISLAND_MEDIA__ = { added: [], removed: [] };
  if (typeof MediaQueryList === 'function') {
    const proto = MediaQueryList.prototype;
    const add = proto.addEventListener;
    const remove = proto.removeEventListener;
    proto.addEventListener = function (...args) {
      window.__ISLAND_MEDIA__.added.push(this.media);
      return add.apply(this, args);
    };
    proto.removeEventListener = function (...args) {
      window.__ISLAND_MEDIA__.removed.push(this.media);
      return remove.apply(this, args);
    };
  }
"""

# Mount a second island by fetching the same fragment the server serves, so the
# copy is the real thing -- span id, frame and all -- rather than a hand-built
# element the site would never produce.
MOUNT_SECOND = """async ({ attributes, holderId }) => {
  const response = await fetch('/fragments/constellation');
  const holder = document.createElement('div');
  if (holderId) holder.id = holderId;
  holder.innerHTML = await response.text();
  const island = holder.querySelector('ui-constellation');
  for (const [name, value] of Object.entries(attributes || {})) {
    island.setAttribute(name, value);
  }
  document.querySelector('main').appendChild(holder);
  return holder.querySelector('[data-span-id]').dataset.spanId;
}"""

# Cut the four contract colours off one island. `initial` on a custom property
# is the guaranteed-invalid value, which is exactly what a missing token looks
# like from inside the component.
BLANK_THE_TOKENS = """(holderId) => {
  const style = document.createElement('style');
  style.textContent = `#${holderId} ui-constellation {
    --color-accent: initial; --color-accent-2: initial;
    --color-border: initial; --color-muted: initial;
  }`;
  document.head.appendChild(style);
}"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_inputs() -> list[Path]:
    """Everything `build.sh` reads. `loader.js` is not here: it is hand-written
    and served as-is, so editing it needs no rebuild."""
    candidates = [
        ISLAND_CRATE / "Cargo.toml",
        ISLAND_CRATE / "Cargo.lock",
        BUILD,
        *(ISLAND_CRATE / "src").rglob("*"),
    ]
    return [path for path in candidates if path.is_file()]


def stale() -> bool:
    """Has anything the build reads moved on since the oldest thing it wrote?

    Testing a stale island proves nothing about the source in the tree, so this
    is treated the same as no island at all. Compared against the *oldest*
    output, because a half-refreshed pkg directory is not a build.
    """
    outputs = [GLUE, WASM]
    if not all(path.is_file() for path in outputs):
        return True
    newest_input = max(path.stat().st_mtime for path in build_inputs())
    oldest_output = min(path.stat().st_mtime for path in outputs)
    return newest_input > oldest_output


@pytest.fixture(scope="module")
def island() -> Path:
    """Build the island if the checked-in artefacts are missing or behind."""
    assert LOADER.is_file(), f"{LOADER} is hand-written and must be in the tree"
    if not stale():
        return ISLAND_ASSETS
    if shutil.which("wasm-pack") is None:
        pytest.skip("wasm-pack is not installed and site/assets/islands is missing or stale")
    subprocess.run(["sh", str(BUILD)], cwd=ISLAND_CRATE, check=True)
    return ISLAND_ASSETS


@pytest.fixture(scope="module")
def base_url(island: Path) -> Iterator[str]:
    """A real `cargo`-built server, in dev mode so it carries the probe.

    The binary is launched directly rather than through `cargo run`: killing
    cargo leaves the server it spawned holding the port.
    """
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    subprocess.run(["cargo", "build"], cwd=SITE, check=True)

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ | {
        "UI_SERVO_DEV": "1",
        "UI_SERVO_PORT": str(port),
        # Nothing listens here; the beacon POST is intercepted in-page. Pointing
        # it at the site's own origin keeps the URL same-origin and obvious.
        "UI_SERVO_BEACON": f"{url}/beacon",
        "UI_SERVO_TURN_ID": TURN_ID,
        "RUST_LOG": "warn",
    }
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


def open_page(browser: Browser, **context: object) -> tuple[Page, list[str]]:
    """A page that remembers every uncaught error it saw.

    An island that draws while throwing once a frame is not working; the errors
    list is asserted on rather than merely printed.
    """
    page = browser.new_page(**context)
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    failures: list[str] = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.add_init_script(CAPTURE_EVENTS)
    return page, failures


def mounted(page: Page, url: str, state: str = "ready") -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(f"ui-constellation[data-island='{state}']")


# --------------------------------------------------------------------------- #
# The island as a feature.
# --------------------------------------------------------------------------- #


def test_the_island_is_mounted_inside_a_fragment(base_url: str) -> None:
    """Server-side half of the contract: the element lives in a span-bearing
    fragment, so anything it reports has a join key."""
    with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
        markup = response.read().decode()

    assert "<ui-constellation" in markup
    fragment_start = markup.index('data-fragment="constellation"')
    section_start = markup.rindex("<section", 0, fragment_start)
    section_end = markup.index("</section>", fragment_start)
    section = markup[section_start:section_end]
    assert "data-span-id=" in section
    assert "<ui-constellation" in section
    assert '/assets/islands/loader.js"' in markup


def test_element_upgrades_and_draws(browser: Browser, base_url: str) -> None:
    """The custom element upgrades and puts pixels on the canvas.

    Non-blank is measured, not eyeballed: a canvas that was never drawn on has
    every alpha byte at zero.
    """
    page, failures = open_page(browser)
    try:
        mounted(page, f"{base_url}/")
        frame = page.evaluate(FINGERPRINT, "ui-constellation")
        assert frame is not None, "the island never created a canvas"
        assert frame["width"] > 0 and frame["height"] > 0
        # A few hundred painted pixels is nine dots and their links; zero is a
        # canvas that upgraded but never drew.
        assert frame["painted"] > 200, frame
        assert failures == [], failures
    finally:
        page.close()


def test_the_island_animates(browser: Browser, base_url: str) -> None:
    page, failures = open_page(browser)
    try:
        mounted(page, f"{base_url}/")
        before = page.evaluate(FINGERPRINT, "ui-constellation")
        page.wait_for_timeout(900)
        after = page.evaluate(FINGERPRINT, "ui-constellation")
        assert before["checksum"] != after["checksum"], (before, after)
        assert failures == [], failures
    finally:
        page.close()


def test_reduced_motion_renders_once_and_holds_still(browser: Browser, base_url: str) -> None:
    """Under `prefers-reduced-motion` the island draws one frame and schedules
    nothing -- so the same canvas is still there a second later, pointer
    movement included."""
    page, failures = open_page(browser, reduced_motion="reduce")
    try:
        mounted(page, f"{base_url}/")
        before = page.evaluate(FINGERPRINT, "ui-constellation")
        assert before["painted"] > 200, before
        page.mouse.move(300, 300)
        page.wait_for_timeout(900)
        after = page.evaluate(FINGERPRINT, "ui-constellation")
        assert before == after, (before, after)
        assert failures == [], failures
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# The island as a sensor subject.
# --------------------------------------------------------------------------- #


def test_panic_dispatches_the_wasm_panic_event(browser: Browser, base_url: str) -> None:
    """`?panic=1` reaches the probe's convention event with the Rust message
    intact.

    Asserted via a page listener installed before navigation (the documented
    alternative to routing it through the preview server shell, which is a
    different unit's process).
    """
    page, _ = open_page(browser)
    try:
        mounted(page, f"{base_url}/?panic=1", state="panicked")
        page.wait_for_function("window.__ISLAND_PANICS__.length > 0")
        detail = page.evaluate("window.__ISLAND_PANICS__[0]")

        assert "deliberate panic from the ?panic=1 test hook" in detail["message"]
        assert detail["module"] == "ui_servo_islands_bg.wasm"
        assert detail["stack"], "a panic beacon with no stack is half an answer"

        # The trap that follows the panic is the second, independent witness.
        errors = page.evaluate("window.__ISLAND_CE_ERRORS__")
        assert [error for error in errors if error["phase"] == "panic"], errors

        # Drawn before it died: the still frame is what the user is left with.
        assert page.evaluate(FINGERPRINT, "ui-constellation")["painted"] > 200
    finally:
        page.close()


def test_panic_reaches_the_beacon_attributed_to_the_fragment(
    browser: Browser, base_url: str
) -> None:
    """The whole sensor chain, including probe.js: the panic arrives at the
    beacon as a `wasm-panic` event carrying this turn's id and the span id of
    the fragment the island was mounted in."""
    page, _ = open_page(browser)
    batches: list[list[dict]] = []

    def collect(route: Route) -> None:
        body = route.request.post_data or "[]"
        batches.append(json.loads(body))
        route.fulfill(status=204, body="")

    page.route("**/beacon", collect)
    try:
        mounted(page, f"{base_url}/?panic=1", state="panicked")
        span_id = page.get_attribute("[data-fragment='constellation']", "data-span-id")
        page.wait_for_function(
            "window.__ISLAND_PANICS__.length > 0 && window.__ISLAND_CE_ERRORS__.length > 0"
        )
        # Error-class kinds flush immediately; this is transport latency, not a
        # 2 s flush interval.
        deadline = time.monotonic() + 5
        events: list[dict] = []
        while time.monotonic() < deadline:
            events = [event for batch in batches for event in batch]
            if any(event["kind"] == "wasm-panic" for event in events):
                break
            page.wait_for_timeout(100)

        panics = [event for event in events if event["kind"] == "wasm-panic"]
        assert panics, [event["kind"] for event in events]
        panic = panics[0]
        assert panic["turnId"] == TURN_ID
        assert panic["spanId"] == span_id, "the panic is not attributed to its fragment"
        assert "deliberate panic" in panic["payload"]["message"]
        assert panic["payload"]["module"] == "ui_servo_islands_bg.wasm"
    finally:
        page.unroute("**/beacon")
        page.close()
