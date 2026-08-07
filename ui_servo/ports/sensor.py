"""The shadow-browser sensor port: latency class 2, the loop's second measurement.

The in-page probe (latency class 1) sees what happened to a *user's* session and
is therefore cheap, partial and unreproducible. This port describes the other
kind of sensor: a browser the loop drives itself, off the user's path, which can
afford to do the expensive things -- render the whole page, walk the accessibility
tree, run an audit engine, compare pixels against a kept baseline, sample frame
timing while it pokes at the UI, and re-render the same page under
``prefers-reduced-motion`` to see whether the answer changes.

The value of a second sensor family is decorrelation. The probe and the harness
observe the same span through different apparatus and fail in different ways, so
agreement between them is evidence and disagreement is a lead. ``data-span-id``
is what makes the comparison possible at all: every element the sensor screenshots
is keyed by it, and every :class:`~ui_servo.domain.evidence.Signal` it emits is
attributed to the *nearest* span that owns the finding -- a contrast failure inside
a nested fragment belongs to that fragment, not to the page it happened to be
observed through -- so a harness observation and a probe observation about the same
fragment land in the same :class:`~ui_servo.domain.evidence.SpanEvidence` without
either sensor knowing the other exists.

:class:`SensorReport` is a *report*, not a verdict. It says the contrast ratio was
audited and these violations came back, that 3.1% of pixels moved, that eleven
frames exceeded two refresh intervals -- and never that any of that is bad enough
to fail. Thresholds are policy and live with the regulator and the gates, which
change on a different clock than the sensors do; a report that had already decided
would have to be re-run every time the policy moved.

Pure port: standard library plus the domain. The browser, the audit engine, the
image library and the frame sampler are all adapter concerns (see
``ui_servo.adapters.playwright_sensor``); :class:`StubSensor` at the bottom of this
module is the proof that the contract is cheap to satisfy without any of them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.domain.variant import StyleSample

type ScreenshotName = str
type AxeViolation = dict[str, Any]
type Interaction = dict[str, Any]

FULL_PAGE_SCREENSHOT: ScreenshotName = "full-page"
"""Key of the whole-page capture in :attr:`SensorReport.screenshots`."""

SPAN_SCREENSHOT_PREFIX: str = "span:"
"""Prefix separating element captures from the reserved page-level keys.

