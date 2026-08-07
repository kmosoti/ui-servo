"""Promotion: a pick only becomes the site if it can prove it was gated.

The Rust half of this contract is tested in ``site/src/fragments/promoted.rs``;
these are the Python half, plus one test that pins the *interop* — the digest
this module writes has to be the digest the server recomputes, or a promotion
that is perfectly valid fails to serve.
"""

import re
from hashlib import sha256
from pathlib import Path

import pytest

from ui_servo.control.promote import (
    PromotionRefused,
    body_digest,
    promote,
    render_promoted_file,
    strip_span_ids,
)
from ui_servo.domain.contract import DirectionContract
from ui_servo.ports.sanitizer import SanitizeResult, SanitizerPort, Violation, ViolationKind

CONTRACT = Path(__file__).resolve().parents[1] / "direction" / "direction.toml"
_SPAN_ID = re.compile(r"data-span-id\s*=", re.IGNORECASE)
"""What must be gone from a promoted file, and present nowhere it was not."""

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
        # The body, minus the candidate's own span id — see
        # test_the_candidates_span_id_does_not_survive_promotion.
        assert strip_span_ids(GOOD) in written
        assert promotion.digest == sha256(strip_span_ids(GOOD).strip().encode()).hexdigest()

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

    def test_the_candidates_span_id_does_not_survive_promotion(self, tmp_path: Path) -> None:
        """The join key belongs to the round, not to the site.

        The server's ``frame`` stamps a fresh span id on what it serves. A
        retained inner id means the page carries two, and the probe files live
        readings under a candidate that stopped existing when the round ended —
        a mis-attributed sensor reading, which is quieter and worse than an error.
        """
        promotion = promote(
            GOOD, part="hero", round_id="4", sanitizer=_Accepting(), fragments_dir=tmp_path
        )
        written = promotion.path.read_text()
        assert "data-span-id" not in written
        assert 'class="my-3xl"' in written, "stripping the id must not disturb other attributes"
        # The hash covers the stripped body, so the server still verifies it.
        _, _, body = written.partition("\n")
        assert body_digest(body) == promotion.digest

    @pytest.mark.parametrize(
        "spelling",
        [
            '<section data-span-id="hero-v0" class="my-3xl">x</section>',
            "<section data-span-id='hero-v0' class='my-3xl'>x</section>",
            "<section data-span-id=hero-v0 class='my-3xl'>x</section>",
            '<section  DATA-SPAN-ID = "hero-v0"  class="my-3xl">x</section>',
            '<div data-span-id="a"><p data-span-id="b">nested</p></div>',
        ],
    )
    def test_every_spelling_of_the_attribute_is_stripped(self, spelling: str) -> None:
        assert "data-span-id" not in strip_span_ids(spelling).lower()

    def test_stripping_leaves_lookalike_attributes_alone(self) -> None:
        """`data-span-idea` and `id` are different attributes, not this one."""
        kept = '<section id="hero" data-span-idea="x" aria-describedby="data-span-id">y</section>'
        assert strip_span_ids(kept) == kept

    @pytest.mark.parametrize(
        "markup",
        [
            # A `>` inside a quoted value must not be read as the end of the tag,
            # or the span id after it is never seen.
            '<section title="a > b" data-span-id="hero-v0">x</section>',
            "<section title='a > b' data-span-id='hero-v0'>x</section>",
            '<img src="a.png" data-span-id=hero-v0 />',
            '<section  DATA-SPAN-ID = "hero-v0"  class="my-3xl">x</section>',
        ],
    )
    def test_the_id_is_found_however_the_tag_is_written(self, markup: str) -> None:
        assert not _SPAN_ID.search(strip_span_ids(markup)), markup

    @pytest.mark.parametrize(
        "untouched",
        [
            # Body text, in every disguise.
            '<p>the attribute data-span-id="hero" is the join key</p>',
            '<pre>&lt;section data-span-id="x"&gt;</pre>',
            "<p>Set <code>data-span-id='a'</code> on the root.</p>",
            '<!-- a note about data-span-id="x" -->',
            # Another attribute's *value*. Stripping here would edit markup a
            # critic already approved, then hash the corruption as if judged.
            '<p title="a data-span-id=&quot;fake&quot; b">t</p>',
            "<section data-tip='use data-span-id=x' class='my-lg'>y</section>",
            # A different attribute that merely starts the same way.
            '<section id="hero" data-span-idea="x">y</section>',
        ],
    )
    def test_nothing_else_is_rewritten(self, untouched: str) -> None:
        """Promotion runs *after* the gate, so anything it edits is unjudged.

        Both failure directions are silent: the result still sanitises, still
        hashes and still serves, so nothing downstream would ever report it.
        """
        assert strip_span_ids(untouched) == untouched

    def test_promoting_twice_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        promote(GOOD, part="hero", round_id="1", sanitizer=_Accepting(), fragments_dir=tmp_path)
        second = '<section data-span-id="hero-v9" class="my-xl">second</section>'
        promotion = promote(
            second, part="hero", round_id="2", sanitizer=_Accepting(), fragments_dir=tmp_path
        )
        written = promotion.path.read_text()
        assert "second" in written and "Hi" not in written
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
        promoted = Path(__file__).resolve().parents[1] / "site/promoted/hero.html"
        if not promoted.is_file():
            pytest.skip("no hero has been promoted yet")
        raw = promoted.read_text()
        header, _, body = raw.partition("\n")
        recorded = header.split("sha256=")[1].split()[0].rstrip("->").strip()
        assert body_digest(body) == recorded, "the promoted hero was edited after promotion"


def test_the_contract_the_demo_round_ran_against_still_parses() -> None:
    contract = DirectionContract.from_toml(CONTRACT.read_text())
    assert contract.class_allowlist_seed()
