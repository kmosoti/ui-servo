"""Calibration for the service worker the exported site ships at `/sw.js`.

Two claims, and neither one can be asserted anywhere but in a browser.

**The site opens with the network switched off.** Nothing about the markup says
whether a worker installed, precached seven pages, and answered a navigation
from that cache — only a browser that has actually lost the network can say so.

**A new build discards the old cache.** The cache is named after a fingerprint
of everything in `dist/`, so re-exporting produces a different name (span ids
differ per render, so the fingerprint changes by construction). The property
worth pinning is not that the new cache appears — it is that the old one is
*gone*: origin storage is a quota, and a site that leaked a cache per deploy
would eventually have the browser evict the whole origin rather than the stale
half.

The subject is the *export*, not the dev server: `sw-register.js` is gated on
`!dev`, the served worker is stamped `dev` and sent `no-store`, and the whole
point of both is that the development loop never measures a cached page. So
this file builds the static site with `cargo run --bin export`, serves it with
`python -m http.server` — a plain file host, which is what mosoti.dev is — and
drives that. Localhost counts as a secure context, so a worker registers over
plain HTTP without a certificate.

Requires a Rust toolchain and Chromium. Both are skipped rather than failed when
absent: a missing toolchain is not evidence of a broken worker.
"""

import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

pytestmark = pytest.mark.playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"

# The coordinator's port for this unit. Fixed rather than ephemeral because a
# service worker's registration is keyed by origin, and a port that moved
# between runs would leave a registration behind for an origin nothing serves.
PORT = 8102
BASE_URL = f"http://127.0.0.1:{PORT}"

BOOT_TIMEOUT_S = 30.0
ACTION_TIMEOUT_MS = 15_000
EXPORT_TIMEOUT_S = 600.0

# The hero, server-rendered into the home page. Present offline means the
# document came from the cache and not from a browser error page.
HERO = "h1:text-is('Kennedy Mosoti')"


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def export_site(dist: Path) -> None:
    """Build the static site into `dist`, failing loudly.

    Through `cargo run --bin export` rather than by calling the binary directly:
    the exporter is a build step that runs to completion and exits, so there is
    no process left for cargo to hold on to, and going through cargo is what
    guarantees the export reflects the source in the tree.
    """
    subprocess.run(
        ["cargo", "run", "--quiet", "--bin", "export", "--", str(dist)],
        cwd=SITE,
        check=True,
        timeout=EXPORT_TIMEOUT_S,
    )


def cache_names(page: Page) -> list[str]:
    """The origin's cache names, as the page sees them."""
    return page.evaluate("() => caches.keys()")


def ours(names: list[str]) -> list[str]:
    return sorted(name for name in names if name.startswith("ui-servo-"))


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly exported site, in a directory the exporter is allowed to own."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    directory = tmp_path_factory.mktemp("service-worker-dist")
    export_site(directory)
    assert (directory / "sw.js").is_file(), "the export wrote no service worker"
    return directory


