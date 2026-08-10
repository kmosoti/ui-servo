"""The fast homeostatic loop: one variant, all the cheap comparators, one verdict.

This is the inner loop of the servo. It takes a single candidate fragment and runs
it through every measurement that is deterministic and model-free -- parse it
(latency class 0), render and audit it (latency class 2), read whatever the in-page
probe already reported about it (latency class 1) -- and reduces the whole
observation to a fixed list of hard gates. Nothing here has an opinion. Every gate
is a predicate over evidence that a second implementation would compute the same way.

**The gate/taste separation is the invariant this module exists to hold.**
:attr:`RegulatorReport.passed` is derived from :attr:`RegulatorReport.gates` and from
nothing else -- it is a property, not a field, so there is no assignment that could
smuggle a score into it. The style vector and the blandness score ride in their own
fields, are computed from a source the gates never touch, and are never consulted.
That separation is what keeps two different failures separable: a fragment with a
contrast violation is *broken* and is rejected here; a fragment that looks like every
other landing page is *median* and survives to be argued about by the slow loop.
Fuse them and the loop starts trading correctness for novelty, which is the specific
way this kind of system Goodharts itself.

The gates fail closed. A fragment the sanitiser refused never renders, so the gates
that need a render are recorded as *not evaluated* -- and a gate that did not run is
not a gate that passed. Recording them as absent instead would let a rejection
silently shrink the bar.

Pure control: standard library plus the domain and the ports. The browser, the
sanitiser and the clock all arrive through parameters, so the whole loop runs against
two ten-line fakes in a unit test.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import Signal, SpanEvidence, SpanId, TurnId
from ui_servo.domain.variant import (
    StyleSample,
    StyleVector,
    Variant,
    blandness,
)
from ui_servo.ports.sanitizer import SanitizeResult, SanitizerPort, ViolationKind
from ui_servo.ports.sensor import AxeViolation, Interaction, SensorPort, SensorReport

type GateName = str
type SignalKind = str

# --------------------------------------------------------------------------- #
# Failure vocabulary                                                           #
# --------------------------------------------------------------------------- #
#
# Finding (batch, 2026-08-09): NOT a mechanical consolidation into
# `ui_servo.domain.policy`. This module's FAILURE_KINDS (10 kinds: the 7 runtime-error
# kinds plus unknown-class, overflow and motion-violation) is a strict subset of
# `policy.FAILURE_KINDS` (20 kinds). Policy additionally carries axe-violation, the
# sanitizer kinds (malformed-fragment, disallowed-tag, disallowed-attribute,
# disallowed-url, missing-span-id, unknown-attribute, invalid-attribute-value),
# layout-shift and judge-error -- kinds that each already have their own gate
# elsewhere in the pipeline. Substituting policy's set here would double-count those
# and silently widen this gate. The two vocabularies are intentionally different
# granularities: this one is fast-loop gates, policy's is the governance-wide
# canonical list. Kept local rather than defaulted inside `ui_servo.domain.evidence`,
# which deliberately holds no opinion about which kinds are failures -- that is
# policy, and policy moves on its own clock.
#
# `tests/test_regulator.py` enforces `FAILURE_KINDS <= policy.FAILURE_KINDS` so this
# claim cannot silently rot, but the reverse is a judgement call, not a bug: if a new
# runtime failure kind is added to policy's `CORRECTNESS_FAILURE_KINDS`, decide
# separately whether this fast-loop gate should also carry it -- the subset test will
# not force the question.

RUNTIME_ERROR_KINDS: Final[frozenset[SignalKind]] = frozenset(
    {"js-error", "rejection", "csp", "swap-error", "ce-error", "wasm-error", "wasm-panic"}
)
"""The probe's error-class kinds: the page told us it broke, in its own words."""

UNKNOWN_CLASS_KIND: Final[SignalKind] = "unknown-class"
OVERFLOW_KIND: Final[SignalKind] = "overflow"
MOTION_VIOLATION_KIND: Final[SignalKind] = "motion-violation"

