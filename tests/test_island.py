"""Calibration for the WASM island the site actually mounts: `<resume-sandbox>`
on `/about`.

It must upgrade, it must render structured input, it must refuse malformed input
*legibly*, and when it cannot load at all it must say so somewhere a machine can
read.

The first three are the island as a feature. The fourth is the island as a
sensor subject, and it is the reason this file drives a real browser instead of
asserting on markup: nothing about a custom element's failure path is visible in
the HTML the server sent. Only the browser can prove that the loader's report is
wired to the probe, and that the probe attributes it to the page the element was
on.

The chain under test, end to end:

    /about -> loader.js -> <resume-sandbox> connectedCallback
           -> mount_resume_sandbox() -> editor, preview, violations
           -> (on a failed module fetch) ui-servo:ce-error CustomEvent
              (bubbling, from the element)
           -> probe.js document listener -> beacon POST, attributed to the
              page wrapper's data-span-id

Both halves of the last hop are asserted. The CustomEvent itself is checked with
a page-level listener installed before navigation, and the beacon is checked by
intercepting the POST with ``page.route`` -- the preview server is a separate
unit, so this file stands the beacon endpoint up itself rather than depending on
one being run alongside.

The failure is provoked by aborting the module request rather than by a test
hook in the crate: `?panic=1` is `<ui-constellation>`'s, and no page mounts a
constellation any more. A refused fetch is a real thing that happens to a lazily
loaded island, and it needs nothing in the shipped code to exist for it.

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

# The page that mounts the island, and the one that does not. `/` carrying the
# loader and no island at all is half the contract: the script is on every page
# so that an island swapped in later still works, and paying for it on a page
# with nothing to upgrade is the deliberate cost.
ABOUT = "/about"
HOME = "/"

# The island builds its own furniture and these are the hooks it builds it
# against -- the same selectors `mount` resolves, which the crate pins in
# `the_skeleton_carries_every_hook_mount_looks_for`. They are collected here so
# that a rename on that side is one edit on this one.
HOST = "resume-sandbox"
EDITOR = f"{HOST} textarea"
PREVIEW = f"{HOST} [data-resume-preview]"
VIOLATIONS = f"{HOST} [data-validation-output]"
TITLE = f"{HOST} [data-validation-title]"
PILL = f"{HOST} [data-render-state]"

# Installed before navigation so the listeners beat the island's own upgrade.
#
# Only the two convention events are captured. The counters the old file kept
# (ResizeObserver, MediaQueryList) belonged to the constellation's redraw
# lifecycle; this island has neither, and bookkeeping for something nothing
# tests is bookkeeping that rots.
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
"""

# An empty object: every one of the eight required fields is missing at once,
# which is the case that proves the panel lists *all* the violations rather than
# stopping at the first.
EMPTY_DOCUMENT = "{}"
REQUIRED_FIELDS = [
    "name",
    "headline",
    "contact",
    "summary",
    "skills",
    "experience",
    "projects",
    "education",
]


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


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()


def open_page(browser: Browser, **context: object) -> tuple[Page, list[str]]:
    """A page that remembers every uncaught error it saw.

    An island that renders while throwing on every keystroke is not working; the
    errors list is asserted on rather than merely printed.
    """
    page = browser.new_page(**context)
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    failures: list[str] = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    page.add_init_script(CAPTURE_EVENTS)
    return page, failures


def mounted(page: Page, base_url: str, state: str = "ready") -> None:
    """Open `/about` and wait for the sandbox to reach `state`.

    `data-island` is written by `loader.js` at each step of the lifecycle, so
    waiting on it is waiting on the wasm fetch, the mount, and the first
    evaluation -- not on a timeout.

    `attached`, not the default `visible`: a sandbox that failed to load has a
    one-line fallback in it and a sandbox mid-teardown has nothing, and neither
    state is one this helper should refuse to observe.
    """
    page.goto(f"{base_url}{ABOUT}", wait_until="domcontentloaded")
    page.wait_for_selector(f"{HOST}[data-island='{state}']", state="attached")


def panel(page: Page) -> dict[str, str]:
    """Everything the island currently says about the editor's contents.

    Read as one snapshot rather than one assertion at a time: the four surfaces
    are written by a single `update()`, and the failure worth catching is two of
    them disagreeing.
    """
    return {
        "title": page.text_content(TITLE) or "",
        "pill": page.text_content(PILL) or "",
        "violations": page.text_content(VIOLATIONS) or "",
        "preview": page.text_content(PREVIEW) or "",
    }


