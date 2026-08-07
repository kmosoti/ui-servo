"""The shadow browser, tested against pages with known defects seeded into them.

A sensor is only trustworthy if it has been shown to fire. Every fixture here is
a page with exactly one thing wrong (or one thing deliberately right), so a test
that passes says the sensor detected *that* defect rather than that it produced
some output. The pairs matter as much as the cases: honoured/ignored reduced
motion, baseline/regressed pixels and generic/on-contract styling are the same page
twice, differing only in the property under observation, which is what rules out
the sensor having noticed something incidental.

Chromium is launched once for the module. A missing browser is an environment fact
rather than a regression, so it skips -- unless ``UI_SERVO_REQUIRE_BROWSER=1``, in
which case it fails loudly. Acceptance runs set that variable: a suite that quietly
skips its only integration tests is a suite that reports green for an unmeasured
sensor.
"""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("PIL")

from ui_servo.adapters.playwright_sensor import (  # noqa: E402
    AXE_SHA256,
    AXE_VERSION,
    PlaywrightSensor,
    SensorError,
    axe_source,
)
from ui_servo.domain.contract import DirectionContract  # noqa: E402
from ui_servo.domain.evidence import Signal, spans_in_turn  # noqa: E402
from ui_servo.domain.variant import (  # noqa: E402
    COLOR_BINS,
    StyleSample,
    StyleVector,
    blandness,
    build_anti_corpus,
)
from ui_servo.ports.sensor import (  # noqa: E402
    FULL_PAGE_SCREENSHOT,
    ObservationCall,
    PixelDiff,
    SensorPort,
    SensorReport,
    StubSensor,
    TraceSummary,
    span_screenshot_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sensor"
BLANDNESS = REPO_ROOT / "tests" / "fixtures" / "blandness"
ANTI_CORPUS_NAMES = ("generic_shadcn", "generic_bootstrap", "generic_saas_landing")
TURN = "turn-u5"

FAILURE_KINDS = frozenset({"axe-violation", "motion-violation", "capture-error"})
"""The kinds *this test* counts as departures.

The domain deliberately ships no default set -- which kinds are failures is
policy -- so a caller that wants the distinction states it, and the sensor is
tested for emitting kinds a policy can route on rather than for agreeing with one.
"""


def fixture_url(name: str) -> str:
    path = FIXTURES / name
    assert path.exists(), f"missing sensor fixture {path}"
    return path.as_uri()


def compose_page(fragment: Path, stylesheet: Path, destination: Path) -> str:
    """A full page from a builder's fragment and the stylesheet it presumes.

    The blandness fixtures are fragments: markup and class names, no CSS. U10's
    unit test does not need any, because its samples are hand-derived; a browser
    does, because it cannot render a look that was never declared. The markup is
    used verbatim -- only the stylesheet is supplied.
    """
    destination.write_text(
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{fragment.stem}</title><style>\n"
        f"{stylesheet.read_text(encoding='utf-8')}\n</style></head><body>\n"
        f"{fragment.read_text(encoding='utf-8')}\n</body></html>\n",
        encoding="utf-8",
    )
    return destination.as_uri()


@pytest.fixture(scope="module")
def sensor(tmp_path_factory: pytest.TempPathFactory):
    instance = PlaywrightSensor(artifacts_dir=tmp_path_factory.mktemp("sensor-artifacts"))
    try:
        with instance:
            yield instance
    except SensorError as error:
        if os.environ.get("UI_SERVO_REQUIRE_BROWSER") == "1":
            pytest.fail(f"UI_SERVO_REQUIRE_BROWSER=1 but chromium is unusable: {error}")
        pytest.skip(f"headless chromium unavailable: {error}")


@pytest.fixture(scope="session")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(
        (REPO_ROOT / "direction" / "direction.toml").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def contrast_report(sensor: PlaywrightSensor) -> SensorReport:
    return sensor.observe(fixture_url("contrast.html"), span_id="contrast-main", turn_id=TURN)


@pytest.fixture(scope="module")
def baseline_report(sensor: PlaywrightSensor) -> SensorReport:
    return sensor.observe(fixture_url("baseline.html"), span_id="pixel-baseline", turn_id=TURN)


@pytest.fixture(scope="module")
def regressed_report(sensor: PlaywrightSensor, baseline_report: SensorReport) -> SensorReport:
    return sensor.observe(
        fixture_url("regressed.html"),
        span_id="pixel-regressed",
        turn_id=TURN,
        baseline=baseline_report.full_page_screenshot,
    )


@pytest.fixture(scope="module")
def hazards_report(sensor: PlaywrightSensor) -> SensorReport:
    return sensor.observe(
        fixture_url("capture_hazards.html"), span_id="hazards-main", turn_id=TURN
    )


class TestVendoredAxe:
    """The pin is enforced by the adapter, so it is testable without a browser."""

    def test_source_matches_the_recorded_digest(self) -> None:
        source = axe_source()
        assert f"axe v{AXE_VERSION}" in source[:200]
        assert len(AXE_SHA256) == 64

    def test_a_tampered_engine_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        forged = tmp_path / "axe.min.js"
        forged.write_text("window.axe = { run: async () => ({ violations: [] }) };\n")
        monkeypatch.setattr("ui_servo.adapters.playwright_sensor.AXE_PATH", forged)
        with pytest.raises(SensorError, match="digest"):
            axe_source()


class TestTheContractIsCheapToSatisfy:
    """The port, exercised with no browser at all -- the shape consumers code against."""

    def test_a_report_can_be_built_by_hand_exactly_as_documented(self) -> None:
        report = SensorReport(
            span_id="hero",
            turn_id="turn-3",
            url="http://localhost:8000/",
            screenshots={
                FULL_PAGE_SCREENSHOT: Path("/tmp/full-page.png"),
                span_screenshot_key("hero"): Path("/tmp/hero.png"),
            },
            aria_snapshot='- heading "Ship it" [level=1]',
            axe_violations=({"id": "color-contrast", "nodes": []},),
            pixel_diff=PixelDiff(ratio=0.0),
            trace=TraceSummary(dropped_frames=0, longtasks=0),
            reduced_motion_ok=True,
        )
        assert report.full_page_screenshot == Path("/tmp/full-page.png")
        assert report.screenshot_for("hero") == Path("/tmp/hero.png")
        assert report.span_screenshots == {"hero": Path("/tmp/hero.png")}
        assert report.axe_violation_ids == ("color-contrast",)
        assert report.style_sample is None

    def test_a_ratio_outside_the_unit_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="fraction"):
            PixelDiff(ratio=1.5)

    def test_negative_trace_counts_are_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            TraceSummary(dropped_frames=-1, longtasks=0)

    def test_the_stub_satisfies_the_port_and_records_the_call(self) -> None:
        stub = StubSensor(report=SensorReport(span_id="x", turn_id="y", url="z", aria_snapshot="a"))
        assert isinstance(stub, SensorPort)
        report = stub.observe(
            "http://localhost/hero",
            span_id="hero",
            turn_id="turn-3",
            interactions=[{"action": "click", "selector": "#go"}],
        )
        assert (report.span_id, report.turn_id, report.url) == (
            "hero",
            "turn-3",
            "http://localhost/hero",
        )
        assert report.aria_snapshot == "a"
        assert stub.calls == [
            ObservationCall(
                url="http://localhost/hero",
                span_id="hero",
                turn_id="turn-3",
                baseline=None,
                interactions=({"action": "click", "selector": "#go"},),
            )
        ]


class TestPortConformance:
    def test_the_adapter_satisfies_the_port(self, sensor: PlaywrightSensor) -> None:
        assert isinstance(sensor, SensorPort)


class TestAxeAudit:
    def test_seeded_contrast_violation_is_reported(self, contrast_report: SensorReport) -> None:
        assert "color-contrast" in contrast_report.axe_violation_ids

    def test_the_violation_names_the_offending_element_only(
        self, contrast_report: SensorReport
    ) -> None:
        contrast = next(
            violation
            for violation in contrast_report.axe_violations
            if violation["id"] == "color-contrast"
        )
        markup = " ".join(str(node.get("html", "")) for node in contrast["nodes"])
        assert "contrast-faint" in markup
        assert "contrast-legible" not in markup

    def test_violations_are_passed_through_verbatim(self, contrast_report: SensorReport) -> None:
        contrast = next(
            violation
            for violation in contrast_report.axe_violations
            if violation["id"] == "color-contrast"
        )
        assert {"id", "impact", "tags", "help", "nodes"} <= set(contrast)
        assert any(tag.startswith("wcag") for tag in contrast["tags"])
        assert contrast["nodes"] and contrast["nodes"][0]["target"]

    def test_a_violation_is_attributed_to_the_span_that_owns_the_node(
        self, contrast_report: SensorReport
    ) -> None:
        """The join key has to point at the broken fragment, not at the page."""
        axe_signals = [
            signal for signal in contrast_report.signals if signal.kind == "axe-violation"
        ]
        contrast = next(signal for signal in axe_signals if signal.payload["rule"] == "color-contrast")
        assert contrast.span_id == "contrast-faint"
        assert contrast.span_id != contrast_report.span_id
        assert contrast.payload["spans"] == ["contrast-faint"]

    def test_every_violation_becomes_at_least_one_harness_signal(
        self, contrast_report: SensorReport
    ) -> None:
        axe_signals = [
            signal for signal in contrast_report.signals if signal.kind == "axe-violation"
        ]
        assert {signal.payload["rule"] for signal in axe_signals} == set(
            contrast_report.axe_violation_ids
        )
        assert all(signal.kind in FAILURE_KINDS for signal in axe_signals)

    def test_a_strict_csp_page_is_still_audited(self, sensor: PlaywrightSensor) -> None:
        """A locked-down ``script-src`` must not read as an unauditable page."""
        report = sensor.observe(fixture_url("strict_csp.html"), span_id="csp-main", turn_id=TURN)
        assert "color-contrast" in report.axe_violation_ids
        assert report.aria_snapshot
        assert report.style_sample is not None


class TestCaptureAndAriaSnapshot:
    def test_full_page_and_per_span_screenshots_are_written(
        self, contrast_report: SensorReport
    ) -> None:
        assert set(contrast_report.span_screenshots) >= {
            "contrast-main",
            "contrast-faint",
            "contrast-legible",
        }
        assert contrast_report.full_page_screenshot is not None
        for path in contrast_report.screenshots.values():
            assert path.exists() and path.stat().st_size > 0

    def test_a_span_can_be_looked_up_by_its_join_key(self, contrast_report: SensorReport) -> None:
        assert contrast_report.screenshot_for("contrast-faint") is not None
        assert contrast_report.screenshot_for("never-rendered") is None

    def test_a_span_named_after_the_page_key_does_not_collide_with_it(
        self, hazards_report: SensorReport
    ) -> None:
        page = hazards_report.full_page_screenshot
        span = hazards_report.screenshot_for(FULL_PAGE_SCREENSHOT)
        assert page is not None and span is not None
        assert page != span
        assert hazards_report.screenshots[span_screenshot_key("full-page")] == span

    def test_an_element_screenshot_is_attributed_to_its_own_span(
        self, hazards_report: SensorReport
    ) -> None:
        element_signals = {
            signal.span_id: signal
            for signal in hazards_report.signals
            if signal.kind == "screenshot" and signal.payload["scope"] == "element"
        }
        assert "hazard-visible" in element_signals
        assert element_signals["hazard-visible"].payload["path"].endswith(".png")
        page_signal = next(
            signal
            for signal in hazards_report.signals
            if signal.kind == "screenshot" and signal.payload["scope"] == "full-page"
        )
        assert page_signal.span_id == "hazards-main"

    def test_unphotographable_spans_are_reported_not_dropped(
        self, hazards_report: SensorReport
    ) -> None:
        failures = {
            signal.span_id: signal.payload["reason"]
            for signal in hazards_report.signals
            if signal.kind == "capture-error"
        }
        assert set(failures) == {
            "hazard-display-none",
            "hazard-visibility-hidden",
            "hazard-zero-area",
        }
        assert all(reason for reason in failures.values())
        assert "zero-area" in failures["hazard-zero-area"]
        assert hazards_report.screenshot_for("hazard-display-none") is None
        assert hazards_report.screenshot_for("hazard-visible") is not None

    def test_aria_snapshot_describes_the_tree(self, contrast_report: SensorReport) -> None:
        snapshot = contrast_report.aria_snapshot
        assert "heading" in snapshot
        assert "Readable heading" in snapshot


class TestArtefactsAreNeverOverwritten:
    def test_two_observations_of_one_span_keep_both_renders(
        self, sensor: PlaywrightSensor
    ) -> None:
        first = sensor.observe(fixture_url("baseline.html"), span_id="repeat", turn_id="turn-a")
        second = sensor.observe(fixture_url("regressed.html"), span_id="repeat", turn_id="turn-a")
        assert first.full_page_screenshot != second.full_page_screenshot
        assert first.full_page_screenshot.exists() and second.full_page_screenshot.exists()
        assert first.full_page_screenshot.read_bytes() != second.full_page_screenshot.read_bytes()

    def test_the_same_span_in_a_later_turn_diffs_against_the_earlier_one(
        self, sensor: PlaywrightSensor
    ) -> None:
        """The regression case the whole store exists for: one span, two turns."""
        before = sensor.observe(fixture_url("baseline.html"), span_id="hero", turn_id="turn-1")
        after = sensor.observe(
            fixture_url("regressed.html"),
            span_id="hero",
            turn_id="turn-2",
            baseline=before.full_page_screenshot,
        )
        assert after.pixel_diff is not None
        assert after.pixel_diff.ratio > 0.05
        assert before.full_page_screenshot.exists()
        assert before.full_page_screenshot != after.full_page_screenshot
        assert "turn-1" in str(before.full_page_screenshot)
        assert "turn-2" in str(after.full_page_screenshot)

    def test_the_baseline_is_kept_beside_the_observation_it_was_compared_against(
        self, sensor: PlaywrightSensor, tmp_path: Path
    ) -> None:
        first = sensor.observe(fixture_url("baseline.html"), span_id="kept", turn_id="turn-k")
        borrowed = tmp_path / "borrowed.png"
        borrowed.write_bytes(first.full_page_screenshot.read_bytes())
        second = sensor.observe(
            fixture_url("regressed.html"),
            span_id="kept",
            turn_id="turn-k",
            baseline=borrowed,
        )
        kept = second.pixel_diff.baseline_path
        assert kept is not None and kept.exists()
        assert kept != borrowed
        borrowed.unlink()
        assert kept.exists()


class TestPixelDiff:
    def test_no_baseline_means_no_comparison_rather_than_zero(
        self, baseline_report: SensorReport
    ) -> None:
        assert baseline_report.pixel_diff is None

    def test_a_regressed_page_moves_pixels_and_writes_a_heatmap(
        self, regressed_report: SensorReport
    ) -> None:
        diff = regressed_report.pixel_diff
        assert diff is not None
        assert diff.ratio > 0.05
        assert diff.diff_path is not None and diff.diff_path.exists()
        assert diff.diff_path.stat().st_size > 0

    def test_the_same_page_against_itself_is_quiet(
        self, sensor: PlaywrightSensor, baseline_report: SensorReport
    ) -> None:
        again = sensor.observe(
            fixture_url("baseline.html"),
            span_id="pixel-baseline-again",
            turn_id=TURN,
            baseline=baseline_report.full_page_screenshot,
        )
        assert again.pixel_diff is not None
        assert again.pixel_diff.ratio < 0.001

    def test_a_missing_baseline_is_an_error_not_a_silent_skip(
        self, sensor: PlaywrightSensor, tmp_path: Path
    ) -> None:
        with pytest.raises(SensorError, match="baseline"):
            sensor.observe(
                fixture_url("baseline.html"),
                span_id="pixel-missing-baseline",
                turn_id=TURN,
                baseline=tmp_path / "not-there.png",
            )

    def test_the_diff_is_reported_as_a_signal(self, regressed_report: SensorReport) -> None:
        (signal,) = [s for s in regressed_report.signals if s.kind == "pixel-diff"]
        assert signal.payload["ratio"] == pytest.approx(regressed_report.pixel_diff.ratio)
        assert signal.payload["diff_path"].endswith(".png")


class TestReducedMotion:
    def test_a_page_honouring_the_query_passes(self, sensor: PlaywrightSensor) -> None:
        report = sensor.observe(
            fixture_url("motion_honoured.html"), span_id="motion-ok", turn_id=TURN
        )
        assert report.reduced_motion_ok is True
        assert not [signal for signal in report.signals if signal.kind == "motion-violation"]
        (motion,) = [signal for signal in report.signals if signal.kind == "reduced-motion"]
        assert motion.payload["offenders"] == []

    def test_a_page_ignoring_the_query_fails_with_the_offender_named(
        self, sensor: PlaywrightSensor
    ) -> None:
        report = sensor.observe(
            fixture_url("motion_ignored.html"), span_id="motion-bad", turn_id=TURN
        )
        assert report.reduced_motion_ok is False
        (violation,) = [signal for signal in report.signals if signal.kind == "motion-violation"]
        assert violation.kind in FAILURE_KINDS
        assert violation.span_id == "motion-bad-pulse"
        offenders = violation.payload["offenders"]
        assert offenders and offenders[0]["duration_ms"] > 1.0

    def test_a_short_finite_animation_cannot_outrun_the_check(
        self, sensor: PlaywrightSensor
    ) -> None:
        """The false pass: a 250ms entrance is over before any settle wait ends."""
        report = sensor.observe(
            fixture_url("motion_short.html"), span_id="motion-short", turn_id=TURN
        )
        assert report.reduced_motion_ok is False
        (violation,) = [signal for signal in report.signals if signal.kind == "motion-violation"]
        assert violation.span_id == "motion-short-entrance"
        offenders = violation.payload["offenders"]
        assert any(offender["source"] == "computed-style" for offender in offenders)
        assert any(offender["duration_ms"] == pytest.approx(250.0) for offender in offenders)


class TestInteractionTrace:
    def test_no_script_means_no_trace(self, contrast_report: SensorReport) -> None:
        assert contrast_report.trace is None

    def test_a_blocking_handler_shows_up_as_dropped_frames(
        self, sensor: PlaywrightSensor
    ) -> None:
        report = sensor.observe(
            fixture_url("jank.html"),
            span_id="jank-main",
            turn_id=TURN,
            interactions=[
                {"action": "hover", "selector": "#jank"},
                {"action": "click", "selector": "#jank"},
                {"action": "wait", "ms": 200},
            ],
        )
        trace = report.trace
        assert trace is not None
        assert trace.sampled_frames > 0
        assert trace.dropped_frames >= 1
        assert trace.max_frame_gap_ms > 32.0
        (frames,) = [signal for signal in report.signals if signal.kind == "frame-timing"]
        assert frames.payload["method"] == "raf-gap-sampler"
        assert frames.payload["dropped_frames"] == trace.dropped_frames

    def test_load_time_long_tasks_are_not_charged_to_the_interaction(
        self, sensor: PlaywrightSensor
    ) -> None:
        """``buffered: true`` replays page load; the window is what makes it honest."""
        report = sensor.observe(
            fixture_url("slow_load.html"),
            span_id="slow-load-main",
            turn_id=TURN,
            interactions=[{"action": "click", "selector": "#quiet"}],
        )
        assert report.trace is not None
        assert report.trace.longtasks == 0

    def test_an_unknown_interaction_is_refused(self, sensor: PlaywrightSensor) -> None:
        with pytest.raises(SensorError, match="teleport"):
            sensor.observe(
                fixture_url("jank.html"),
                span_id="jank-main",
                turn_id=TURN,
                interactions=[{"action": "teleport", "selector": "#jank"}],
            )


class TestStyleSample:
    def test_the_render_is_reduced_to_the_numbers_the_vector_needs(
        self, contrast_report: SensorReport
    ) -> None:
        sample = contrast_report.style_sample
        assert sample is not None
        assert sample.span_id == "contrast-main"
        assert sample.viewport is not None and sample.viewport.width == 1280
        assert sample.elements
        first = sample.elements[0]
        assert first.color is not None and first.background is not None
        assert first.contrast_ratio is not None and first.contrast_ratio > 1.0
        assert first.font_size_px is not None and first.font_size_px > 0.0

    def test_the_pixel_histogram_is_in_the_domain_s_own_bin_order(
        self, contrast_report: SensorReport
    ) -> None:
        screenshot = contrast_report.style_sample.screenshot
        assert screenshot is not None
        assert len(screenshot.oklch_bins) == COLOR_BINS
        assert sum(screenshot.oklch_bins) > 0.0

    def test_a_white_page_lands_in_a_light_bin(self, contrast_report: SensorReport) -> None:
        bins = contrast_report.style_sample.screenshot.oklch_bins
        dominant = max(range(COLOR_BINS), key=lambda index: bins[index])
        assert dominant % 3 == 2

    def test_the_sample_also_reaches_the_evidence_stock(
        self, contrast_report: SensorReport
    ) -> None:
        (signal,) = [s for s in contrast_report.signals if s.kind == "style-sample"]
        assert signal.payload["element_count"] == len(contrast_report.style_sample.elements)
        assert StyleSample.parse(signal.payload["sample"]) == contrast_report.style_sample


@pytest.fixture(scope="module")
def vectors(
    sensor: PlaywrightSensor,
    contract: DirectionContract,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, StyleVector]:
    pages = tmp_path_factory.mktemp("blandness-pages")
    built: dict[str, StyleVector] = {}
    for name, stylesheet in (("generic", "generic.css"), ("styled", "styled.css")):
        url = compose_page(
            BLANDNESS / f"candidate_{name}.html",
            FIXTURES / "blandness" / stylesheet,
            pages / f"candidate_{name}.html",
        )
        report = sensor.observe(url, span_id="hero", turn_id=f"turn-{name}")
        assert report.style_sample is not None
        built[name] = StyleVector.from_sample(report.style_sample, contract=contract)
    return built


@pytest.fixture(scope="module")
def anti_corpus(contract: DirectionContract) -> list[StyleVector]:
    corpus = build_anti_corpus(
        (
            (name, StyleSample.parse(json.loads((BLANDNESS / f"{name}.json").read_text("utf-8"))))
            for name in ANTI_CORPUS_NAMES
        ),
        contract=contract,
    )
    return list(corpus.values())


class TestBlandnessEndToEnd:
    """The seam that lets the fast loop score a real render instead of a fixture."""

    def test_a_rendered_generic_candidate_scores_as_bland(
        self, vectors: dict[str, StyleVector], anti_corpus: list[StyleVector]
    ) -> None:
        generic = blandness(vectors["generic"], anti_corpus)
        styled = blandness(vectors["styled"], anti_corpus)
        assert generic < styled
        assert styled - generic > 0.25

    def test_the_separation_survives_a_real_browser(
        self, vectors: dict[str, StyleVector], anti_corpus: list[StyleVector]
    ) -> None:
        assert blandness(vectors["generic"], anti_corpus) < 0.35
        assert blandness(vectors["styled"], anti_corpus) > 0.5

    def test_the_two_renders_are_far_apart_from_each_other(
        self, vectors: dict[str, StyleVector]
    ) -> None:
        from ui_servo.domain.variant import distance

        assert distance(vectors["generic"], vectors["styled"]) > 0.4


class TestSignalsFeedTheStock:
    def test_every_signal_is_a_harness_observation_of_a_known_span(
        self, contrast_report: SensorReport
    ) -> None:
        known = {contrast_report.span_id, *contrast_report.span_screenshots}
        assert contrast_report.signals
        for signal in contrast_report.signals:
            assert isinstance(signal, Signal)
            assert signal.source == "harness"
            assert signal.turn_id == TURN
            assert signal.span_id in known
            assert signal.observed_at is not None

    def test_the_report_is_restated_as_joinable_evidence(
        self, contrast_report: SensorReport
    ) -> None:
        spans = spans_in_turn(contrast_report.signals, TURN)
        root = spans["contrast-main"]
        assert root.key == (TURN, "contrast-main")
        assert root.sources == {"harness"}
        assert {"screenshot", "aria-snapshot", "reduced-motion", "style-sample"} <= root.kinds
        offender = spans["contrast-faint"]
        assert offender.has_failure(FAILURE_KINDS)
        assert not root.has_failure(FAILURE_KINDS)
