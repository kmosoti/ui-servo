"""The fast loop, and the one invariant it exists to hold.

Two things are under test here and they are not the same thing.

The first is the bar: every hard gate has a failure path, and each failure path must
trip ``passed`` and name *itself* while leaving the other gates alone. A gate that
fails without saying which one it was is a gate a builder cannot fix.

The second is the separation of gates from taste, which is the property the whole
design is arranged around. It is asserted three ways, because one way is an
assertion about today's code and three ways are an assertion about the shape:
structurally (``passed`` is a derived property, not a field anyone can set),
behaviourally (the same rendering scored against a different anti-corpus produces a
different blandness and an identical verdict; the same fragment with a broken gate
produces an identical style vector), and lexically (no gate name is reachable from
anything on the taste side).

Everything runs against two fakes. That is the point of the ports: the fast loop is
a pure function of a sanitiser verdict, a sensor report and a bundle of probe
signals, so testing it needs no browser, no network and no clock.
"""

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import Signal, SpanEvidence
from ui_servo.domain.variant import (
    BLOCK_NAMES,
    DIMENSIONS,
    StyleSample,
    StyleVector,
    Variant,
    blandness,
    build_anti_corpus,
    distance,
    nearest_template,
)
from ui_servo.ports.sanitizer import SanitizeResult, Violation, ViolationKind
from ui_servo.ports.sensor import PixelDiff, SensorReport, TraceSummary
from ui_servo.control.regulator import (
    DEFAULT_AXE_TAGS,
    FAILURE_KINDS,
    GATE_AXE_CLEAN,
    GATE_KIND,
    GATE_MOTION_CONFORMS,
    GATE_NO_OVERFLOW,
    GATE_NO_RUNTIME_ERRORS,
    GATE_NO_UNKNOWN_CLASS,
    GATE_REDUCED_MOTION,
    GATE_SANITIZED,
    REQUIRED_GATES,
    RENDER_DEPENDENT_GATES,
    RUNTIME_ERROR_KINDS,
    SANITIZE_KIND,
    TASTE_FIELDS,
    GateResult,
    Regulator,
    RegulatorReport,
    with_anti_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "blandness"
ANTI_CORPUS_NAMES = ("generic_shadcn", "generic_bootstrap", "generic_saas_landing")

TURN = "t-0001"
SPAN = "hero"
URL = "http://127.0.0.1:8700/candidate/hero"
FROZEN_CLOCK = "2026-08-06T09:00:00+00:00"


# --------------------------------------------------------------------------- #
# Fixtures and fakes                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(
        (REPO_ROOT / "direction" / "direction.toml").read_text(encoding="utf-8")
    )


def load_sample(name: str) -> StyleSample:
    return StyleSample.parse(json.loads((FIXTURES / f"{name}.json").read_text("utf-8")))


@pytest.fixture(scope="session")
def anti_corpus(contract: DirectionContract) -> dict[str, StyleVector]:
    return build_anti_corpus(
        ((name, load_sample(name)) for name in ANTI_CORPUS_NAMES), contract=contract
    )


class StubSanitizer:
    """A :class:`SanitizerPort` that answers however the test needs, and remembers."""

    def __init__(self, *violations: Violation) -> None:
        self.violations = violations
        self.seen: list[str] = []

    def check(self, fragment_html: str) -> SanitizeResult:
        self.seen.append(fragment_html)
        if not self.violations:
            return SanitizeResult.ok(fragment_html)
        return SanitizeResult.rejected(self.violations)


class StubSensor:
    """A :class:`SensorPort` returning a canned report, recording how it was called."""

    def __init__(
        self,
        *,
        signals: Sequence[Signal] = (),
        axe_violations: Sequence[dict[str, Any]] = (),
        reduced_motion_ok: bool = True,
    ) -> None:
        self.signals = tuple(signals)
        self.axe_violations = tuple(axe_violations)
        self.reduced_motion_ok = reduced_motion_ok
        self.calls: list[dict[str, Any]] = []

    def observe(
        self,
        url: str,
        *,
        span_id: str,
        turn_id: str,
        baseline: Path | None = None,
        interactions: Sequence[dict[str, Any]] | None = None,
    ) -> SensorReport:
        self.calls.append(
            {
                "url": url,
                "span_id": span_id,
                "turn_id": turn_id,
                "baseline": baseline,
                "interactions": interactions,
            }
        )
        return SensorReport(
            span_id=span_id,
            turn_id=turn_id,
            url=url,
            screenshots={"full": Path("/tmp/full.png")},
            aria_snapshot="- heading \"Kennedy Mosoti\" [level=1]",
            axe_violations=self.axe_violations,
            pixel_diff=PixelDiff(ratio=0.0),
            trace=TraceSummary(dropped_frames=0, longtasks=0, sampled_frames=120),
            reduced_motion_ok=self.reduced_motion_ok,
            signals=self.signals,
        )


