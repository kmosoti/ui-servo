"""The promotion contract, checked against the real server instead of a fake.

Everything else about promotion is tested against doubles: a fake sanitiser in
``test_promotion.py``, a temp directory in ``promoted.rs``. Those are fast and
they pin the logic, but they share a blind spot — both halves can agree with each
other while disagreeing with the binary that actually serves the site. The digest
is computed in Python and verified in Rust, the refusal is a Python exception on
one side and an HTTP status on the other, and nothing in the unit tests would
notice if the two implementations drifted a newline apart.

So this file launches ``ui-servo-site`` and makes real requests. It is the test
that would have caught a broken interop, and it is what lets `demo/README.md`
publish its promotion table as fact: every row below is one line of that table.

Skipped, not failed, when the binary has not been built — a Python-only checkout
is a legitimate state, and a red suite there would train people to ignore it.
"""

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY = REPO_ROOT / "site/target/debug/ui-servo-site"
HERO = REPO_ROOT / "site/promoted/hero.html"

pytestmark = pytest.mark.skipif(
    not BINARY.is_file() or not HERO.is_file(),
    reason="needs `cargo build` in site/ and a promoted hero",
)


def _stale_sources() -> list[Path]:
    """Rust sources newer than the binary under test.

    A skip is honest when the binary was never built. It is a lie when the binary
    exists but predates the code — the suite then reports green for an
    interoperability contract it checked against a previous version of one side,
    which is worse than not running at all. Cheap to detect, so detect it.
    """
    if not BINARY.is_file():
        return []
    built = BINARY.stat().st_mtime
    sources = [*(REPO_ROOT / "site/src").rglob("*.rs"), REPO_ROOT / "site/Cargo.toml"]
    return sorted(path for path in sources if path.is_file() and path.stat().st_mtime > built)


def test_the_binary_under_test_is_not_stale() -> None:
    """Fail loudly rather than certify the wrong build."""
    stale = _stale_sources()
    assert not stale, (
        "site/target/debug/ui-servo-site is older than "
        + ", ".join(str(path.relative_to(REPO_ROOT)) for path in stale[:5])
        + ". Run `cargo build` in site/; these tests would otherwise pass against "
        "a binary that does not contain the code being reviewed."
    )