FAILURE_KINDS: Final[frozenset[SignalKind]] = RUNTIME_ERROR_KINDS | {
    UNKNOWN_CLASS_KIND,
    OVERFLOW_KIND,
    MOTION_VIOLATION_KIND,
}
"""Every signal kind this regulator treats as a departure from the reference signal."""

DEFAULT_AXE_TAGS: Final[frozenset[str]] = frozenset({"wcag2aa"})
"""The conformance level the accessibility gate is set at.

A violation carrying no tags at all still counts: an audit rule the engine could not
classify is a fact we cannot dismiss, and the gate fails closed.
"""

SANITIZE_KIND: Final[SignalKind] = "sanitize"
GATE_KIND: Final[SignalKind] = "gate"

GATE_SANITIZED: Final[GateName] = "sanitizer-accepted"
GATE_NO_RUNTIME_ERRORS: Final[GateName] = "no-runtime-errors"
GATE_NO_UNKNOWN_CLASS: Final[GateName] = "no-unknown-class"
GATE_NO_OVERFLOW: Final[GateName] = "no-overflow"
GATE_MOTION_CONFORMS: Final[GateName] = "no-motion-violation"
GATE_AXE_CLEAN: Final[GateName] = "axe-clean"
GATE_REDUCED_MOTION: Final[GateName] = "reduced-motion-honoured"

REQUIRED_GATES: Final[tuple[GateName, ...]] = (
    GATE_SANITIZED,
    GATE_NO_RUNTIME_ERRORS,
    GATE_NO_UNKNOWN_CLASS,
    GATE_NO_OVERFLOW,
    GATE_MOTION_CONFORMS,
    GATE_AXE_CLEAN,
    GATE_REDUCED_MOTION,
)
"""The whole bar, fixed in one place.

A report must carry a result for every one of these. Letting a caller assemble a
short list would make "we did not check" and "there was nothing to check"
indistinguishable, and the bar would erode a gate at a time without a diff.
"""

RENDER_DEPENDENT_GATES: Final[tuple[GateName, ...]] = (
    GATE_NO_RUNTIME_ERRORS,
    GATE_NO_OVERFLOW,
    GATE_MOTION_CONFORMS,
    GATE_AXE_CLEAN,
    GATE_REDUCED_MOTION,
)
"""Gates with nothing to say about a fragment that never reached a browser."""

TASTE_FIELDS: Final[frozenset[str]] = frozenset({"style_vector", "blandness_score"})
"""Where taste is allowed to live on a report. Deliberately disjoint from the gates."""

GATE_FIELDS: Final[frozenset[str]] = frozenset({"gates"})


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class GateResult:
    """One hard comparator's answer, with the evidence that produced it.

    ``evaluated`` is separate from ``passed`` because they answer different
    questions, and collapsing them is how a skipped check turns into a silent pass.
    A gate that never ran is always ``passed=False``; the distinction survives so a
    reader can tell "this fragment overflows" from "this fragment never rendered".
    """

    name: GateName
    passed: bool
    detail: str = ""
    evaluated: bool = True
    offenders: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a gate result must name its gate")
        if not self.evaluated and self.passed:
            raise ValueError(f"gate {self.name!r} cannot pass without being evaluated")

    def __bool__(self) -> bool:
        return self.passed

    @classmethod
    def ok(cls, name: GateName, detail: str = "") -> GateResult:
        return cls(name=name, passed=True, detail=detail)

    @classmethod
    def failed(
        cls,
        name: GateName,
        detail: str,
        offenders: Sequence[Mapping[str, Any]] = (),
    ) -> GateResult:
        return cls(
            name=name,
            passed=False,
            detail=detail,
            offenders=tuple(dict(offender) for offender in offenders),
        )

    @classmethod
    def not_evaluated(cls, name: GateName, reason: str) -> GateResult:
        return cls(name=name, passed=False, detail=reason, evaluated=False)

    def as_payload(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "passed": self.passed,
            "evaluated": self.evaluated,
            "detail": self.detail,
            "offenders": [dict(offender) for offender in self.offenders],
        }


