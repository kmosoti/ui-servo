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
HERO = REPO_ROOT / "site/assets/fragments/hero.html"

pytestmark = pytest.mark.skipif(
    not BINARY.is_file() or not HERO.is_file(),
    reason="needs `cargo build` in site/ and a promoted hero",
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


class TestTamperingIsRefused:
    """Each of these is a row of the promotion table in `demo/README.md`."""

    def test_an_edited_pick_is_a_500_and_never_renders(self, hero_file: Path) -> None:
        hero_file.write_text(hero_file.read_text() + "<p>smuggled</p>\n")
        with _server(dev=True) as (_, port):
            fragment_status, _ = _get(port, "/fragments/promoted/hero")
            home_status, home = _get(port, "/")
        assert fragment_status == 500
        # The page fails rather than falling back: "nobody picked yet" and
        # "someone edited the pick" must not look the same to a visitor.
        assert home_status == 500
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