def edit(page: Page, source: str, pill: str) -> dict[str, str]:
    """Put `source` in the editor and read the panel back, once it says `pill`.

    `fill` dispatches a real `input` event and the island updates *inside* that
    dispatch, so the panel has already settled by the time this returns. The
    wait is therefore an assertion rather than a synchronisation: it is where a
    panel that settled on the wrong state fails, with the state it settled on in
    the message, instead of four confusing assertions later.
    """
    page.fill(EDITOR, source)
    page.wait_for_selector(f"{PILL}:text-is('{pill}')")
    return panel(page)


def sideways(page: Page) -> int:
    """How far the *document* can be scrolled horizontally, in px.

    The document, not the element. An element wider than the viewport does not
    merely overflow itself: unless something contains it, it widens the whole
    page and every paragraph on it starts one swipe to the left.
    """
    return page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")


def scrolling_descendants(page: Page) -> list[str]:
    """Every element that can be scrolled horizontally *itself*, by selector.

    The complement of :func:`sideways`, and the reason both exist: containing
    an overlong line with `overflow-x: auto` keeps the document narrow -- the
    first version of the CSS fix did exactly that and passed `sideways` -- but
    a locally scrolling element is what the probe's overflow sensor files as
    an offender, so the page was certified here and flagged there. The site's
    answer is wrapping; nothing on the page should scroll sideways, at any
    level.
    """
    return page.evaluate(
        """() => [...document.querySelectorAll('body *')]
            .filter((n) => n.scrollWidth > n.clientWidth)
            .map((n) => n.tagName.toLowerCase() + (n.className ? '.' + n.className : ''))"""
    )


# --------------------------------------------------------------------------- #
# The island as a feature.
# --------------------------------------------------------------------------- #


def test_the_sandbox_is_mounted_inside_the_pages_span(base_url: str) -> None:
    """Server-side half of the contract: the element is on `/about`, closed, and
    inside the wrapper that carries the page's join key, so anything it reports
    has somewhere to be attributed.

    Enclosure, not mere order. The probe resolves an element-targeted reading by
    walking up to the nearest `[data-span-id]`; a sandbox that rendered *after*
    the wrapper closed would look identical in a `contains` check and beacon
    into the probe's fallback.
    """
    markup = fetch(f"{base_url}{ABOUT}")

    assert markup.count("<resume-sandbox") == 1, "more than one sandbox on the page"
    span = markup.index('data-span-id="')
    sandbox = markup.index("<resume-sandbox")
    closes = markup.index("</resume-sandbox>")
    wrapper_closes = markup.rindex("</div>")

    # Not empty: what is inside is the fallback a visitor keeps if the module
    # never arrives, and `mount` replaces it only on a load that worked.
    assert "<p" in markup[sandbox:closes], "the sandbox ships no fallback"
    assert span < sandbox < wrapper_closes, "the sandbox is outside the page's span wrapper"

    # It is also under its own heading rather than loose at the end of the page:
    # an element nothing introduces is an element nobody uses.
    heading = markup.index("Résumé, as data")
    assert heading < sandbox < markup.index("</section>", sandbox)

    # Inert markup until the loader defines the tag.
    assert '/assets/islands/loader.js"' in markup


def test_the_loader_stops_at_the_classic_shells_edge(base_url: str) -> None:
    """`/` pays no island bytes; `/about` carries the loader and pre-mounts
    only the sandbox.

    The old rationale -- loader unconditional in the shell so htmx can swap an
    island into any page -- ended with the golden-path port (owner,
    2026-08-09): `/` and the deep-dives wear the portfolio shell, which ships
    neither htmx nor islands, so a loader there would be dead bytes on the
    heaviest pages. This is the assertion that keeps the boundary honest in
    both directions.
    """
    home = fetch(f"{base_url}{HOME}")
    assert '/assets/islands/loader.js"' not in home
    assert "<resume-sandbox" not in home
    assert "<ui-constellation" not in home
    about = fetch(f"{base_url}{ABOUT}")
    assert '/assets/islands/loader.js"' in about
    assert "<ui-constellation" not in about


def test_the_element_upgrades_and_opens_on_the_sample(browser: Browser, base_url: str) -> None:
    """The custom element upgrades, fetches its wasm, and fills itself in.

    The opening state is a worked example, not a blank box: the editor holds
    parseable JSON and the panel already agrees with it. Both halves are checked
    -- an editor pre-filled with text the panel calls invalid would be a worse
    first impression than an empty one.
    """
    page, failures = open_page(browser)
    try:
        mounted(page, base_url)
        opening = panel(page)
        assert opening["pill"] == "rendered", opening
        assert opening["title"] == "Valid resume data", opening
        assert "Kennedy Mosoti" in opening["preview"], opening

        source = page.input_value(EDITOR)
        assert json.loads(source)["name"] == "Kennedy Mosoti"

        # The server-rendered fallback is gone, replaced by the editor it was
        # standing in for. A page showing both would be telling a visitor the
        # thing in front of them does not work.
        assert "needs JavaScript" not in page.inner_text(HOST)
        assert failures == [], failures
    finally:
        page.close()