def probe_signal(kind: str, **payload: Any) -> Signal:
    return Signal(
        span_id=SPAN,
        turn_id=TURN,
        source="probe",
        kind=kind,
        ts=FROZEN_CLOCK,
        payload=payload,
    )


def axe_violation(rule: str, *tags: str) -> dict[str, Any]:
    return {
        "id": rule,
        "impact": "serious",
        "tags": list(tags),
        "help": f"{rule} must be fixed",
        "nodes": [{"target": ["section > p"]}],
    }


VARIANT = Variant(
    variant_id=SPAN,
    part="hero",
    html='<section data-span-id="hero"><h1 class="display">Kennedy Mosoti</h1></section>',
    builder_family="claude",
)


def make_regulator(
    contract: DirectionContract,
    *,
    sanitizer: StubSanitizer | None = None,
    sensor: StubSensor | None = None,
    anti_corpus: Sequence[StyleVector] = (),
) -> Regulator:
    return Regulator(
        sanitizer=sanitizer or StubSanitizer(),
        sensor=sensor or StubSensor(),
        contract=contract,
        anti_corpus=tuple(anti_corpus),
        now=lambda: FROZEN_CLOCK,
    )


def regulate(regulator: Regulator, **kwargs: Any) -> RegulatorReport:
    return regulator.regulate(VARIANT, url=URL, turn_id=TURN, **kwargs)


# --------------------------------------------------------------------------- #
# The clean path                                                               #
# --------------------------------------------------------------------------- #