Span ids come from the renderer and may legally be anything, including
``"full-page"``. Filing element captures under a prefixed key means the two
namespaces cannot collide, so the sensor never has to reject an id -- and a
consumer never has to guess whether ``screenshots["full-page"]`` is the page or a
fragment that happened to be named after it.
"""


def span_screenshot_key(span_id: SpanId) -> ScreenshotName:
    """The :attr:`SensorReport.screenshots` key under which *span_id* is filed."""
    return f"{SPAN_SCREENSHOT_PREFIX}{span_id}"


def span_id_of_key(key: ScreenshotName) -> SpanId | None:
    """Inverse of :func:`span_screenshot_key`; ``None`` for page-level keys."""
    if not key.startswith(SPAN_SCREENSHOT_PREFIX):
        return None
    return key[len(SPAN_SCREENSHOT_PREFIX) :]


@dataclass(frozen=True, slots=True)
class PixelDiff:
    """How much of the rendered surface moved, against a kept baseline.

    Constructed as ``PixelDiff(ratio=0.031, diff_path=Path(...),
    baseline_path=Path(...))``.

    :param ratio: fraction of pixels that changed beyond the adapter's per-channel
        tolerance, in ``[0, 1]``. A scalar, not a pass mark.
    :param diff_path: heatmap written next to the screenshots, or ``None`` when
        nothing changed. The number tells a regulator that something moved; only
        the image tells a human *what*.
    :param baseline_path: the image actually compared against -- the sensor's own
        copy, not the caller's, so the comparison stays reproducible even if the
        original is later replaced.
    """

    ratio: float
    diff_path: Path | None = None
    baseline_path: Path | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError(f"pixel diff ratio {self.ratio!r} is not a fraction in [0, 1]")


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """What the browser's main thread did while the interaction script ran.

    Constructed as ``TraceSummary(dropped_frames=4, longtasks=1,
    sampled_frames=37, max_frame_gap_ms=412.5)``.

    :param dropped_frames: frame intervals wider than the adapter's budget.
    :param longtasks: long tasks that *started inside* the interaction window;
        work done before the script began belongs to page load, not to the
        interaction, and counting it would make every observation look janky.
    :param sampled_frames: how many intervals were observed at all -- zero means
        the sampler never ran, which is a different fact from a smooth page.
    :param max_frame_gap_ms: the worst single interval, which is what a user feels.

    All counts are of *events*, never of severity: a dropped frame during a
    deliberate 400ms layout thrash is expected, and the same count during a hover
    is a bug. Only a caller holding the interaction script knows which it was.
    """

    dropped_frames: int
    longtasks: int
    sampled_frames: int = 0
    max_frame_gap_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.dropped_frames < 0 or self.longtasks < 0 or self.sampled_frames < 0:
            raise ValueError("trace counts cannot be negative")


@dataclass(frozen=True, slots=True)
class SensorReport:
    """Everything one shadow-browser observation of one span produced.

    Constructed positionally-by-keyword, all fields after ``url`` optional::

        SensorReport(
            span_id="hero", turn_id="turn-3", url="http://localhost:8000/",
            screenshots={FULL_PAGE_SCREENSHOT: Path(...),
                         span_screenshot_key("hero"): Path(...)},
            aria_snapshot="- heading \\"Ship it\\" [level=1]",
            axe_violations=({"id": "color-contrast", "nodes": [...]},),
            pixel_diff=PixelDiff(ratio=0.0),
            trace=TraceSummary(dropped_frames=0, longtasks=0),
            reduced_motion_ok=True,
            signals=(Signal(...),),
        )

    :param span_id: the span the observation was *requested* for -- the root of
        what was rendered. Individual findings may be attributed to descendants.
    :param turn_id: the turn the observation belongs to; with ``span_id`` it forms
        the evidence store's join key.
    :param url: what was actually visited.
    :param screenshots: ``{FULL_PAGE_SCREENSHOT: path}`` plus one
        ``span_screenshot_key(id) -> path`` per captured ``[data-span-id]``
        element. Paths, not bytes: the artefacts are large, they are already
        durable on disk next to the evidence stock, and a report that carried them
        in memory would make "keep every observation" cost memory instead of disk.
    :param aria_snapshot: the accessibility tree as text.
    :param axe_violations: the audit engine's violations, verbatim.
    :param pixel_diff: ``None`` when no baseline was supplied -- "unchanged" and
        "never compared" are different facts.
    :param trace: ``None`` when no interaction script ran, for the same reason.
    :param reduced_motion_ok: whether a reduced-motion render still moves.
    :param style_sample: the rendered fragment reduced to the numbers
        :class:`~ui_servo.domain.variant.StyleVector` embeds -- computed styles per
        visible element plus a pixel histogram of the screenshot. Populated on
        every observation, and optional only so that a hand-written report or a
        sensor without pixels is still a legal one. This is the seam that lets the
        fast loop score a *real* render rather than a fixture: without it the
        regulator can measure correctness and never taste.
    :param signals: the same observation restated as flows into the evidence
        stock. The structured fields are for the regulator, which needs the
        numbers; the signals are for the stock, which needs one uniform, joinable
        record type. Neither is derived from the other at read time, so a consumer
        never has to re-parse a report to find out what was seen.
    """

    span_id: SpanId
    turn_id: TurnId
    url: str
    screenshots: Mapping[ScreenshotName, Path] = field(default_factory=dict)
    aria_snapshot: str = ""
    axe_violations: Sequence[AxeViolation] = ()
    pixel_diff: PixelDiff | None = None
    trace: TraceSummary | None = None
    reduced_motion_ok: bool = True
    style_sample: StyleSample | None = None
    signals: Sequence[Signal] = ()

    @property
    def full_page_screenshot(self) -> Path | None:
        return self.screenshots.get(FULL_PAGE_SCREENSHOT)

    @property
    def span_screenshots(self) -> dict[SpanId, Path]:
        """Element captures keyed by their own ``data-span-id``, prefix stripped."""
        return {
            span_id: path
            for key, path in self.screenshots.items()
            if (span_id := span_id_of_key(key)) is not None
        }

    @property
    def axe_violation_ids(self) -> tuple[str, ...]:
        """The rule ids that fired, in report order -- what a gate routes on."""
        return tuple(str(violation.get("id", "")) for violation in self.axe_violations)

    def screenshot_for(self, span_id: SpanId) -> Path | None:
        return self.screenshots.get(span_screenshot_key(span_id))


@runtime_checkable
class SensorPort(Protocol):
    """Drive a browser at *url* and report what a machine can see there.

    Implementations must be side-effect free with respect to the evidence stock:
    they return signals and never write them. Who persists an observation, and
    whether it is persisted at all, is the control loop's decision -- a sensor that
    wrote directly would make a dry run indistinguishable from a recorded one.

    They must also never overwrite an earlier observation's artefacts. Two
    observations of the same span in the same turn are two measurements, and a
    sensor whose second run silently replaced the first would destroy the
    before/after pair that a pixel diff is made of.
    """

    def observe(
        self,
        url: str,
        *,
        span_id: SpanId,
        turn_id: TurnId,
        baseline: Path | None = None,
        interactions: Sequence[Interaction] | None = None,
    ) -> SensorReport:
        """Observe *url*, attributing everything seen to *span_id* in *turn_id*.

        ``baseline`` is a previously kept full-page screenshot; without one there
        is nothing to diff against and :attr:`SensorReport.pixel_diff` is ``None``
        rather than zero. ``interactions`` is a small declarative script (click,
        hover, scroll, wait); without it nothing is driven, so there is no frame
        timing to report and :attr:`SensorReport.trace` is ``None``.
        """
        ...


@dataclass(frozen=True, slots=True)
class ObservationCall:
    """One recorded call to :meth:`SensorPort.observe`, for tests of consumers."""

    url: str
    span_id: SpanId
    turn_id: TurnId
    baseline: Path | None = None
    interactions: tuple[Interaction, ...] = ()


@dataclass(slots=True)
class StubSensor:
    """A canned :class:`SensorPort` that visits nothing.

    Kept in the port module rather than in ``tests/`` for the same reason
    ``AcceptAllSanitizer`` is: if the cheapest possible implementation of a port is
    awkward to write, the port is shaped wrong. The regulator's tests need a sensor
    that returns a known report without a browser, and every consumer that wants one
    should get it from the contract instead of re-deriving it.

    ``report`` is returned with :attr:`SensorReport.span_id`, ``turn_id`` and
    ``url`` overwritten to match the call, so a single canned report can stand in
    for any span. ``calls`` records what was asked of it.
    """

    report: SensorReport | None = None
    calls: list[ObservationCall] = field(default_factory=list)

    def observe(
        self,
        url: str,
        *,
        span_id: SpanId,
        turn_id: TurnId,
        baseline: Path | None = None,
        interactions: Sequence[Interaction] | None = None,
    ) -> SensorReport:
        self.calls.append(
            ObservationCall(
                url=url,
                span_id=span_id,
                turn_id=turn_id,
                baseline=baseline,
                interactions=tuple(interactions or ()),
            )
        )
        canned = self.report or SensorReport(span_id=span_id, turn_id=turn_id, url=url)
        return replace(canned, span_id=span_id, turn_id=turn_id, url=url)