def test_the_binary_contains_the_format_it_is_being_tested_against() -> None:
    """A timestamp is evidence about a file, not about its contents.

    `touch` on an obsolete binary satisfies the staleness check above while the
    code inside it is any age at all. This looks for the one string that must
    move whenever the cross-language format moves — the digest version tag — so
    a binary built before the current format cannot quietly certify it.

    Not a substitute for building; a floor under the mtime heuristic.
    """
    from ui_servo.control.promote import DIGEST_VERSION

    blob = BINARY.read_bytes()
    assert DIGEST_VERSION.encode() in blob, (
        f"the binary does not contain {DIGEST_VERSION!r}, so it was built against a "
        "different provenance format than this test suite. Run `cargo build` in site/."
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _server(*, dev: bool) -> Iterator[tuple[subprocess.Popen[str], int]]:
    """Start the site, or hand back the process that refused to start."""
    port = _free_port()
    env = {**os.environ, "UI_SERVO_PORT": str(port)}
    env.pop("UI_SERVO_DEV", None)
    if dev:
        env["UI_SERVO_DEV"] = "1"
    process = subprocess.Popen(
        [str(BINARY)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        # Poll rather than sleep a fixed amount: a refusal is instant and a
        # successful boot is not, so waiting a flat interval makes the
        # refusal tests slow and the success tests flaky.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                break
            except urllib.error.HTTPError:
                break  # listening; the status is the test's business
            except OSError:
                time.sleep(0.05)
        yield process, port
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def _get(port: int, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@pytest.fixture
def hero_file() -> Iterator[Path]:
    """Let a test corrupt the committed hero, and always put it back."""
    original = HERO.read_text()
    try:
        yield HERO
    finally:
        HERO.write_text(original)
        assert HERO.read_text() == original


class TestTheGatedPathServes:
    def test_a_promoted_hero_is_served_with_a_fresh_span_id(self) -> None:
        with _server(dev=True) as (_, port):
            status, body = _get(port, "/fragments/promoted/hero")
        assert status == 200
        assert "data-span-id" in body, "the server did not stamp a join key"
        assert body.count("data-span-id") == 1, (
            "more than one span id: the candidate's own id survived promotion and live "
            "readings would be filed under a variant that no longer exists"
        )

    def test_release_mode_boots_and_serves_the_committed_pick(self) -> None:
        with _server(dev=False) as (process, port):
            assert process.poll() is None, (process.stderr.read() if process.stderr else "")
            status, body = _get(port, "/")
        assert status == 200
        assert "Kennedy Mosoti" in body


class TestTheStaticRootCannotServeAPick:
    """A promoted file must be unreachable except through the verifying route.

    This started as deny routes on `/assets/fragments/{*rest}` while the files
    still lived inside the static root. It did not hold: axum matches the raw
    path and `ServeDir` percent-decodes, so `/assets/%66ragments/hero.html`
    missed the deny route, decoded back to the real file, and returned the raw
    body — no provenance check, no hash check, no frame. Confirmed against the
    running binary, which is why the fix was to move the directory out of the
    static root rather than to add another encoding to the denylist.

    A denylist over an attacker-controlled encoding is a losing position. These
    cases exist to notice if anyone re-enters it.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/assets/promoted/hero.html",
            "/assets/%70romoted/hero.html",  # 'p' percent-encoded
            "/assets/promoted%2Fhero.html",  # separator percent-encoded
            "/assets/../promoted/hero.html",
            "/assets/%2e%2e/promoted/hero.html",
            "/assets/..%2fpromoted%2fhero.html",
            "/promoted/hero.html",
            "/assets/fragments/hero.html",  # the old location
            "/assets/%66ragments/hero.html",  # the exploit that worked
        ],
    )
    def test_no_encoding_reaches_the_raw_file(self, path: str) -> None:
        with _server(dev=False) as (_, port):
            status, body = _get(port, path)
        assert "ui-servo: gated" not in body, (
            f"{path} served the raw promoted file, provenance comment and all"
        )
        assert status != 200, f"{path} returned 200"

    def test_the_verifying_route_still_works(self) -> None:
        """The point is that one door is open, not that all of them are shut."""
        with _server(dev=False) as (_, port):
            status, body = _get(port, "/fragments/promoted/hero")
        assert status == 200
        # Framed and stamped, not the file's own bytes.
        assert "ui-servo: gated" not in body
        assert "data-fragment=\"promoted-hero\"" in body


class TestTheTwoSidesAgreeOnAGrammar:
    """Every round id the Python writer accepts, the Rust server must read.

    This contract has drifted twice, in opposite directions, and both times the
    symptom was silent. The reader terminated a value at `-`, so `round-4` was
    read as `round` — a promotion that served fine while misreporting its own
    origin. Removing `-` from the terminator set then broke ids *ending* in one,
    because the value swallowed the closing `-->`.

    The fix was to stop scanning for terminators and parse the line by its three
    known literals. This test is the reason to keep it that way: it exercises the
    writer's whole documented grammar (`[A-Za-z0-9._-]{1,64}`) against the real
    binary, so any future disagreement fails here instead of in a deploy.
    """

    @pytest.mark.parametrize(
        "round_id",
        ["4", "round-4", "4.1", "a_b", "R-2026.08.07", "1-2-3", "--", "a--b", "x" * 64],
    )
    def test_the_server_reads_back_every_round_id_the_writer_permits(
        self, round_id: str, hero_file: Path
    ) -> None:
        from ui_servo.control.promote import _SAFE_TOKEN, promote
        from ui_servo.ports.sanitizer import SanitizeResult

        class _Accepting:
            def check(self, fragment_html: str) -> SanitizeResult:
                return SanitizeResult(accepted=True, cleaned_html=fragment_html, violations=())

        assert _SAFE_TOKEN.match(round_id), "the writer would reject this; fix the case"
        body = hero_file.read_text().partition("\n")[2]
        promote(
            body,
            part="hero",
            round_id=round_id,
            sanitizer=_Accepting(),
            fragments_dir=hero_file.parent,
        )

        with _server(dev=False) as (process, port):
            assert process.poll() is None, (
                f"the server refused to start on round_id={round_id!r}: "
                + (process.stderr.read() if process.stderr else "")
            )
            status, served = _get(port, "/fragments/promoted/hero")
        assert status == 200
        # The label echoes what the reader parsed, so a truncated round shows up.
        assert f"round {round_id})" in served, served[:200]


class TestTamperingIsRefused:
    """Each of these is a row of the promotion table in `demo/README.md`."""

    def test_an_edited_pick_is_a_500_and_never_renders(self, hero_file: Path) -> None:
        hero_file.write_text(hero_file.read_text() + "<p>smuggled</p>\n")
        with _server(dev=True) as (_, port):
            fragment_status, _ = _get(port, "/fragments/promoted/hero")
            home_status, home = _get(port, "/")
        assert fragment_status == 500
        # Since the golden-path port (2026-08-09) the home page no longer
        # mounts promoted fragments, so a tampered pick cannot poison `/` at
        # all: the page stays healthy and the smuggled bytes never render.
        # The endpoint itself is still the wall — asserted above.
        assert home_status == 200
        assert "smuggled" not in home

    def test_a_fragment_with_no_provenance_is_refused(self, hero_file: Path) -> None:
        hero_file.write_text('<section class="my-lg">never gated</section>\n')
        with _server(dev=True) as (_, port):
            status, _ = _get(port, "/fragments/promoted/hero")
        assert status == 500

    def test_release_mode_refuses_to_start_at_all(self, hero_file: Path) -> None:
        """The deploy fails, rather than the first visitor."""
        hero_file.write_text(hero_file.read_text() + "<p>smuggled</p>\n")
        with _server(dev=False) as (process, _):
            assert process.wait(timeout=15) != 0
            stderr = process.stderr.read() if process.stderr else ""
        assert "refused at startup" in stderr, stderr
        assert "edited after promotion" in stderr, stderr