class TestCleanPass:
    def test_a_clean_variant_passes_every_gate(self, contract: DirectionContract) -> None:
        report = regulate(make_regulator(contract))
        assert report.passed
        assert report.failed_gate_names == ()
        assert all(gate.evaluated for gate in report.gates)

    def test_the_whole_bar_is_always_present_and_ordered(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(make_regulator(contract))
        assert tuple(gate.name for gate in report.gates) == REQUIRED_GATES

    def test_the_sensor_is_told_what_to_attribute_the_observation_to(
        self, contract: DirectionContract
    ) -> None:
        sensor = StubSensor()
        regulate(make_regulator(contract, sensor=sensor))
        (call,) = sensor.calls
        assert call["span_id"] == VARIANT.span_id
        assert call["turn_id"] == TURN
        assert call["url"] == URL

    def test_an_explicit_span_id_overrides_the_variant_id(
        self, contract: DirectionContract
    ) -> None:
        sensor = StubSensor()
        regulate(make_regulator(contract, sensor=sensor), span_id="hero-b")
        assert sensor.calls[0]["span_id"] == "hero-b"

    def test_baseline_and_interactions_reach_the_sensor_untouched(
        self, contract: DirectionContract
    ) -> None:
        sensor = StubSensor()
        interactions = [{"action": "hover", "selector": "a.link-accent"}]
        regulate(
            make_regulator(contract, sensor=sensor),
            baseline=Path("/tmp/baseline.png"),
            interactions=interactions,
        )
        assert sensor.calls[0]["baseline"] == Path("/tmp/baseline.png")
        assert sensor.calls[0]["interactions"] == interactions


# --------------------------------------------------------------------------- #
# One failure path per gate                                                    #
# --------------------------------------------------------------------------- #


class TestGateFailurePaths:
    def test_sanitizer_rejection_trips_its_own_gate(
        self, contract: DirectionContract
    ) -> None:
        sanitizer = StubSanitizer(
            Violation(
                kind=ViolationKind.DISALLOWED_TAG,
                locator="section > script",
                detail="script is outside the hypermedia policy",
            )
        )
        report = regulate(make_regulator(contract, sanitizer=sanitizer))
        assert not report.passed
        assert GATE_SANITIZED in report.failed_gate_names
        assert "script" in report.gate(GATE_SANITIZED).detail

    def test_a_refused_fragment_never_reaches_the_browser(
        self, contract: DirectionContract
    ) -> None:
        sanitizer = StubSanitizer(
            Violation(
                kind=ViolationKind.MALFORMED_FRAGMENT,
                locator=":root",
                detail="unbalanced fragment",
            )
        )
        sensor = StubSensor()
        regulate(make_regulator(contract, sanitizer=sanitizer, sensor=sensor))
        assert sensor.calls == []

    def test_gates_that_could_not_run_are_recorded_as_not_evaluated_and_not_passed(
        self, contract: DirectionContract
    ) -> None:
        sanitizer = StubSanitizer(
            Violation(
                kind=ViolationKind.DISALLOWED_URL,
                locator="a",
                detail="off-origin hx-get",
            )
        )
        report = regulate(make_regulator(contract, sanitizer=sanitizer))
        for name in RENDER_DEPENDENT_GATES:
            gate = report.gate(name)
            assert not gate.evaluated
            assert not gate.passed
            assert "never rendered" in gate.detail

    def test_unknown_class_from_the_sanitizer_trips_the_unknown_class_gate(
        self, contract: DirectionContract
    ) -> None:
        sanitizer = StubSanitizer(
            Violation(
                kind=ViolationKind.UNKNOWN_CLASS,
                locator="section > div",
                detail="no token justifies 'rounded-xl'",
            )
        )
        report = regulate(make_regulator(contract, sanitizer=sanitizer))
        assert GATE_NO_UNKNOWN_CLASS in report.failed_gate_names
        assert report.gate(GATE_NO_UNKNOWN_CLASS).evaluated
        assert report.gate(GATE_NO_UNKNOWN_CLASS).offenders[0]["source"] == "sanitizer"

    def test_unknown_class_from_the_probe_trips_the_same_gate(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(
            make_regulator(contract),
            probe_signals=[probe_signal("unknown-class", classes=[{"name": "shadow-lg"}])],
        )
        assert GATE_NO_UNKNOWN_CLASS in report.failed_gate_names
        assert report.gate(GATE_NO_UNKNOWN_CLASS).offenders[0]["source"] == "probe"

    @pytest.mark.parametrize("kind", sorted(RUNTIME_ERROR_KINDS))
    def test_every_runtime_error_kind_trips_the_runtime_error_gate(
        self, contract: DirectionContract, kind: str
    ) -> None:
        report = regulate(
            make_regulator(contract), probe_signals=[probe_signal(kind, message="boom")]
        )
        assert not report.passed
        assert report.failed_gate_names == (GATE_NO_RUNTIME_ERRORS,)
        assert kind in report.gate(GATE_NO_RUNTIME_ERRORS).detail

    def test_overflow_trips_only_the_overflow_gate(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(
            make_regulator(contract),
            probe_signals=[probe_signal("overflow", offenders=[{"overflowBy": 42}])],
        )
        assert report.failed_gate_names == (GATE_NO_OVERFLOW,)
        assert report.gate(GATE_NO_OVERFLOW).offenders[0]["kind"] == "overflow"

    def test_motion_violation_trips_only_the_motion_gate(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(
            make_regulator(contract),
            probe_signals=[probe_signal("motion-violation", duration=900, easing="linear")],
        )
        assert report.failed_gate_names == (GATE_MOTION_CONFORMS,)

    def test_a_harness_motion_violation_counts_too(
        self, contract: DirectionContract
    ) -> None:
        harness = Signal(
            span_id=SPAN,
            turn_id=TURN,
            source="harness",
            kind="motion-violation",
            ts=FROZEN_CLOCK,
            payload={"duration": 900},
        )
        report = regulate(make_regulator(contract, sensor=StubSensor(signals=[harness])))
        assert report.failed_gate_names == (GATE_MOTION_CONFORMS,)

    def test_an_axe_violation_at_the_gated_level_trips_the_axe_gate(
        self, contract: DirectionContract
    ) -> None:
        sensor = StubSensor(axe_violations=[axe_violation("color-contrast", "wcag2aa", "cat.color")])
        report = regulate(make_regulator(contract, sensor=sensor))
        assert report.failed_gate_names == (GATE_AXE_CLEAN,)
        assert "color-contrast" in report.gate(GATE_AXE_CLEAN).detail

    def test_a_violation_outside_the_gated_level_does_not_trip_it(
        self, contract: DirectionContract
    ) -> None:
        sensor = StubSensor(axe_violations=[axe_violation("region", "best-practice")])
        report = regulate(make_regulator(contract, sensor=sensor))
        assert report.passed

    def test_an_untagged_violation_fails_closed(self, contract: DirectionContract) -> None:
        sensor = StubSensor(axe_violations=[axe_violation("mystery-rule")])
        report = regulate(make_regulator(contract, sensor=sensor))
        assert report.failed_gate_names == (GATE_AXE_CLEAN,)

    def test_reduced_motion_not_honoured_trips_its_gate(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(make_regulator(contract, sensor=StubSensor(reduced_motion_ok=False)))
        assert report.failed_gate_names == (GATE_REDUCED_MOTION,)

    def test_several_failures_are_all_named(self, contract: DirectionContract) -> None:
        sensor = StubSensor(
            axe_violations=[axe_violation("color-contrast", "wcag2aa")],
            reduced_motion_ok=False,
        )
        report = regulate(
            make_regulator(contract, sensor=sensor),
            probe_signals=[probe_signal("overflow"), probe_signal("js-error", message="x")],
        )
        assert set(report.failed_gate_names) == {
            GATE_NO_RUNTIME_ERRORS,
            GATE_NO_OVERFLOW,
            GATE_AXE_CLEAN,
            GATE_REDUCED_MOTION,
        }

    def test_every_required_gate_has_a_demonstrated_failure_path(self) -> None:
        """The bar is only real if each rung has been shown to break."""
        demonstrated = {
            GATE_SANITIZED,
            GATE_NO_RUNTIME_ERRORS,
            GATE_NO_UNKNOWN_CLASS,
            GATE_NO_OVERFLOW,
            GATE_MOTION_CONFORMS,
            GATE_AXE_CLEAN,
            GATE_REDUCED_MOTION,
        }
        assert demonstrated == set(REQUIRED_GATES)


# --------------------------------------------------------------------------- #
# Evidence                                                                     #
# --------------------------------------------------------------------------- #


class TestEvidence:
    def test_the_span_holds_probe_sensor_and_gate_signals_together(
        self, contract: DirectionContract
    ) -> None:
        harness = Signal(
            span_id=SPAN, turn_id=TURN, source="harness", kind="screenshot", ts=FROZEN_CLOCK
        )
        report = regulate(
            make_regulator(contract, sensor=StubSensor(signals=[harness])),
            probe_signals=[probe_signal("swap-ok", event="afterSwap")],
        )
        span = report.evidence_span
        assert isinstance(span, SpanEvidence)
        assert span.key == (TURN, SPAN)
        assert span.sources == {"probe", "harness", "gate"}

    def test_a_gate_signal_is_written_for_every_gate_pass_or_fail(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(make_regulator(contract))
        gate_signals = report.evidence_span.of_kind(GATE_KIND)
        assert len(gate_signals) == len(REQUIRED_GATES)
        assert {signal.payload["gate"] for signal in gate_signals} == set(REQUIRED_GATES)
        assert all(signal.payload["passed"] for signal in gate_signals)

    def test_the_sanitizer_verdict_lands_in_the_stock(
        self, contract: DirectionContract
    ) -> None:
        sanitizer = StubSanitizer(
            Violation(
                kind=ViolationKind.MISSING_SPAN_ID,
                locator=":root",
                detail="no data-span-id",
            )
        )
        report = regulate(make_regulator(contract, sanitizer=sanitizer))
        (signal,) = report.evidence_span.of_kind(SANITIZE_KIND)
        assert signal.source == "gate"
        assert signal.payload["accepted"] is False
        assert signal.payload["kinds"] == ["missing-span-id"]

    def test_the_failure_vocabulary_the_gates_route_on_is_stated_once(self) -> None:
        report_kinds = {"overflow", "motion-violation", "unknown-class"} | RUNTIME_ERROR_KINDS
        assert FAILURE_KINDS == report_kinds
        assert GATE_KIND not in FAILURE_KINDS
        assert SANITIZE_KIND not in FAILURE_KINDS

    def test_the_fast_loop_vocabulary_is_a_subset_of_the_governance_vocabulary(
        self,
    ) -> None:
        """Regression for the deleted consolidation TODO: this module's failure
        vocabulary must never name a kind that `ui_servo.domain.policy` does not
        also recognise, or the two "departure" concepts would have silently
        diverged. The reverse containment is not asserted -- policy carries kinds
        (axe-violation, the sanitizer kinds, layout-shift, judge-error) that have
        their own gates and are deliberately absent here; see the comment above
        `FAILURE_KINDS` in `ui_servo/control/regulator.py`.
        """
        from ui_servo.domain.policy import FAILURE_KINDS as POLICY_FAILURE_KINDS

        assert FAILURE_KINDS <= POLICY_FAILURE_KINDS


# --------------------------------------------------------------------------- #
# Report construction                                                          #
# --------------------------------------------------------------------------- #


def _full_gates(**overrides: bool) -> tuple[GateResult, ...]:
    return tuple(
        GateResult(name=name, passed=overrides.get(name, True)) for name in REQUIRED_GATES
    )


def _empty_span() -> SpanEvidence:
    return SpanEvidence(span_id=SPAN, turn_id=TURN, signals=())


class TestReportConstruction:
    def test_a_short_gate_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="missing gates"):
            RegulatorReport(
                variant_id=SPAN,
                gates=(GateResult.ok(GATE_SANITIZED),),
                evidence_span=_empty_span(),
            )

    def test_duplicate_gate_results_are_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate gate"):
            RegulatorReport(
                variant_id=SPAN,
                gates=(*_full_gates(), GateResult.ok(GATE_SANITIZED)),
                evidence_span=_empty_span(),
            )

    def test_blandness_outside_the_cosine_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cosine distance"):
            RegulatorReport(
                variant_id=SPAN,
                gates=_full_gates(),
                evidence_span=_empty_span(),
                blandness_score=1.5,
            )

    def test_a_gate_cannot_pass_without_being_evaluated(self) -> None:
        with pytest.raises(ValueError, match="without being evaluated"):
            GateResult(name=GATE_AXE_CLEAN, passed=True, evaluated=False)

    def test_an_unknown_gate_name_is_a_key_error(self) -> None:
        report = RegulatorReport(
            variant_id=SPAN, gates=_full_gates(), evidence_span=_empty_span()
        )
        with pytest.raises(KeyError):
            report.gate("vibes-check")


# --------------------------------------------------------------------------- #
# The taste separation                                                         #
# --------------------------------------------------------------------------- #


class TestGateTasteSeparation:
    def test_passed_is_not_a_field_anyone_can_set(self) -> None:
        names = {field.name for field in dataclasses.fields(RegulatorReport)}
        assert "passed" not in names
        assert names == {"variant_id", "gates", "evidence_span", *TASTE_FIELDS}

    def test_passed_cannot_be_assigned_after_the_fact(self) -> None:
        report = RegulatorReport(
            variant_id=SPAN, gates=_full_gates(), evidence_span=_empty_span()
        )
        with pytest.raises(AttributeError):
            report.passed = False  # type: ignore[misc]

    def test_passed_is_exactly_the_conjunction_of_the_gates(self) -> None:
        assert RegulatorReport(
            variant_id=SPAN, gates=_full_gates(), evidence_span=_empty_span()
        ).passed
        for name in REQUIRED_GATES:
            broken = RegulatorReport(
                variant_id=SPAN,
                gates=_full_gates(**{name: False}),
                evidence_span=_empty_span(),
            )
            assert not broken.passed
            assert broken.failed_gate_names == (name,)

    def test_a_maximally_bland_variant_still_passes_the_gates(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        regulator = make_regulator(contract, anti_corpus=list(anti_corpus.values()))
        report = regulate(regulator, style_sample=load_sample("candidate_generic"))
        assert report.blandness_score is not None
        assert report.blandness_score < 0.1
        assert report.passed

    def test_a_distinctive_variant_with_a_broken_gate_still_fails(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        regulator = make_regulator(
            contract,
            sensor=StubSensor(axe_violations=[axe_violation("color-contrast", "wcag2aa")]),
            anti_corpus=list(anti_corpus.values()),
        )
        report = regulate(regulator, style_sample=load_sample("candidate_styled"))
        assert report.blandness_score is not None
        assert report.blandness_score > 0.5
        assert not report.passed

    def test_moving_the_anti_corpus_moves_taste_and_never_the_verdict(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        sample = load_sample("candidate_styled")
        base = make_regulator(contract, anti_corpus=list(anti_corpus.values()))
        rival = with_anti_corpus(
            base, [StyleVector.from_sample(sample, contract=contract)]
        )
        first = regulate(base, style_sample=sample)
        second = regulate(rival, style_sample=sample)
        assert first.blandness_score != second.blandness_score
        assert second.blandness_score == pytest.approx(0.0, abs=1e-9)
        assert first.passed == second.passed is True

    def test_breaking_a_gate_never_moves_the_style_vector_or_the_blandness(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        sample = load_sample("candidate_styled")
        corpus = list(anti_corpus.values())
        clean = regulate(
            make_regulator(contract, anti_corpus=corpus), style_sample=sample
        )
        broken = regulate(
            make_regulator(
                contract,
                sensor=StubSensor(reduced_motion_ok=False),
                anti_corpus=corpus,
            ),
            style_sample=sample,
            probe_signals=[probe_signal("overflow"), probe_signal("js-error", message="x")],
        )
        assert clean.passed and not broken.passed
        assert clean.style_vector == broken.style_vector
        assert clean.blandness_score == broken.blandness_score

    def test_no_gate_name_is_reachable_from_the_taste_side(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        report = regulate(
            make_regulator(contract, anti_corpus=list(anti_corpus.values())),
            style_sample=load_sample("candidate_styled"),
        )
        taste = {name: getattr(report, name) for name in TASTE_FIELDS}
        rendered = repr(taste)
        for gate in REQUIRED_GATES:
            assert gate not in rendered
        assert set(DIMENSIONS).isdisjoint(REQUIRED_GATES)
        assert set(BLOCK_NAMES).isdisjoint(REQUIRED_GATES)

    def test_taste_is_optional_and_its_absence_changes_no_gate(
        self, contract: DirectionContract
    ) -> None:
        without = regulate(make_regulator(contract))
        assert without.style_vector is None
        assert without.blandness_score is None
        assert without.passed

    def test_a_style_sample_without_a_corpus_yields_a_vector_and_no_score(
        self, contract: DirectionContract
    ) -> None:
        report = regulate(
            make_regulator(contract), style_sample=load_sample("candidate_styled")
        )
        assert report.style_vector is not None
        assert report.blandness_score is None

    def test_the_summary_states_the_verdict_and_the_taste_separately(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        report = regulate(
            make_regulator(
                contract,
                sensor=StubSensor(reduced_motion_ok=False),
                anti_corpus=list(anti_corpus.values()),
            ),
            style_sample=load_sample("candidate_styled"),
        )
        summary = report.summary()
        assert "FAIL" in summary
        assert GATE_REDUCED_MOTION in summary
        assert "blandness" in summary


# --------------------------------------------------------------------------- #
# The style vector                                                             #
# --------------------------------------------------------------------------- #


class TestStyleVector:
    def test_the_embedding_has_a_fixed_shape_and_unit_norm(
        self, contract: DirectionContract
    ) -> None:
        vector = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        assert len(vector.values) == len(DIMENSIONS)
        assert sum(value * value for value in vector.values) == pytest.approx(1.0)
        assert all(value >= 0.0 for value in vector.values)

    def test_the_embedding_is_deterministic(self, contract: DirectionContract) -> None:
        first = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        second = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        assert first == second
        assert first.values == second.values

    def test_a_vector_of_the_wrong_length_is_refused(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            StyleVector(values=(1.0, 0.0))

    def test_distance_is_zero_to_itself_and_symmetric(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        generic = StyleVector.from_sample(load_sample("candidate_generic"), contract=contract)
        shadcn = anti_corpus["generic_shadcn"]
        assert distance(generic, generic) == pytest.approx(0.0, abs=1e-9)
        assert distance(generic, shadcn) == pytest.approx(distance(shadcn, generic))
        assert 0.0 <= distance(generic, shadcn) <= 1.0

    def test_the_generic_template_is_blander_than_the_styled_one(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        """The headline property: bland means close to the corpus."""
        corpus = list(anti_corpus.values())
        generic = StyleVector.from_sample(load_sample("candidate_generic"), contract=contract)
        styled = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        assert blandness(generic, corpus) < blandness(styled, corpus)
        assert blandness(generic, corpus) < 0.1
        assert blandness(styled, corpus) > 0.5

    def test_the_anti_corpus_members_are_bland_by_their_own_measure(
        self, anti_corpus: dict[str, StyleVector]
    ) -> None:
        corpus = list(anti_corpus.values())
        for member in corpus:
            assert blandness(member, corpus) == pytest.approx(0.0, abs=1e-9)

    def test_the_nearest_template_is_named_not_just_scored(
        self, contract: DirectionContract, anti_corpus: dict[str, StyleVector]
    ) -> None:
        generic = StyleVector.from_sample(load_sample("candidate_generic"), contract=contract)
        name, score = nearest_template(generic, anti_corpus)
        assert name in anti_corpus
        assert score == pytest.approx(blandness(generic, list(anti_corpus.values())))

    def test_an_empty_corpus_is_an_error_rather_than_a_sentinel(
        self, contract: DirectionContract
    ) -> None:
        vector = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        with pytest.raises(ValueError, match="anti-corpus"):
            blandness(vector, [])
        with pytest.raises(ValueError, match="anti-corpus"):
            nearest_template(vector, {})

    def test_the_styled_candidate_lands_on_the_contract_scales(
        self, contract: DirectionContract
    ) -> None:
        """Quantisation against the contract is what makes off-scale measurable."""
        styled = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        generic = StyleVector.from_sample(load_sample("candidate_generic"), contract=contract)
        labels = dict(zip(DIMENSIONS, range(len(DIMENSIONS)), strict=True))
        off_scale = (
            labels["spacing.off-scale-small"],
            labels["spacing.off-scale-large"],
            labels["type.off-scale-small"],
            labels["type.off-scale-large"],
        )
        assert all(styled.values[index] == 0.0 for index in off_scale)
        assert any(generic.values[index] > 0.0 for index in off_scale)

    def test_an_empty_block_reads_as_no_opinion_rather_than_maximal_difference(
        self, contract: DirectionContract
    ) -> None:
        bare = StyleVector.from_sample(StyleSample(), contract=contract)
        assert sum(value * value for value in bare.values) == pytest.approx(1.0)
        assert all(value > 0.0 for value in bare.values)

    def test_blocks_are_addressable_and_partition_the_vector(
        self, contract: DirectionContract
    ) -> None:
        vector = StyleVector.from_sample(load_sample("candidate_styled"), contract=contract)
        rejoined = tuple(
            value for name in BLOCK_NAMES for value in vector.block(name)
        )
        assert rejoined == vector.values
        with pytest.raises(KeyError):
            vector.block("mood")

    def test_the_screenshot_histogram_is_optional(
        self, contract: DirectionContract
    ) -> None:
        raw = json.loads((FIXTURES / "candidate_styled.json").read_text("utf-8"))
        del raw["screenshot"]
        without = StyleVector.from_sample(StyleSample.parse(raw), contract=contract)
        with_pixels = StyleVector.from_sample(
            load_sample("candidate_styled"), contract=contract
        )
        assert without != with_pixels
        assert distance(without, with_pixels) < 0.2

    def test_a_malformed_histogram_is_refused(self, contract: DirectionContract) -> None:
        raw = json.loads((FIXTURES / "candidate_styled.json").read_text("utf-8"))
        raw["screenshot"]["oklch_bins"] = [1.0, 2.0]
        with pytest.raises(ValueError, match="24 bins"):
            StyleSample.parse(raw)

    def test_every_fixture_pairs_html_with_a_sample(self) -> None:
        for name in (*ANTI_CORPUS_NAMES, "candidate_generic", "candidate_styled"):
            assert (FIXTURES / f"{name}.html").is_file()
            assert (FIXTURES / f"{name}.json").is_file()


class TestVariant:
    def test_a_variant_is_inert_and_immutable(self) -> None:
        assert VARIANT.span_id == VARIANT.variant_id
        with pytest.raises(ValueError):
            VARIANT.html = "<div></div>"  # type: ignore[misc]

    def test_a_variant_carries_no_verdict_and_no_score(self) -> None:
        fields = set(Variant.model_fields)
        assert fields == {"variant_id", "part", "html", "builder_family", "cell_hint"}
        assert fields.isdisjoint(TASTE_FIELDS)
        assert fields.isdisjoint(REQUIRED_GATES)

    def test_the_cell_hint_is_a_hint_and_defaults_to_absent(self) -> None:
        assert VARIANT.cell_hint is None
        aimed = Variant(
            variant_id="hero-2",
            part="hero",
            html="<section data-span-id='hero-2'></section>",
            builder_family="codex",
            cell_hint=(2, 0, 1),
        )
        assert aimed.cell_hint == (2, 0, 1)


def test_the_gated_conformance_level_is_stated_not_assumed() -> None:
    assert DEFAULT_AXE_TAGS == frozenset({"wcag2aa"})
