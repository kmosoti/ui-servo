"""Tier 0 is the cheapest gate in the loop, so it is the one that must not be wrong.

Two things are being calibrated here. First, that the seeded-bad fixtures are
each rejected *for the stated reason* -- a gate that rejects everything is as
useless as one that accepts everything, and the violation kind is what the
control loop routes on. Second, that the clean fixture survives untouched: if
on-contract markup does not round-trip byte for byte, builders learn to fight
the sanitiser instead of the contract.
"""

from pathlib import Path

import pytest

from ui_servo.adapters.nh3_sanitizer import (
    HX_SWAP_POSITIONS,
    Nh3Sanitizer,
    class_names_from_css,
    default_sanitizer,
    normalize_css_escapes,
)
from ui_servo.domain.contract import DirectionContract
from ui_servo.ports.sanitizer import (
    AcceptAllSanitizer,
    SanitizeResult,
    SanitizerPort,
    Violation,
    ViolationKind,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = Path(__file__).resolve().parent / "fixtures" / "fragments"
CONTRACT_PATH = REPO_ROOT / "direction" / "direction.toml"


@pytest.fixture(scope="session")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sanitizer(contract: DirectionContract) -> Nh3Sanitizer:
    return Nh3Sanitizer.from_contract(contract)


def fragment(name: str) -> str:
    return (FRAGMENTS / name).read_text(encoding="utf-8")


def wrap(inner: str, *, span_id: str = "probe") -> str:
    """A minimal on-contract root, so a test can seed exactly one thing."""
    return f'<section class="p-lg" data-span-id="{span_id}">{inner}</section>'


# --------------------------------------------------------------------------- #
# The clean path.
# --------------------------------------------------------------------------- #


def test_clean_fixture_is_accepted(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(fragment("clean.html"))
    assert result.accepted, result.report()
    assert result.violations == ()
    assert bool(result) is True


def test_clean_fixture_round_trips_byte_for_byte(sanitizer: Nh3Sanitizer) -> None:
    source = fragment("clean.html")
    assert sanitizer.check(source).cleaned_html == source


def test_cleaned_output_is_idempotent(sanitizer: Nh3Sanitizer) -> None:
    once = sanitizer.check(fragment("clean.html")).cleaned_html
    assert once is not None
    twice = sanitizer.check(once)
    assert twice.accepted and twice.cleaned_html == once


def test_boolean_attributes_are_normalised_not_rejected(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(wrap('<input class="p-sm" type="email" name="email" required>'))
    assert result.accepted, result.report()
    assert 'required=""' in (result.cleaned_html or "")


def test_optional_end_tags_are_not_reported_as_truncation(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(wrap('<ul class="gap-sm"><li class="type-base">a<li class="type-base">b</ul>'))
    assert result.accepted, result.report()


# --------------------------------------------------------------------------- #
# The seeded-bad path: one fixture, one reason.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("xss_event_handler.html", ViolationKind.DISALLOWED_ATTRIBUTE),
        ("xss_script_tag.html", ViolationKind.DISALLOWED_TAG),
        ("xss_javascript_url.html", ViolationKind.DISALLOWED_URL),
        ("inline_style.html", ViolationKind.DISALLOWED_ATTRIBUTE),
        ("unknown_class.html", ViolationKind.UNKNOWN_CLASS),
        ("bad_hx_swap.html", ViolationKind.INVALID_ATTRIBUTE_VALUE),
        ("off_origin_hx_get.html", ViolationKind.DISALLOWED_URL),
        ("missing_span_id.html", ViolationKind.MISSING_SPAN_ID),
        ("data_hx_alias.html", ViolationKind.DISALLOWED_URL),
        ("trigger_filter.html", ViolationKind.INVALID_ATTRIBUTE_VALUE),
        ("backslash_url.html", ViolationKind.DISALLOWED_URL),
        ("srcset_javascript.html", ViolationKind.DISALLOWED_URL),
        ("stray_end_tag.html", ViolationKind.MALFORMED_FRAGMENT),
        ("loose_root_text.html", ViolationKind.MISSING_SPAN_ID),
    ],
)
def test_seeded_fixture_is_rejected_for_the_right_reason(
    sanitizer: Nh3Sanitizer, name: str, kind: ViolationKind
) -> None:
    result = sanitizer.check(fragment(name))
    assert not result.accepted
    assert result.cleaned_html is None
    assert kind in result.kinds(), result.report()


def test_rejection_names_the_element(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(fragment("xss_event_handler.html"))
    handler = next(v for v in result.violations if v.kind is ViolationKind.DISALLOWED_ATTRIBUTE)
    assert handler.locator == "figure > img"
    assert "onerror" in handler.detail


def test_script_content_is_not_smuggled_back_as_text(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(fragment("xss_script_tag.html"))
    assert not result.accepted
    assert ViolationKind.DISALLOWED_TAG in result.kinds()


def test_every_check_reports_rather_than_stopping_at_the_first(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(fragment("many_violations.html"))
    assert not result.accepted
    assert {
        ViolationKind.DISALLOWED_TAG,
        ViolationKind.DISALLOWED_ATTRIBUTE,
        ViolationKind.DISALLOWED_URL,
        ViolationKind.UNKNOWN_CLASS,
        ViolationKind.UNKNOWN_ATTRIBUTE,
        ViolationKind.INVALID_ATTRIBUTE_VALUE,
        ViolationKind.MISSING_SPAN_ID,
    } <= result.kinds(), result.report()


def test_violations_are_deduplicated_and_ordered(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(fragment("many_violations.html"))
    assert list(result.violations) == sorted(set(result.violations))


def test_truncated_output_is_malformed(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check('<section class="p-lg" data-span-id="x"><div class="p-sm">unfinished')
    assert ViolationKind.MALFORMED_FRAGMENT in result.kinds(), result.report()


# --------------------------------------------------------------------------- #
# Check 1: the nh3 policy surface.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "inner",
    [
        "<iframe src=\"/embed\"></iframe>",
        "<object data=\"/x.swf\"></object>",
        "<embed src=\"/x\">",
        "<form-injector></form-injector>",
        "<style>body{display:none}</style>",
    ],
)
def test_tags_outside_the_policy_are_rejected(sanitizer: Nh3Sanitizer, inner: str) -> None:
    result = sanitizer.check(wrap(inner))
    assert not result.accepted, inner
    assert ViolationKind.DISALLOWED_TAG in result.kinds(), result.report()


@pytest.mark.parametrize(
    "attribute",
    ['onclick="x()"', 'onmouseover="x()"', 'style="color:red"', 'srcdoc="<b>x</b>"'],
)
def test_attributes_outside_the_policy_are_rejected(sanitizer: Nh3Sanitizer, attribute: str) -> None:
    result = sanitizer.check(wrap(f'<p class="type-base" {attribute}>hi</p>'))
    assert not result.accepted, attribute
    assert ViolationKind.DISALLOWED_ATTRIBUTE in result.kinds(), result.report()


def test_hypermedia_prefixes_survive(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(
        wrap(
            '<button class="p-sm" hx-post="/fragments/save" hx-swap="outerHTML" '
            'data-analytics-id="save" aria-label="Save">Save</button>'
        )
    )
    assert result.accepted, result.report()


# --------------------------------------------------------------------------- #
# Check 2: the class allowlist.
# --------------------------------------------------------------------------- #


def test_seed_classes_come_from_the_contract(contract: DirectionContract, sanitizer: Nh3Sanitizer) -> None:
    assert contract.class_allowlist_seed() <= sanitizer.class_allowlist
    assert "bg-surface" in sanitizer.class_allowlist


def test_extra_classes_widen_the_allowlist(contract: DirectionContract) -> None:
    strict = Nh3Sanitizer.from_contract(contract)
    widened = Nh3Sanitizer.from_contract(contract, extra_classes={"glass-card"})
    source = fragment("unknown_class.html")
    assert ViolationKind.UNKNOWN_CLASS in strict.check(source).kinds()
    assert widened.check(source).accepted, widened.check(source).report()


def test_unknown_class_is_reported_per_element(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(wrap('<p class="type-base shimmer glow">hi</p>'))
    unknown = {v.detail.split("'")[1] for v in result.violations if v.kind is ViolationKind.UNKNOWN_CLASS}
    assert unknown == {"shimmer", "glow"}
    assert all(v.locator == "section > p" for v in result.violations)


def test_escaped_variant_selectors_normalise(contract: DirectionContract) -> None:
    css = r"""
    .p-lg { padding: 1rem }
    .hover\:text-accent:hover, .md\:gap-md { color: red }
    @media (min-width: 40rem) { .lg\:p-xl { padding: 2rem } }
    """
    names = class_names_from_css(css)
    assert {"p-lg", "hover:text-accent", "md:gap-md", "lg:p-xl"} <= names

    sanitizer = default_sanitizer(contract, tokens_css=css)
    result = sanitizer.check(wrap('<p class="hover:text-accent lg:p-xl">hi</p>'))
    assert result.accepted, result.report()


def test_normalize_css_escapes_handles_hex_and_literal_escapes() -> None:
    assert normalize_css_escapes(r"hover\:x") == "hover:x"
    assert normalize_css_escapes(r"w\31 \/2") == "w1/2"
    assert normalize_css_escapes("plain-name") == "plain-name"


def test_css_parsing_ignores_non_class_selectors() -> None:
    names = class_names_from_css(":root { --x: 1 } a#main::after { content: '' } .kept {}")
    assert names == frozenset({"kept"})


# --------------------------------------------------------------------------- #
# Check 3: the attribute schema.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("position", sorted(HX_SWAP_POSITIONS))
def test_every_documented_swap_position_is_accepted(sanitizer: Nh3Sanitizer, position: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-swap="{position}">x</div>'))
    assert result.accepted, result.report()


@pytest.mark.parametrize("swap", ["innerHTML swap:200ms settle:100ms", "outerHTML transition:true", "beforeend scroll:bottom"])
def test_swap_modifiers_are_accepted(sanitizer: Nh3Sanitizer, swap: str) -> None:
    assert sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-swap="{swap}">x</div>')).accepted


@pytest.mark.parametrize("swap", ["replaceAll", "innerhtml", "", "innerHTML teleport:1"])
def test_bad_swap_values_are_rejected(sanitizer: Nh3Sanitizer, swap: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-swap="{swap}">x</div>'))
    assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()


@pytest.mark.parametrize(
    "trigger",
    [
        "click",
        "load once",
        "every 2s",
        "keyup changed delay:500ms",
        "submit, keyup from:body",
        "click from:closest form",
        "intersect root:#main threshold:0.5",
        "click queue:first consume",
    ],
)
def test_sane_triggers_are_accepted(sanitizer: Nh3Sanitizer, trigger: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-trigger="{trigger}">x</div>'))
    assert result.accepted, result.report()


@pytest.mark.parametrize("trigger", ["click,", "", "click alert(1)", "click[ctrlKey"])
def test_broken_triggers_are_rejected(sanitizer: Nh3Sanitizer, trigger: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-trigger="{trigger}">x</div>'))
    assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()


@pytest.mark.parametrize("url", ["/fragments/x", "fragments/x", "./x", "?page=2", "#anchor", "/x?a=1&b=2"])
def test_same_origin_relative_urls_are_accepted(sanitizer: Nh3Sanitizer, url: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="{url}">x</div>'))
    assert result.accepted, result.report()


@pytest.mark.parametrize(
    "url",
    ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "http://localhost/x", ""],
)
def test_off_origin_or_scheme_urls_are_rejected(sanitizer: Nh3Sanitizer, url: str) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" hx-post="{url}">x</div>'))
    assert ViolationKind.DISALLOWED_URL in result.kinds(), result.report()


@pytest.mark.parametrize(
    "attribute",
    ['hx-on:click="alert(1)"', 'hx-on="alert(1)"', 'hx-on-click="alert(1)"', 'data-hx-on:click="alert(1)"'],
)
def test_hx_on_is_not_a_back_door_for_inline_javascript(
    sanitizer: Nh3Sanitizer, attribute: str
) -> None:
    result = sanitizer.check(wrap(f'<div class="p-sm" {attribute}>x</div>'))
    assert ViolationKind.DISALLOWED_ATTRIBUTE in result.kinds(), result.report()


def test_misspelled_htmx_attribute_fails_closed(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(wrap('<div class="p-sm" hx-swp="innerHTML">x</div>'))
    assert ViolationKind.UNKNOWN_ATTRIBUTE in result.kinds(), result.report()


# --------------------------------------------------------------------------- #
# The span id.
# --------------------------------------------------------------------------- #


def test_every_root_needs_a_span_id(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check(
        '<div class="p-sm" data-span-id="a">a</div><div class="p-sm">b</div>'
    )
    missing = [v for v in result.violations if v.kind is ViolationKind.MISSING_SPAN_ID]
    assert [v.locator for v in missing] == ["div:nth-of-type(2)"]


def test_a_span_id_must_be_a_slug(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check('<div class="p-sm" data-span-id="Not A Slug">x</div>')
    assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()


def test_empty_fragment_has_nothing_to_attribute(sanitizer: Nh3Sanitizer) -> None:
    result = sanitizer.check("   just text   ")
    assert ViolationKind.MISSING_SPAN_ID in result.kinds()
    assert result.violations[0].locator == ":root"


def test_span_id_requirement_can_be_relaxed_for_sub_fragments(contract: DirectionContract) -> None:
    relaxed = Nh3Sanitizer.from_contract(contract, require_span_id=False)
    assert relaxed.check(fragment("missing_span_id.html")).accepted


# --------------------------------------------------------------------------- #
# The port itself.
# --------------------------------------------------------------------------- #


def test_adapter_satisfies_the_port(sanitizer: Nh3Sanitizer) -> None:
    assert isinstance(sanitizer, SanitizerPort)


def test_the_trivial_implementation_also_satisfies_the_port() -> None:
    fake = AcceptAllSanitizer()
    assert isinstance(fake, SanitizerPort)
    assert fake.check("<p>x</p>").cleaned_html == "<p>x</p>"

    refusing = AcceptAllSanitizer(
        violations=(Violation(ViolationKind.UNKNOWN_CLASS, "p", "seeded"),)
    )
    assert not refusing.check("<p>x</p>")


def test_a_result_cannot_be_both_accepted_and_violated() -> None:
    with pytest.raises(ValueError):
        SanitizeResult(accepted=True, cleaned_html="<p></p>", violations=(Violation(ViolationKind.UNKNOWN_CLASS, "p", "x"),))
    with pytest.raises(ValueError):
        SanitizeResult(accepted=False)
    with pytest.raises(ValueError):
        SanitizeResult(accepted=False, cleaned_html="<p></p>", violations=(Violation(ViolationKind.UNKNOWN_CLASS, "p", "x"),))


def test_violation_renders_for_a_human() -> None:
    rendered = str(Violation(ViolationKind.MISSING_SPAN_ID, "section > div", "no data-span-id"))
    assert rendered == "missing-span-id: no data-span-id [section > div]"


def test_violations_are_frozen_at_construction() -> None:
    result = SanitizeResult.rejected([Violation(ViolationKind.UNKNOWN_CLASS, "p", "x")])
    assert isinstance(result.violations, tuple)
    with pytest.raises(AttributeError):
        result.violations.append(Violation(ViolationKind.UNKNOWN_CLASS, "p", "y"))  # type: ignore[attr-defined]


def test_an_accepted_result_must_carry_its_output() -> None:
    with pytest.raises(ValueError):
        SanitizeResult(accepted=True, cleaned_html=None)


# --------------------------------------------------------------------------- #
# Regressions from adversarial review. Each of these was, at one point, a way
# past the gate; each stays here so it cannot become one again.
# --------------------------------------------------------------------------- #


class TestDataHxAliasIsNotABypass:
    """htmx honours ``data-hx-*``; a gate that only reads ``hx-*`` is decorative."""

    @pytest.mark.parametrize(
        ("attribute", "kind"),
        [
            ('data-hx-get="https://evil.example/x"', ViolationKind.DISALLOWED_URL),
            ('data-hx-get="/\\evil.example/x"', ViolationKind.DISALLOWED_URL),
            ('data-hx-swap="teleport"', ViolationKind.INVALID_ATTRIBUTE_VALUE),
            ('data-hx-trigger="click[alert(1)]"', ViolationKind.INVALID_ATTRIBUTE_VALUE),
            ('data-hx-trigger="click,"', ViolationKind.INVALID_ATTRIBUTE_VALUE),
            ('data-hx-swp="innerHTML"', ViolationKind.UNKNOWN_ATTRIBUTE),
        ],
    )
    def test_data_prefixed_attributes_get_the_same_schema(
        self, sanitizer: Nh3Sanitizer, attribute: str, kind: ViolationKind
    ) -> None:
        result = sanitizer.check(wrap(f'<div class="p-sm" {attribute}>x</div>'))
        assert kind in result.kinds(), result.report()

    def test_both_spellings_of_a_valid_attribute_are_accepted(self, sanitizer: Nh3Sanitizer) -> None:
        assert sanitizer.check(wrap('<div class="p-sm" data-hx-get="/f" data-hx-swap="innerHTML">x</div>')).accepted

    def test_conflicting_spellings_of_one_attribute_are_rejected(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check(wrap('<div class="p-sm" hx-get="/a" data-hx-get="/b">x</div>'))
        assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()

    def test_the_error_names_the_spelling_the_author_used(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check(wrap('<div class="p-sm" data-hx-swap="teleport">x</div>'))
        assert any("data-hx-swap" in violation.detail for violation in result.violations), result.report()


class TestTriggerFiltersAreScript:
    """htmx evaluates ``hx-trigger`` filters, so no filter may pass the gate."""

    @pytest.mark.parametrize(
        "trigger",
        [
            "click[alert(1)]",
            "click[ctrlKey]",
            "keyup[key=='Enter']",
            "click[document.cookie.length>0] once",
        ],
    )
    def test_event_filters_are_refused(self, sanitizer: Nh3Sanitizer, trigger: str) -> None:
        result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-trigger="{trigger}">x</div>'))
        assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()
        assert any("filter" in violation.detail for violation in result.violations)


class TestUrlGrammarMatchesBrowsersNotUrlsplit:
    """Where Python's parser and a browser's disagree, the browser is the threat model."""

    @pytest.mark.parametrize(
        "url",
        [
            r"/\evil.example/steal",
            r"\\evil.example/steal",
            "//evil.example/x",
            "/x\twith\ttabs",
            "/x with spaces",
            " /leading-space",
            "/trailing-space ",
            "/caf\u00e9",
            "java\tscript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "\x01/control",
            "http://[",
        ],
    )
    def test_hostile_urls_are_rejected(self, sanitizer: Nh3Sanitizer, url: str) -> None:
        result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="{url}">x</div>'))
        assert not result.accepted, url
        assert ViolationKind.DISALLOWED_URL in result.kinds(), result.report()

    def test_a_malformed_url_in_a_plain_attribute_does_not_crash_the_gate(
        self, sanitizer: Nh3Sanitizer
    ) -> None:
        result = sanitizer.check(wrap('<a class="text-accent" href="http://[">x</a>'))
        assert isinstance(result, SanitizeResult)
        assert not result.accepted

    @pytest.mark.parametrize(
        "attribute",
        [
            'srcset="javascript:alert(1) 1x, /img/a.avif 2x"',
            'srcset="/img/a.avif 1x, vbscript:msgbox(1) 2x"',
        ],
    )
    def test_srcset_candidates_are_scheme_checked(self, sanitizer: Nh3Sanitizer, attribute: str) -> None:
        result = sanitizer.check(wrap(f'<img class="p-sm" src="/img/a.avif" alt="a" {attribute}>'))
        assert ViolationKind.DISALLOWED_URL in result.kinds(), result.report()

    def test_poster_and_formaction_are_scheme_checked(self, sanitizer: Nh3Sanitizer) -> None:
        video = sanitizer.check(wrap('<video class="p-sm" src="/v.mp4" poster="javascript:alert(1)"></video>'))
        assert ViolationKind.DISALLOWED_URL in video.kinds(), video.report()
        button = sanitizer.check(wrap('<button class="p-sm" formaction="javascript:alert(1)">go</button>'))
        assert ViolationKind.DISALLOWED_URL in button.kinds(), button.report()

    def test_ordinary_relative_urls_still_work(self, sanitizer: Nh3Sanitizer) -> None:
        assert sanitizer.check(
            wrap('<img class="p-sm" src="/img/a.avif" alt="a" srcset="/img/a.avif 1x, /img/a@2x.avif 2x">')
        ).accepted


HOSTILE_CORPUS: tuple[str, ...] = (
    "",
    " ",
    "<",
    "<<<<",
    "<div",
    "<div>",
    "</div>",
    "<div class=>",
    '<div class="">',
    "<div class=unclosed",
    "<!doctype html>",
    "<!-- comment -->",
    "<![CDATA[x]]>",
    "<?php echo 1; ?>",
    "<div " + "a" * 5000 + ">x</div>",
    "<" * 500 + ">" * 500,
    '<a href="http://[">x</a>',
    '<a href="http://[::1]/">x</a>',
    '<a href="%">x</a>',
    '<a href="http://example.com:99999999999/">x</a>',
    '<div hx-get="http://[">x</div>',
    '<div hx-get="\udcff">x</div>',
    '<div class="\x00\x01\x02">x</div>',
    "<div class='p-lg\u00a0p-sm'>x</div>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
    "<div>\u2028\u2029\ufeff</div>",
    "<div>" + "\U0001f600" * 100 + "</div>",
    "<textarea></textarea></textarea>",
    "<template><div></div></template>",
    "<p>a<p>b<p>c",
    "<table><tr><td>a<td>b</table>",
    "\x00",
    "&#x6a;avascript:alert(1)",
    '<a href="&#106;avascript:alert(1)">x</a>',
    '<a href="jav&#x09;ascript:alert(1)">x</a>',
)


@pytest.mark.parametrize("hostile", HOSTILE_CORPUS, ids=lambda value: repr(value)[:48])
def test_the_gate_never_raises_on_hostile_input(sanitizer: Nh3Sanitizer, hostile: str) -> None:
    """A gate that can be made to raise is a gate that can be made not to apply."""
    result = sanitizer.check(hostile)
    assert isinstance(result, SanitizeResult)
    assert result.accepted is False or result.cleaned_html is not None


def test_the_hostile_corpus_is_actually_rejected(sanitizer: Nh3Sanitizer) -> None:
    accepted = [source for source in HOSTILE_CORPUS if sanitizer.check(source).accepted]
    assert accepted == []


class TestClassTokensAreComparedVerbatim:
    """The class gate must not transform what it is about to compare."""

    def test_css_escapes_in_a_class_attribute_are_not_unescaped(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check(wrap(r'<p class="p\-lg">x</p>'))
        assert ViolationKind.UNKNOWN_CLASS in result.kinds(), result.report()

    def test_non_ascii_whitespace_does_not_separate_classes(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check(wrap('<p class="p-lg\u00a0p-sm">x</p>'))
        assert ViolationKind.UNKNOWN_CLASS in result.kinds(), result.report()

    @pytest.mark.parametrize("separator", [" ", "\t", "\n", "\r", "\x0c", "   \t\n"])
    def test_ascii_whitespace_does_separate_classes(self, sanitizer: Nh3Sanitizer, separator: str) -> None:
        assert sanitizer.check(wrap(f'<p class="p-lg{separator}p-sm">x</p>')).accepted

    def test_an_allowlist_fed_from_css_still_unescapes(self, contract: DirectionContract) -> None:
        widened = default_sanitizer(contract, tokens_css=r".hover\:p-lg { padding: 1rem }")
        assert widened.check(wrap('<p class="hover:p-lg">x</p>')).accepted
        assert not widened.check(wrap(r'<p class="hover\:p-lg">x</p>')).accepted


class TestCssEscapeNormalisationNeverRaises:
    @pytest.mark.parametrize("escaped", [r"\ffffff", r"\110000", r"\0", r"\d800", r"\10FFFF", "\\"])
    def test_out_of_range_escapes_degrade_instead_of_crashing(self, escaped: str) -> None:
        assert isinstance(normalize_css_escapes(escaped), str)

    @pytest.mark.parametrize("css", [r".\ffffff {}", r".a\110000 b {}", r".\0 {}"])
    def test_a_hostile_stylesheet_parses(self, css: str) -> None:
        assert isinstance(class_names_from_css(css), frozenset)


class TestStructuralRepairIsRejection:
    @pytest.mark.parametrize(
        "source",
        [
            '<div class="p-sm" data-span-id="x"><span>a</div>',
            '<div class="p-sm" data-span-id="x">a</span></div>',
            '<div class="p-sm" data-span-id="x">a</div></script>',
            '<div class="p-sm" data-span-id="x">a</div></div>',
            '<div class="p-sm" data-span-id="x"><b><i>a</b></i></div>',
            '<div class="p-sm" data-span-id="x">unfinished',
        ],
    )
    def test_repairable_markup_is_refused(self, sanitizer: Nh3Sanitizer, source: str) -> None:
        result = sanitizer.check(source)
        assert ViolationKind.MALFORMED_FRAGMENT in result.kinds(), result.report()

    @pytest.mark.parametrize(
        "source",
        [
            '<div class="p-sm" data-span-id="x"><!doctype html></div>',
            '<div class="p-sm" data-span-id="x"><?xml-stylesheet href="/x.css"?></div>',
            '<div class="p-sm" data-span-id="x"><![CDATA[alert(1)]]></div>',
        ],
    )
    def test_declarations_and_processing_instructions_are_refused(
        self, sanitizer: Nh3Sanitizer, source: str
    ) -> None:
        result = sanitizer.check(source)
        assert ViolationKind.MALFORMED_FRAGMENT in result.kinds(), result.report()

    @pytest.mark.parametrize(
        "inner",
        [
            "<p>one</p><div>two</div>",
            "<p>one<div>two</div>",
            "<ul><li>a<li>b</ul>",
            "<dl><dt>a<dd>b</dl>",
            "<table><colgroup><col><tbody><tr><td>a<td>b</table>",
            "<select><optgroup label='g'><option>a<option>b</select>",
            "<ruby>base<rt>gloss</ruby>",
        ],
    )
    def test_html_optional_end_tags_are_not_truncation(self, sanitizer: Nh3Sanitizer, inner: str) -> None:
        result = sanitizer.check(wrap(inner))
        assert ViolationKind.MALFORMED_FRAGMENT not in result.kinds(), result.report()


class TestModifierValuesAreChecked:
    @pytest.mark.parametrize(
        "swap",
        [
            "innerHTML swap:banana",
            "innerHTML settle:100",
            "innerHTML transition:yes",
            "innerHTML scroll:sideways",
            "innerHTML swap",
            "innerHTML ignoreTitle:1",
        ],
    )
    def test_bogus_swap_modifier_values_are_rejected(self, sanitizer: Nh3Sanitizer, swap: str) -> None:
        result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-swap="{swap}">x</div>'))
        assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()

    @pytest.mark.parametrize(
        "swap",
        ["innerHTML swap:200ms settle:1.5s", "outerHTML transition:true", "beforeend show:#main:top", "innerHTML scroll:bottom"],
    )
    def test_documented_swap_modifier_values_are_accepted(self, sanitizer: Nh3Sanitizer, swap: str) -> None:
        assert sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-swap="{swap}">x</div>')).accepted

    @pytest.mark.parametrize(
        "trigger",
        [
            "click once:oops",
            "click delay:banana",
            "click throttle:500",
            "click queue:sometimes",
            "click threshold:soon",
            "click delay",
            "click from:",
            "click from:closest",
        ],
    )
    def test_bogus_trigger_modifier_values_are_rejected(self, sanitizer: Nh3Sanitizer, trigger: str) -> None:
        result = sanitizer.check(wrap(f'<div class="p-sm" hx-get="/f" hx-trigger="{trigger}">x</div>'))
        assert ViolationKind.INVALID_ATTRIBUTE_VALUE in result.kinds(), result.report()


class TestRootLevelContentIsAttributable:
    def test_loose_text_beside_a_valid_root_is_still_a_violation(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check('stray words<div class="p-sm" data-span-id="ok">x</div>')
        assert ViolationKind.MISSING_SPAN_ID in result.kinds(), result.report()

    def test_whitespace_between_roots_is_not_a_violation(self, sanitizer: Nh3Sanitizer) -> None:
        result = sanitizer.check(
            '<div class="p-sm" data-span-id="a">x</div>\n  \n<div class="p-sm" data-span-id="b">y</div>'
        )
        assert result.accepted, result.report()


class TestTagPolicyCoversStandardElements:
    @pytest.mark.parametrize(
        "inner",
        [
            "<address>a</address>",
            "<bdi>a</bdi>",
            "<bdo dir='rtl'>a</bdo>",
            "<ruby>a<rp>(</rp><rt>b</rt><rp>)</rp></ruby>",
            "<figure><figcaption>a</figcaption></figure>",
            "<details><summary>a</summary>b</details>",
            "<abbr title='a'>b</abbr><cite>c</cite><kbd>d</kbd><samp>e</samp><var>f</var>",
            "<sub>a</sub><sup>b</sup><time datetime='2026-08-05'>c</time><dfn>d</dfn>",
            "<mark>a</mark><small>b</small><s>c</s><u>d</u><wbr>",
            "<picture><source srcset='/a.avif'><img src='/a.avif' alt='a'></picture>",
            "<video src='/v.mp4'><track src='/t.vtt' kind='captions'></video>",
            "<table><caption>c</caption><colgroup><col></colgroup><thead><tr><th>h</th></tr></thead>"
            "<tbody><tr><td>d</td></tr></tbody><tfoot><tr><td>f</td></tr></tfoot></table>",
            "<dl><dt>a</dt><dd>b</dd></dl>",
            "<menu><li>a</li></menu>",
            "<search><form action='/s'><input type='search' name='q'></form></search>",
            "<hgroup><h2>a</h2><p>b</p></hgroup>",
        ],
    )
    def test_safe_standard_elements_are_allowed(self, sanitizer: Nh3Sanitizer, inner: str) -> None:
        result = sanitizer.check(wrap(inner))
        assert ViolationKind.DISALLOWED_TAG not in result.kinds(), result.report()


class TestStrippingIsAttributedPerElement:
    def test_a_surviving_attribute_does_not_vouch_for_a_stripped_one(
        self, sanitizer: Nh3Sanitizer
    ) -> None:
        result = sanitizer.check(
            wrap('<a class="text-accent" href="/ok">ok</a><a class="text-accent" href="ftp://evil.example/x">bad</a>')
        )
        stripped = [v for v in result.violations if v.kind is ViolationKind.DISALLOWED_ATTRIBUTE]
        assert [v.locator for v in stripped] == ["section > a:nth-of-type(2)"], result.report()

    def test_a_surviving_element_does_not_vouch_for_a_stripped_sibling(
        self, sanitizer: Nh3Sanitizer
    ) -> None:
        result = sanitizer.check(wrap('<p class="type-base">ok</p><p class="type-base" style="color:red">bad</p>'))
        stripped = [v for v in result.violations if v.kind is ViolationKind.DISALLOWED_ATTRIBUTE]
        assert [v.locator for v in stripped] == ["section > p:nth-of-type(2)"], result.report()


class TestCssHarvestingIsThorough:
    @pytest.mark.parametrize(
        ("css", "expected"),
        [
            (":is(.alpha, .beta) p {}", {"alpha", "beta"}),
            (":where(.gamma) {}", {"gamma"}),
            ("p:not(.delta) {}", {"delta"}),
            (".outer { color: red; .inner { color: blue } }", {"outer", "inner"}),
            ("@media print { @supports (display:grid) { .deep {} } }", {"deep"}),
            ("@layer base { .layered {} }", {"layered"}),
        ],
    )
    def test_classes_are_found_wherever_they_hide(self, css: str, expected: set[str]) -> None:
        assert expected <= class_names_from_css(css)


class TestSpanIdSlug:
    @pytest.mark.parametrize("value", ["Not A Slug", "trailing\n", "", "UPPER", "sp ace", "x" * 100])
    def test_bad_slugs_are_rejected(self, sanitizer: Nh3Sanitizer, value: str) -> None:
        result = sanitizer.check(f'<div class="p-sm" data-span-id="{value}">x</div>')
        assert not result.accepted, value
        assert result.kinds() & {ViolationKind.INVALID_ATTRIBUTE_VALUE, ViolationKind.MISSING_SPAN_ID}

    @pytest.mark.parametrize("value", ["a", "notes-index", "hero_2", "x9"])
    def test_good_slugs_are_accepted(self, sanitizer: Nh3Sanitizer, value: str) -> None:
        assert sanitizer.check(f'<div class="p-sm" data-span-id="{value}">x</div>').accepted