@dataclass(frozen=True, slots=True)
class RegulatorReport:
    """Everything the fast loop concluded about one variant.

    Two kinds of fact live here and they are wired separately on purpose.
    :attr:`gates` is correctness -- reproducible, adversarial, and the only input to
    :attr:`passed`. :attr:`style_vector` and :attr:`blandness_score` are taste --
    computed from the rendered look, read by the slow loop and by a human, and
    structurally incapable of moving the verdict because :attr:`passed` is a derived
    property with no setter and no field behind it.
    """

    variant_id: str
    gates: tuple[GateResult, ...]
    evidence_span: SpanEvidence
    style_vector: StyleVector | None = None
    blandness_score: float | None = None

    def __post_init__(self) -> None:
        names = [gate.name for gate in self.gates]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate gate results in report for {self.variant_id!r}")
        missing = [name for name in REQUIRED_GATES if name not in set(names)]
        if missing:
            raise ValueError(
                f"report for {self.variant_id!r} is missing gates {missing}; "
                "the bar is fixed, a short list would erode it silently"
            )
        if self.blandness_score is not None and not 0.0 <= self.blandness_score <= 1.0:
            raise ValueError(
                f"blandness {self.blandness_score!r} is not a cosine distance in [0, 1]"
            )
        if not TASTE_FIELDS.isdisjoint(GATE_FIELDS):
            raise ValueError("taste and gate fields must stay disjoint")

    @property
    def passed(self) -> bool:
        """Gates only. Never taste.

        A property rather than a field so the invariant is enforced by construction:
        there is no argument, no attribute and no code path by which a blandness
        score could set it.
        """
        return all(gate.passed for gate in self.gates)

    @property
    def failed_gates(self) -> tuple[GateResult, ...]:
        return tuple(gate for gate in self.gates if not gate.passed)

    @property
    def failed_gate_names(self) -> tuple[GateName, ...]:
        return tuple(gate.name for gate in self.failed_gates)

    def gate(self, name: GateName) -> GateResult:
        for result in self.gates:
            if result.name == name:
                return result
        raise KeyError(f"unknown gate {name!r}; known: {list(REQUIRED_GATES)}")

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        taste = (
            "blandness n/a"
            if self.blandness_score is None
            else f"blandness {self.blandness_score:.3f}"
        )
        failures = ", ".join(self.failed_gate_names) or "none"
        return f"{self.variant_id}: {verdict} ({taste}) failed gates: {failures}"