def test_invalid_json_lists_every_violation(browser: Browser, base_url: str) -> None:
    """Malformed input fails legibly, and the two ways it can be malformed are
    told apart.

    A structural failure reports one line per problem -- all eight at once for
    an empty object, because a person fixing a paste-in wants the whole list.
    A syntax failure reports the parser's own complaint under a *different*
    heading: telling someone their braces are wrong when their braces are fine
    is the one unhelpful thing a validator can do.
    """
    page, failures = open_page(browser)
    try:
        mounted(page, base_url)

        structural = edit(page, EMPTY_DOCUMENT, "blocked")
        assert structural["title"] == "Invalid structure", structural
        for field in REQUIRED_FIELDS:
            assert f"Missing required field: {field}" in structural["violations"], field
        assert "Fix validation errors before rendering." in structural["preview"], structural

        syntax = edit(page, "{ oops", "blocked")
        assert syntax["title"] == "Invalid JSON", syntax
        assert syntax["violations"], "a syntax failure with no message is half an answer"
        assert "Fix JSON syntax before rendering." in syntax["preview"], syntax

        assert failures == [], failures
    finally:
        page.close()


def test_valid_json_renders_the_preview(browser: Browser, base_url: str) -> None:
    """An edit that keeps the document valid re-renders the preview from it.

    The edit is made by parsing what the editor already holds and putting it
    back, so the test proves the round trip -- editor to JSON to preview -- and
    not merely that some fixed string this file happens to carry is accepted.
    """
    page, failures = open_page(browser)
    try:
        mounted(page, base_url)
        resume = json.loads(page.input_value(EDITOR))
        resume["name"] = "Ada Lovelace"
        resume["skills"] = ["Analytical Engine"]

        rendered = edit(page, json.dumps(resume, indent=2), "rendered")
        assert rendered["title"] == "Valid resume data", rendered
        assert "Ada Lovelace" in rendered["preview"], rendered
        assert "Analytical Engine" in rendered["preview"], rendered
        assert "Kennedy Mosoti" not in rendered["preview"], "the preview is stale"
        assert rendered["violations"].startswith("Schema checks passed."), rendered
        assert failures == [], failures
    finally:
        page.close()


def test_the_preview_escapes_what_the_editor_hands_it(browser: Browser, base_url: str) -> None:
    """The preview pane is filled with `innerHTML`, and everything that reaches
    it comes from a `maud` template -- so a `<script>` in the editor arrives as
    text.

    The crate asserts this about the string it builds. This asserts it about the
    DOM the browser built from that string, which is the only place the claim
    can actually be wrong.
    """
    page, failures = open_page(browser)
    try:
        mounted(page, base_url)
        resume = json.loads(page.input_value(EDITOR))
        # Two payloads, because they fail differently. `innerHTML` never runs a
        # `<script>` it builds, so a script that survived as an *element* would
        # be a hole this test could not observe by waiting for it to fire --
        # the element count is what catches that one. An `onerror` attribute on
        # a broken image is the shape that does fire, immediately, and it is
        # the one the global witnesses.
        resume["name"] = "<script>window.__PWNED__ = 'script';</script>"
        resume["headline"] = "<img src=x onerror=\"window.__PWNED__ = 'img'\">"

        rendered = edit(page, json.dumps(resume, indent=2), "rendered")
        assert "window.__PWNED__" in rendered["preview"], "the payload was dropped, not escaped"
        assert page.locator(f"{PREVIEW} script").count() == 0, "a script element was built"
        assert page.locator(f"{PREVIEW} img").count() == 0, "an img element was built"
        assert page.evaluate("window.__PWNED__ === undefined"), "the payload ran"
        assert failures == [], failures
    finally:
        page.close()


