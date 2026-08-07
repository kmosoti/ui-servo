"""Promotion: a pick only becomes the site if it can prove it was gated.

The Rust half of this contract is tested in ``site/src/fragments/promoted.rs``;
these are the Python half, plus one test that pins the *interop* — the digest
this module writes has to be the digest the server recomputes, or a promotion
that is perfectly valid fails to serve.
"""

from hashlib import sha256
from pathlib import Path

import pytest

from ui_servo.control.promote import (
    PromotionRefused,
    body_digest,
    promote,
    render_promoted_file,
)
from ui_servo.domain.contract import DirectionContract
from ui_servo.ports.sanitizer import SanitizeResult, SanitizerPort, Violation, ViolationKind

CONTRACT = Path(__file__).resolve().parents[1] / "direction" / "direction.toml"
GOOD = '<section data-span-id="hero-v2" class="my-3xl"><h1 class="type-display">Hi</h1></section>'


class _Accepting:
    """Sanitiser that passes everything, so the test is about promotion."""

    def check(self, fragment_html: str) -> SanitizeResult:
        return SanitizeResult(accepted=True, cleaned_html=fragment_html, violations=())


class _Refusing:
    def check(self, fragment_html: str) -> SanitizeResult:
        return SanitizeResult(
            accepted=False,
            cleaned_html=None,
            violations=(
                Violation(
                    kind=ViolationKind.UNKNOWN_CLASS,
                    locator="section.hero-shout",
                    detail="unknown class 'hero-shout'",
                ),
            ),
        )


def test_ports_are_satisfied_by_the_doubles() -> None:
    assert isinstance(_Accepting(), SanitizerPort)
    assert isinstance(_Refusing(), SanitizerPort)


class TestPromotion:
    def test_a_gated_pick_is_written_with_its_provenance(self, tmp_path: Path) -> None:
        promotion = promote(
            GOOD, part="hero", round_id="1", sanitizer=_Accepting(), fragments_dir=tmp_path
        )
        written = promotion.path.read_text()
        assert written.startswith("<!-- ui-servo: gated round=1 sha256=")
        assert GOOD in written
        assert promotion.digest == sha256(GOOD.strip().encode()).hexdigest()

    def test_a_pick_that_fails_the_gate_is_not_written_at_all(self, tmp_path: Path) -> None:
        with pytest.raises(PromotionRefused, match="hero-shout"):
            promote(
                '<section class="hero-shout"></section>',
                part="hero",
                round_id="1",
                sanitizer=_Refusing(),
                fragments_dir=tmp_path,
            )
        # The refusal is total: nothing lands on disk for the server to find.
        assert list(tmp_path.iterdir()) == []

    def test_the_digest_covers_the_body_and_not_the_comment(self) -> None:
        """The comment cannot vouch for itself, so the hash starts after it."""
        rendered = render_promoted_file(GOOD, round_id="7")
        header, _, body = rendered.partition("\n")
        assert body_digest(body) in header

    def test_promoting_twice_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        promote(GOOD, part="hero", round_id="1", sanitizer=_Accepting(), fragments_dir=tmp_path)
        second = '<section data-span-id="hero-v9" class="my-xl">second</section>'
        promotion = promote(
            second, part="hero", round_id="2", sanitizer=_Accepting(), fragments_dir=tmp_path
        )
        written = promotion.path.read_text()
        assert "hero-v9" in written and "hero-v2" not in written
        assert written.count("<!-- ui-servo: gated") == 1


class TestServerInterop:
    """The digest the writer computes must equal the one the reader recomputes.

    Rust trims what it reads before hashing (``sha256_of(hashed_body(...))``);
    Python trims before writing. If those two ever disagree by one newline, a
    valid promotion serves a 500 and the failure looks like tampering.
    """

    def test_the_rust_reader_and_the_python_writer_agree_on_what_is_hashed(self) -> None:
        rendered = render_promoted_file(GOOD, round_id="1")
        # What promoted.rs does: drop the first line, trim, hash.
        _, _, rust_body = rendered.partition("\n")
        assert sha256(rust_body.strip().encode()).hexdigest() == body_digest(GOOD)

    def test_the_live_promoted_hero_still_verifies(self) -> None:
        """The committed pick is not stale relative to its own provenance."""
        promoted = Path(__file__).resolve().parents[1] / "site/assets/fragments/hero.html"
        if not promoted.is_file():
            pytest.skip("no hero has been promoted yet")
        raw = promoted.read_text()
        header, _, body = raw.partition("\n")
        recorded = header.split("sha256=")[1].split()[0].rstrip("->").strip()
        assert body_digest(body) == recorded, "the promoted hero was edited after promotion"


def test_the_contract_the_demo_round_ran_against_still_parses() -> None:
    contract = DirectionContract.from_toml(CONTRACT.read_text())
    assert contract.class_allowlist_seed()