class FileServer:
    """`python -m http.server` over the export: a file host, like the real one.

    Stoppable on purpose. `context.set_offline(True)` emulates the *page's*
    network, and whether that emulation reaches a service worker — which runs in
    its own target — is a Playwright implementation detail this file must not
    depend on: if it does not, the worker's own `fetch` still succeeds, the
    reload is answered from the network, and the offline test passes while
    proving nothing. Killing the server is not emulation. Nothing can answer.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process is not None:
            return
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(PORT),
                "--directory",
                str(self.directory),
                "--bind",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"the file server exited early with {self.process.returncode}")
            try:
                with urllib.request.urlopen(f"{BASE_URL}/", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        raise RuntimeError(f"the file server never answered on {BASE_URL}")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process = None
        # The socket has to be gone, not merely closing: the next start() binds
        # the same fixed port, and a lingering listener would make it exit early
        # with an error this file would report as "never answered".
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not port_is_free(PORT):
            time.sleep(0.05)


@pytest.fixture(scope="module")
def server(dist: Path) -> Iterator[FileServer]:
    if not port_is_free(PORT):
        pytest.skip(f"port {PORT} is already in use")
    running = FileServer(dist)
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture()
def base_url(server: FileServer) -> Iterator[str]:
    """The origin, with the guarantee that it is answering when a test starts.

    Function-scoped over the module-scoped server so that a test which stops the
    host cannot leave the next one talking to nothing.
    """
    server.start()
    yield BASE_URL


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Exception as error:  # noqa: BLE001  # pragma: no cover - environment, not logic
            pytest.skip(f"chromium is not available: {error}")
        try:
            yield instance
        finally:
            instance.close()


def registered(page: Page, base_url: str) -> None:
    """Open the home page and wait for a worker to be controlling the origin.

    `serviceWorker.ready` resolves on an *active* registration, which is one
    step past installed: the precache has been filled and `clients.claim` has
    run. Waiting on it is waiting on the install, not on a timeout.
    """
    page.goto(f"{base_url}/", wait_until="load")
    page.wait_for_function("() => navigator.serviceWorker.ready.then(() => true)")


def test_the_exported_site_opens_with_the_network_switched_off(
    browser: Browser, base_url: str, server: FileServer
) -> None:
    """A visitor who has been here once can come back with no network at all.

    The reload is the assertion. A page kept alive in memory proves nothing —
    the document has to be built again, from a cache, by a worker that answered
    a navigation the network refused.

    The network is removed twice over, and deliberately: the host is killed, so
    there is nothing to answer even a request the emulation misses, *and* the
    context is set offline, so the page does not merely fail differently.
    """
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    try:
        registered(page, base_url)
        assert ours(cache_names(page)), "the worker activated without filling a cache"
        assert page.evaluate("() => navigator.serviceWorker.controller !== null"), (
            "the worker activated without claiming the page it installed from"
        )

        server.stop()
        context.set_offline(True)

        page.reload(wait_until="load")
        assert page.locator(HERO).count() == 1, "the offline page is not the site"
        assert page.text_content(HERO) == "Kennedy Mosoti"

        # And the stylesheet came back too, or the "page" is unstyled text.
        assert page.evaluate(
            "() => fetch('/assets/portfolio.css').then((r) => r.ok).catch(() => false)"
        ), "the precached stylesheet is not being served offline"

        # A page the precache lists but the visitor never opened. It is stored
        # under the no-trailing-slash key while the host redirects to the slash
        # form, so this is where a cached-but-unreachable page shows up.
        page.goto(f"{base_url}/about/", wait_until="load")
        assert "Kennedy Mosoti" in (page.text_content("body") or ""), (
            "/about/ is precached but cannot be served offline"
        )
    finally:
        context.set_offline(False)
        context.close()
        server.start()


def test_a_new_build_leaves_exactly_one_cache_behind(
    browser: Browser, base_url: str, dist: Path
) -> None:
    """Re-exporting evicts the previous generation rather than joining it.

    The same directory is exported twice on purpose. Span ids are regenerated
    per render, so the second build's files differ from the first's, so the
    fingerprint differs, so the cache name differs — no version to bump and
    nothing to remember. What is asserted is the *count*: one `ui-servo-*` name,
    and it is the new one.

    `skipWaiting` plus `clients.claim` is why this is prompt rather than a wait
    for every tab to close, but "prompt" is not "synchronous": the new worker
    installs, activates and evicts across several turns of the event loop, so
    the assertion polls.
    """
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    try:
        registered(page, base_url)
        before = ours(cache_names(page))
        assert len(before) == 1, before

        export_site(dist)
        page.reload(wait_until="load")
        page.wait_for_function("() => navigator.serviceWorker.ready.then(() => true)")

        deadline = time.monotonic() + 15
        after: list[str] = before
        while time.monotonic() < deadline:
            after = ours(cache_names(page))
            if len(after) == 1 and after != before:
                break
            page.wait_for_timeout(200)

        assert len(after) == 1, f"caches accumulated across builds: {after}"
        assert after != before, "the new build reused the old build's cache name"
    finally:
        context.close()