def test_the_page_does_not_scroll_sideways_with_the_island_on_it(
    browser: Browser, base_url: str
) -> None:
    """`/about` fits a phone, with the island mounted and in every state it has.

    The island's furniture is sized in *characters* -- a `<textarea cols="72">`
    is about 600px wide however wide the screen is -- and its validation output
    is a `<pre>`, whose lines are as long as they are. Either one, unconstrained,
    makes the whole document scroll sideways on a phone rather than merely
    overflowing itself.

    Checked in all three states, because they are three different widths: the
    sample, a blocked document whose violation lines are the longest text on the
    page, and the syntax error whose one line is `serde_json`'s.
    """
    page, failures = open_page(browser, viewport={"width": 375, "height": 800})
    try:
        mounted(page, base_url)
        assert sideways(page) == 0, "the sample state overflows the viewport"
        assert scrolling_descendants(page) == [], "the sample state scrolls locally"

        edit(page, EMPTY_DOCUMENT, "blocked")
        assert sideways(page) == 0, "the violation list overflows the viewport"
        assert scrolling_descendants(page) == [], "the violation list scrolls locally"

        edit(page, "{ oops", "blocked")
        assert sideways(page) == 0, "the syntax complaint overflows the viewport"
        assert scrolling_descendants(page) == [], "the syntax complaint scrolls locally"

        assert failures == [], failures
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# The island as a sensor subject.
# --------------------------------------------------------------------------- #


def test_a_refused_module_reaches_the_beacon_attributed_to_the_page(
    browser: Browser, base_url: str
) -> None:
    """The whole sensor chain, including probe.js.

    The wasm is fetched lazily, which means it can fail to arrive -- an aborted
    request here, a cache miss against a bad deploy in production. The element
    is then a tag with nothing behind it, and the only thing that says so is the
    report the loader dispatches. It has to arrive at the beacon as a `ce-error`
    carrying this turn's id, the `wasm-init` phase, and the span id of the
    wrapper the element sits in.

    Both witnesses are checked: the CustomEvent itself, via a listener installed
    before navigation, and the beacon POST, via `page.route`.
    """
    page, failures = open_page(browser)
    batches: list[list[dict]] = []

    def collect(route: Route) -> None:
        body = route.request.post_data or "[]"
        batches.append(json.loads(body))
        route.fulfill(status=204, body="")

    page.route("**/beacon", collect)
    # The glue module, not the `.wasm` it goes on to fetch: refusing the import
    # is the failure the loader has a phase for, and it needs no wasm-side code
    # to be reachable.
    page.route("**/ui_servo_islands.js", lambda route: route.abort())
    try:
        mounted(page, base_url, state="error")
        span_id = page.get_attribute("[data-span-id]", "data-span-id")
        page.wait_for_function("window.__ISLAND_CE_ERRORS__.length > 0")

        detail = page.evaluate("window.__ISLAND_CE_ERRORS__[0]")
        assert detail["phase"] == "wasm-init", detail
        # Named as the sandbox, not as the constellation: two elements share
        # this loader, and a report labelled with the wrong one sends whoever
        # reads the beacon to the wrong file.
        assert detail["tag"] == "resume-sandbox", detail
        assert detail["message"], "a report with no message names nothing"

        # Error-class kinds flush immediately; this is transport latency, not a
        # 2 s flush interval.
        deadline = time.monotonic() + 5
        events: list[dict] = []
        while time.monotonic() < deadline:
            events = [event for batch in batches for event in batch]
            if any(event["kind"] == "ce-error" for event in events):
                break
            page.wait_for_timeout(100)

        reports = [event for event in events if event["kind"] == "ce-error"]
        assert reports, [event["kind"] for event in events]
        report = reports[0]
        assert report["turnId"] == TURN_ID
        assert report["spanId"] == span_id, "the failure is not attributed to the page"
        assert report["payload"]["phase"] == "wasm-init", report
        assert report["payload"]["message"], report

        # Two fields, not four, and that is under-reporting rather than a
        # contract. `probe/README.md` documents this kind as carrying `message`,
        # `phase`, `tag` and `stack`; the loader dispatches all four (asserted on
        # `detail` above) and `probe.js`'s convention listener forwards only the
        # first two, so the component name and the stack die on this hop. That
        # is the probe's bug, in a unit this file does not own, and it is filed
        # rather than pinned: asserting the payload is *exactly* two fields would
        # make this test fail on the fix, which is the one thing a test about a
        # known defect must never do.


        # The element says so on itself too, which is what a human debugging in
        # devtools sees before they ever look at a beacon.
        assert page.get_attribute(HOST, "data-island") == "error"
        # And the visitor is left with the fallback rather than an empty box
        # under a paragraph promising an editor.
        assert "needs JavaScript" in page.inner_text(HOST)

        # A refused module is a *handled* failure, unlike the deliberate panic
        # this test replaced: the loader reports it and swallows it, precisely
        # so one dead island does not take a swap with it. Nothing may reach
        # window.onerror, or the page is reporting the same thing twice and the
        # second copy has no span on it.
        assert failures == [], failures
    finally:
        page.unroute("**/ui_servo_islands.js")
        page.unroute("**/beacon")
        page.close()