@dataclass(frozen=True, slots=True)
class Regulator:
    """The fast loop, composed over ports.

    Holds no state between runs. Everything that could vary -- the anti-corpus the
    blandness score is measured against, the conformance level of the audit gate, the
    clock -- is a constructor argument, so a run is reproducible and a test does not
    have to reach into the module.
    """

    sanitizer: SanitizerPort
    sensor: SensorPort
    contract: DirectionContract
    anti_corpus: tuple[StyleVector, ...] = ()
    axe_tags: frozenset[str] = DEFAULT_AXE_TAGS
    now: Callable[[], str] = field(default=_utc_now, repr=False)

    def regulate(
        self,
        variant: Variant,
        *,
        url: str,
        turn_id: TurnId,
        span_id: SpanId | None = None,
        baseline: Path | None = None,
        interactions: Sequence[Interaction] | None = None,
        probe_signals: Sequence[Signal] = (),
        style_sample: StyleSample | None = None,
    ) -> RegulatorReport:
        """Run every cheap comparator over *variant* and report what they found.

        ``probe_signals`` is how latency class 1 reaches the gates. The shadow
        browser cannot see a failed htmx swap, an unknown utility class or a
        horizontal overflow that only a real user's viewport produced -- those are
        the in-page probe's observations, already in the evidence stock. The
        regulator takes them as an argument rather than reading a store, so the fast
        loop stays a pure function of what it was handed.

        ``style_sample`` is optional. Without it there is no style vector and no
        blandness score, and the gates are unaffected -- which is the separation
        stated as behaviour: taste can be entirely absent and the verdict is
        unchanged.
        """
        attribution = span_id or variant.span_id
        sanitized = self.sanitizer.check(variant.html)

        sensor_report: SensorReport | None = None
        if sanitized.accepted:
            sensor_report = self.sensor.observe(
                url,
                span_id=attribution,
                turn_id=turn_id,
                baseline=baseline,
                interactions=interactions,
            )

        observed = tuple(probe_signals) + tuple(
            sensor_report.signals if sensor_report is not None else ()
        )
        gates = self._gates(sanitized, sensor_report, observed)

        emitted = (
            self._sanitize_signal(sanitized, span_id=attribution, turn_id=turn_id),
            *self._gate_signals(gates, span_id=attribution, turn_id=turn_id),
        )
        evidence_span = SpanEvidence(
            span_id=attribution,
            turn_id=turn_id,
            signals=(*observed, *emitted),
        )

        # The sensor measures the page it actually rendered, so its sample is the
        # authoritative one; the argument stays as an override for tests and for
        # callers that measure elsewhere. Preferring the argument would let a
        # stale or synthetic sample silently outrank the real render -- which is
        # how the archive ended up empty while every gate passed.
        sample = style_sample
        if sample is None and sensor_report is not None:
            sample = getattr(sensor_report, "style_sample", None)
        vector = (
            None if sample is None else StyleVector.from_sample(sample, contract=self.contract)
        )
        score = (
            blandness(vector, self.anti_corpus)
            if vector is not None and self.anti_corpus
            else None
        )
        return RegulatorReport(
            variant_id=variant.variant_id,
            gates=gates,
            evidence_span=evidence_span,
            style_vector=vector,
            blandness_score=score,
        )

    # -- gates ------------------------------------------------------------- #

    def _gates(
        self,
        sanitized: SanitizeResult,
        sensor_report: SensorReport | None,
        observed: Sequence[Signal],
    ) -> tuple[GateResult, ...]:
        results = {
            GATE_SANITIZED: self._sanitizer_gate(sanitized),
            GATE_NO_UNKNOWN_CLASS: self._unknown_class_gate(sanitized, observed),
        }
        if sensor_report is None:
            reason = "not evaluated: the sanitiser refused the fragment, so it never rendered"
            for name in RENDER_DEPENDENT_GATES:
                results[name] = GateResult.not_evaluated(name, reason)
        else:
            results[GATE_NO_RUNTIME_ERRORS] = self._kind_gate(
                GATE_NO_RUNTIME_ERRORS, observed, RUNTIME_ERROR_KINDS
            )
            results[GATE_NO_OVERFLOW] = self._kind_gate(
                GATE_NO_OVERFLOW, observed, frozenset({OVERFLOW_KIND})
            )
            results[GATE_MOTION_CONFORMS] = self._kind_gate(
                GATE_MOTION_CONFORMS, observed, frozenset({MOTION_VIOLATION_KIND})
            )
            results[GATE_AXE_CLEAN] = self._axe_gate(sensor_report)
            results[GATE_REDUCED_MOTION] = self._reduced_motion_gate(sensor_report)
        return tuple(results[name] for name in REQUIRED_GATES)

    def _sanitizer_gate(self, sanitized: SanitizeResult) -> GateResult:
        if sanitized.accepted:
            return GateResult.ok(GATE_SANITIZED, "fragment accepted by the sanitiser")
        return GateResult.failed(
            GATE_SANITIZED,
            sanitized.report(),
            [
                {
                    "kind": str(violation.kind),
                    "locator": violation.locator,
                    "detail": violation.detail,
                }
                for violation in sanitized.violations
            ],
        )

    def _unknown_class_gate(
        self, sanitized: SanitizeResult, observed: Sequence[Signal]
    ) -> GateResult:
        """Two independent witnesses to the same failure, joined into one gate.

        The sanitiser reads the fragment as text against the contract's class
        vocabulary; the probe reads the live CSSOM against what the stylesheets
        actually define. They fail differently -- a class the contract permits but no
        stylesheet defines is invisible to the first and obvious to the second -- so
        the gate takes either as sufficient.
        """
        offenders: list[Mapping[str, Any]] = [
            {
                "source": "sanitizer",
                "locator": violation.locator,
                "detail": violation.detail,
            }
            for violation in sanitized.violations
            if violation.kind is ViolationKind.UNKNOWN_CLASS
        ]
        offenders.extend(
            {"source": signal.source, "span_id": signal.span_id, **signal.payload}
            for signal in observed
            if signal.kind == UNKNOWN_CLASS_KIND
        )
        if not offenders:
            return GateResult.ok(
                GATE_NO_UNKNOWN_CLASS, "every class name is accounted for by the contract"
            )
        return GateResult.failed(
            GATE_NO_UNKNOWN_CLASS,
            f"{len(offenders)} unknown-class finding(s): a class no token justifies",
            offenders,
        )

    def _kind_gate(
        self,
        name: GateName,
        observed: Sequence[Signal],
        kinds: frozenset[SignalKind],
    ) -> GateResult:
        found = [signal for signal in observed if signal.kind in kinds]
        if not found:
            return GateResult.ok(name, f"no {'/'.join(sorted(kinds))} signal observed")
        return GateResult.failed(
            name,
            f"{len(found)} signal(s) of kind {sorted({signal.kind for signal in found})}",
            [
                {
                    "kind": signal.kind,
                    "source": signal.source,
                    "span_id": signal.span_id,
                    "ts": signal.ts,
                    **signal.payload,
                }
                for signal in found
            ],
        )

    def _axe_gate(self, sensor_report: SensorReport) -> GateResult:
        gated = tuple(self._in_scope(sensor_report.axe_violations))
        if not gated:
            return GateResult.ok(
                GATE_AXE_CLEAN, f"no {'/'.join(sorted(self.axe_tags))} violations"
            )
        rules = sorted({str(violation.get("id", "")) for violation in gated})
        return GateResult.failed(
            GATE_AXE_CLEAN,
            f"{len(gated)} accessibility violation(s): {rules}",
            [dict(violation) for violation in gated],
        )

    def _in_scope(self, violations: Sequence[AxeViolation]) -> Sequence[AxeViolation]:
        scoped = []
        for violation in violations:
            tags = {str(tag) for tag in violation.get("tags", ())}
            if not tags or tags & self.axe_tags:
                scoped.append(violation)
        return scoped

    def _reduced_motion_gate(self, sensor_report: SensorReport) -> GateResult:
        if sensor_report.reduced_motion_ok:
            return GateResult.ok(
                GATE_REDUCED_MOTION, "animations collapse under prefers-reduced-motion"
            )
        return GateResult.failed(
            GATE_REDUCED_MOTION,
            "animations still run under prefers-reduced-motion",
            [{"url": sensor_report.url, "span_id": sensor_report.span_id}],
        )

    # -- evidence ---------------------------------------------------------- #

    def _sanitize_signal(
        self, sanitized: SanitizeResult, *, span_id: SpanId, turn_id: TurnId
    ) -> Signal:
        return Signal(
            span_id=span_id,
            turn_id=turn_id,
            source="gate",
            kind=SANITIZE_KIND,
            ts=self.now(),
            payload={
                "accepted": sanitized.accepted,
                "kinds": sorted(str(kind) for kind in sanitized.kinds()),
                "violations": [
                    {
                        "kind": str(violation.kind),
                        "locator": violation.locator,
                        "detail": violation.detail,
                    }
                    for violation in sanitized.violations
                ],
            },
        )

    def _gate_signals(
        self, gates: Sequence[GateResult], *, span_id: SpanId, turn_id: TurnId
    ) -> tuple[Signal, ...]:
        """Restate each gate as a flow into the stock.

        Written even when a gate passes: a stock that only recorded failures could
        not tell "the bar was met" from "the bar was never applied", which is the
        comparison a turn-over-turn regression check is made of.
        """
        observed_at = self.now()
        return tuple(
            Signal(
                span_id=span_id,
                turn_id=turn_id,
                source="gate",
                kind=GATE_KIND,
                ts=observed_at,
                payload=gate.as_payload(),
            )
            for gate in gates
        )


def with_anti_corpus(
    regulator: Regulator, anti_corpus: Sequence[StyleVector]
) -> Regulator:
    """A copy measuring taste against a different corpus.

    Exists to make the separation testable as behaviour: swapping the corpus can move
    :attr:`RegulatorReport.blandness_score` and can never move
    :attr:`RegulatorReport.passed`.
    """
    return replace(regulator, anti_corpus=tuple(anti_corpus))
