"""One full gauntlet round: the fast loop, the blind panel, and the archive, composed.

This is the module that spends the budget. Everything under it is a comparator with
one job -- the regulator says *broken or not*, the panel says *which of these two is
closer to the bar*, the archive says *have we been here before* -- and none of them
knows the others exist. What a round is, in what order, and at whose expense, is a
policy decision, and it lives here where it can be read in one sitting.

The order is the whole design, and it is an economics argument:

1. **Every variant meets the deterministic gates first** (:mod:`ui_servo.control.regulator`).
   A parse, a render and an audit cost milliseconds and no tokens.
2. **A variant that failed a gate never reaches a judge.** It leaves the round in
   :attr:`RoundResult.rejected` carrying the gate that stopped it and the offenders
   that gate saw, which is a work item a builder can act on without a critic having
   said a word. This is the single largest saving in the loop, and it is structural:
   the survivors are the only list the tournament is ever given.
3. **Survivors are argued about pairwise, blind, by rotated cross-family critics**
   (:mod:`ui_servo.control.critique`), against a concrete bar.
4. **Disagreement is escalated rather than averaged**, and the ranked survivors are
   offered to the quality-diversity archive so that "good" and "not the same as the
   last six" stay separate questions.

**Blind staging is this module's job and it is done by copying.** The panel refuses
a prompt that names a model family but it cannot make a path opaque; a screenshot
sitting at ``evidence/rounds/3/hero.claude.2/full-page.png`` names the author in the
one place nobody thinks of as text. So before any comparison, both artefacts are
copied into a per-comparison directory under names built from a digest --
``<out>/blind/<comparison-id>/<part>.<A|B>.<hash>.{html,png,json}`` -- with no family
token, no variant id and no ordinal anywhere in the name. The label and the digest
are scoped to the comparison, so the same variant is a different filename in every
comparison it appears in and two prompts cannot be correlated by eye. The mapping
back to real variants is written to the evidence stock and to the round result,
where the human reads it and no judge does.

Two rules keep that from being decorative. The staged fragment's own markup is run
through :func:`~ui_servo.domain.policy.enforce_blindness` with this round's builder
family names added as extra tokens, so a fragment that stamps its author into a
``data-span-id`` stops the round instead of quietly unblinding it; and the observed
markup notes are dropped rather than sent if they carry the same leak.

**Pure control.** Everything imported here is the standard library, the domain
or the ports, which is what makes :func:`run_round` runnable against fakes and
what keeps Playwright, nh3, Litestar and granian out of any process that merely
wants the loop. The composition root -- where those adapters are actually chosen
-- is :mod:`ui_servo.cli.servo`, one ring further out.
"""

import json
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import blake2b
from pathlib import Path
from typing import Any, Final, Protocol

from ui_servo.control import explore
from ui_servo.control.critique import Bar, Candidate, CritiquePanel, PanelRound
from ui_servo.control.regulator import Regulator, RegulatorReport
from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.domain.policy import (
    EMPTY_HISTORY,
    RUBRIC_AXES,
    Choice,
    Family,
    PanelOutcome,
    RotationHistory,
    RoundId,
    blindness_violations,
    enforce_blindness,
)
from ui_servo.domain.variant import (
    STILL,
    build_anti_corpus,
    EliteArchive,
    MotionEvidence,
    PartName,
    StyleSample,
    StyleVector,
    Variant,
    VariantId,
)
from ui_servo.ports.judge import JudgePort, JudgeRequest, JudgeResponse
from ui_servo.ports.sanitizer import SanitizerPort
from ui_servo.ports.sensor import Interaction, SensorPort, SensorReport
from ui_servo.ports.store import EvidenceStorePort, ExemplarStorePort

type Score = float

SERVO_SOURCE: Final[str] = "gate"
"""Which sensor family the servo's own records are filed under.

:data:`~ui_servo.domain.evidence.SIGNAL_SOURCES` is a closed vocabulary of four
sensor families and the loop is not one of them. ``gate`` is the honest choice
rather than a fifth value: everything this module emits is a record of the
gatekeeping it performed -- what was staged for whom, and what the round decided.
"""

STAGING_KIND: Final[str] = "blind-staging"
"""One artefact copied under an opaque name, with the mapping back. The
unblinding key, written where the human reads it and the panel does not."""

ROUND_KIND: Final[str] = "round-result"
"""The whole round, once: who was rejected, who was ranked, what escalated."""

ANIMATION_KIND: Final[str] = "animation"
"""A running animation, as the probe and the harness both report it. The only
motion measurement a round has, and the input to the archive's third axis."""

BLIND_DIRNAME: Final[str] = "blind"
REPORT_FILENAME: Final[str] = "frontier.html"
RESULT_FILENAME: Final[str] = "round.json"

MAX_TOURNAMENT_FIELD: Final[int] = 6
"""How many survivors are argued about pairwise.

**The tournament is a round robin**, chosen over a single-elimination bracket
because the round has to produce a *ranking*, not a champion: the archive admits
several elites and the human reads an ordered frontier, and a bracket tells you
nothing about the relative standing of the candidates it eliminated in round one.
Round robin over six candidates is fifteen comparisons, which is the ceiling this
constant states; a larger field is trimmed to the six most distinctive survivors
(see :func:`_tournament_field`) rather than being run at quadratic cost.
"""

WIN_POINTS: Final[Score] = 1.0
DRAW_POINTS: Final[Score] = 0.5
"""Copeland scoring. An escalation scores as a draw *and* is recorded as an
escalation: "the panel could not separate them" and "the panel called it a tie"
carry the same ordering information, and differ only in that one of them is a
question for the human, which is what :attr:`RoundResult.escalations` is for."""

DIGEST_BYTES: Final[int] = 6
_SPAN_ID_ATTR: Final[re.Pattern[str]] = re.compile(
    r"""data-span-id\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(*parts: str) -> str:
    return blake2b("\x00".join(parts).encode("utf-8"), digest_size=DIGEST_BYTES).hexdigest()


# --------------------------------------------------------------------------- #
# Candidate files                                                              #
# --------------------------------------------------------------------------- #


class CandidateNameError(ValueError):
    """A candidate file whose name does not say what it is.

    Refused rather than guessed at: the builder family is what disqualifies a
    critic from judging the artefact, so a file the loop had to guess about would
    silently put a family on the panel that wrote what it is judging.
    """


@dataclass(frozen=True, slots=True)
class CandidateFile:
    """One fragment on disk, with the provenance its filename declares.

    The grammar is ``<part>.<family>.<k>.html`` -- ``hero.claude.2.html`` is the
    third variant of the hero part built by the ``claude`` family. The two-token
    form ``<part>.<family>.html`` is accepted as ``k = 0`` so a one-shot build does
    not have to invent an index.
    """

    path: Path
    part: PartName
    builder_family: Family
    index: int

    @property
    def name(self) -> str:
        """The stem, which is also the variant id and the preview server's key."""
        return self.path.stem

    def to_variant(self) -> Variant:
        return Variant(
            variant_id=self.name,
            part=self.part,
            html=self.path.read_text(encoding="utf-8"),
            builder_family=self.builder_family,
        )


def parse_candidate_name(path: Path) -> CandidateFile:
    """Read ``<part>.<family>.<k>.html`` off *path*, or say exactly what is wrong."""
    tokens = path.stem.split(".")
    match tokens:
        case [part, family]:
            index = 0
        case [part, family, ordinal]:
            if not ordinal.isdigit():
                raise CandidateNameError(
                    f"{path.name}: variant index {ordinal!r} is not a number; the grammar is "
                    "<part>.<family>.<k>.html"
                )
            index = int(ordinal)
        case _:
            raise CandidateNameError(
                f"{path.name}: expected <part>.<family>.<k>.html (or <part>.<family>.html), "
                f"got {len(tokens)} dot-separated token(s)"
            )
    if not part or not family:
        raise CandidateNameError(f"{path.name}: part and builder family must both be named")
    return CandidateFile(path=path, part=part, builder_family=family, index=index)


def load_candidates(directory: Path, *, part: PartName | None = None) -> tuple[Variant, ...]:
    """Every ``*.html`` in *directory* as a :class:`Variant`, optionally one part.

    Sorted by (part, family, index) so two runs over the same directory build the
    same round in the same order -- the tournament, the rotation seed and the
    staged digests all derive from it.
    """
    files = [parse_candidate_name(path) for path in sorted(directory.glob("*.html"))]
    wanted = [found for found in files if part is None or found.part == part]
    wanted.sort(key=lambda found: (found.part, found.builder_family, found.index))
    return tuple(found.to_variant() for found in wanted)


# --------------------------------------------------------------------------- #
# What a round produced                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Stores:
    """The two stocks a round touches, bundled so the signature stays honest.

    The exemplar store is optional and is never written by this module: taste
    injection is a human act and goes through ``explore.record_pick``. It is
    carried here only so the composition root has one place to put it.
    """

    evidence: EvidenceStorePort
    exemplar: ExemplarStorePort | None = None


@dataclass(frozen=True, slots=True)
class GateRejection:
    """A variant the fast loop stopped, with the evidence that stopped it.

    Carries the offenders verbatim rather than a summary line, because this is the
    whole message the builder gets back: a rejection that says "failed axe-clean"
    without naming the node costs another round trip to find out where.
    """

    variant_id: VariantId
    part: PartName
    builder_family: Family
    failed_gates: tuple[str, ...]
    detail: str
    offenders: tuple[Mapping[str, Any], ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "part": self.part,
            "builder_family": self.builder_family,
            "failed_gates": list(self.failed_gates),
            "detail": self.detail,
            "offenders": [dict(offender) for offender in self.offenders],
        }


@dataclass(frozen=True, slots=True)
class StagedArtefact:
    """One candidate as the panel sees it, plus the mapping back to who built it.

    The two halves are deliberately in one value and deliberately never travel
    together: :attr:`ref` and :attr:`screenshot` are what a prompt may carry,
    :attr:`variant_id` and :attr:`builder_family` are what the evidence stock and
    the human get. Keeping them in one record is what makes the round auditable;
    keeping them apart in the prompt is what makes it blind.
    """

    comparison_id: str
    label: Choice
    ref: str
    variant_id: VariantId
    builder_family: Family
    html: Path
    screenshot: Path
    sidecar: Path

    def as_mapping(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "label": self.label,
            "ref": self.ref,
            "variant_id": self.variant_id,
            "builder_family": self.builder_family,
            "html": str(self.html),
            "screenshot": str(self.screenshot),
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """One pairwise round: who was compared, under what names, and what came back."""

    comparison_id: str
    a: StagedArtefact
    b: StagedArtefact
    outcome: PanelOutcome
    voting_families: tuple[Family, ...] = ()
    silent_families: tuple[Family, ...] = ()
    findings: tuple[Mapping[str, Any], ...] = ()

    @property
    def escalated(self) -> bool:
        return self.outcome.escalated

    def winner_variant(self) -> VariantId | None:
        match self.outcome.winner:
            case "A":
                return self.a.variant_id
            case "B":
                return self.b.variant_id
            case _:
                return None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "A": self.a.as_mapping(),
            "B": self.b.as_mapping(),
            "outcome": self.outcome.as_mapping(),
            "voting_families": list(self.voting_families),
            "silent_families": list(self.silent_families),
            "findings": [dict(finding) for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class Escalation:
    """A comparison the panel could not decide, packaged for the human.

    Both screenshots ride along because that is the whole point: the human is being
    shown the disagreement, not told about it.
    """

    comparison_id: str
    detail: str
    tally: Mapping[Choice, int]
    voting_families: tuple[Family, ...]
    variants: tuple[VariantId, VariantId]
    screenshots: tuple[Path, Path]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "detail": self.detail,
            "tally": dict(self.tally),
            "voting_families": list(self.voting_families),
            "variants": list(self.variants),
            "screenshots": [str(path) for path in self.screenshots],
        }


@dataclass(frozen=True, slots=True)
class RankedVariant:
    """One survivor's standing after the round robin.

    ``score`` is Copeland points, not a quality scale: it says how this candidate
    did against *this* field, which is the only thing a pairwise panel can tell you.
    ``blandness`` sits beside it and never inside it -- taste is reported, never
    summed into the standing.
    """

    rank: int
    variant_id: VariantId
    part: PartName
    builder_family: Family
    score: Score
    wins: int = 0
    losses: int = 0
    draws: int = 0
    escalations: int = 0
    blandness: float | None = None
    findings: tuple[Mapping[str, Any], ...] = ()
    report: RegulatorReport | None = field(default=None, repr=False)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "variant_id": self.variant_id,
            "part": self.part,
            "builder_family": self.builder_family,
            "score": self.score,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "escalations": self.escalations,
            "blandness": self.blandness,
            "findings": [dict(finding) for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class RoundResult:
    """Everything one round decided, and where the artefacts that prove it live."""

    round_id: RoundId
    turn_id: TurnId
    part: PartName
    rejected: tuple[GateRejection, ...] = ()
    ranked: tuple[RankedVariant, ...] = ()
    escalations: tuple[Escalation, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    not_argued: tuple[VariantId, ...] = ()
    report_path: Path | None = None
    result_path: Path | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)

    @property
    def winner(self) -> RankedVariant | None:
        return self.ranked[0] if self.ranked else None

    @property
    def blind_map(self) -> tuple[Mapping[str, Any], ...]:
        """The unblinding key for the whole round, in comparison order."""
        return tuple(
            staged.as_mapping()
            for comparison in self.comparisons
            for staged in (comparison.a, comparison.b)
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "turn_id": self.turn_id,
            "part": self.part,
            "rejected": [rejection.as_mapping() for rejection in self.rejected],
            "ranked": [ranked.as_mapping() for ranked in self.ranked],
            "escalations": [escalation.as_mapping() for escalation in self.escalations],
            "comparisons": [comparison.as_mapping() for comparison in self.comparisons],
            "not_argued": list(self.not_argued),
            "report_path": None if self.report_path is None else str(self.report_path),
            "result_path": None if self.result_path is None else str(self.result_path),
            "coverage": dict(self.coverage),
            "blind_map": [dict(entry) for entry in self.blind_map],
        }

    def summary(self) -> str:
        """The round in the shape a human reads at the end of a CLI run."""
        lines = [
            f"round {self.round_id} / part {self.part}: "
            f"{len(self.ranked)} ranked, {len(self.rejected)} rejected by gates, "
            f"{len(self.comparisons)} comparison(s), {len(self.escalations)} escalated"
        ]
        for rejection in self.rejected:
            lines.append(
                f"  REJECTED {rejection.variant_id}: {', '.join(rejection.failed_gates)}"
            )
        for ranked in self.ranked:
            taste = "blandness n/a" if ranked.blandness is None else f"blandness {ranked.blandness:.3f}"
            lines.append(
                f"  #{ranked.rank} {ranked.variant_id} "
                f"({ranked.score:g} pts, {ranked.wins}W/{ranked.draws}D/{ranked.losses}L, {taste})"
            )
        for escalation in self.escalations:
            lines.append(f"  ESCALATED {escalation.comparison_id}: {escalation.detail}")
        for variant_id in self.not_argued:
            lines.append(f"  NOT ARGUED {variant_id}: field capped at {MAX_TOURNAMENT_FIELD}")
        if self.report_path is not None:
            lines.append(f"  frontier report: {self.report_path}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Collaborators the round can be handed                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _RecordingSensor:
    """A :class:`~ui_servo.ports.sensor.SensorPort` that keeps what it saw.

    The regulator drives the sensor and returns a verdict, not a report -- gates
    are all it is allowed to have an opinion about. The screenshots the panel needs
    are in the sensor's report, so the round wraps the sensor rather than widening
    the regulator: taste plumbing does not get to change the shape of the gate loop.
    """

    inner: SensorPort
    seen: dict[SpanId, SensorReport] = field(default_factory=dict)

    def observe(
        self,
        url: str,
        *,
        span_id: SpanId,
        turn_id: TurnId,
        baseline: Path | None = None,
        interactions: Sequence[Interaction] | None = None,
    ) -> SensorReport:
        report = self.inner.observe(
            url, span_id=span_id, turn_id=turn_id, baseline=baseline, interactions=interactions
        )
        self.seen[span_id] = report
        return report


@dataclass(frozen=True, slots=True)
class _Observation:
    """One variant after the fast loop: the verdict plus the artefacts it produced."""

    variant: Variant
    report: RegulatorReport
    screenshot: Path | None = None
    notes: str = ""


# --------------------------------------------------------------------------- #
# The stub panel                                                               #
# --------------------------------------------------------------------------- #


DRY_FAMILIES: Final[tuple[Family, ...]] = ("wren", "finch", "heron")
"""Families for the stub panel.

Bird names, deliberately: they are not model families, so a stub panel is never
disqualified by :func:`~ui_servo.domain.policy.eligible_judges` for having "built"
a candidate, and a reader of the evidence stock can never mistake a dry run's
votes for a real one's.
"""

SIDECAR_SUFFIX: Final[str] = ".json"


@dataclass(frozen=True, slots=True)
class DryJudge:
    """A judge that reads the measurement instead of looking, for pipeline tests.

    It exists so the whole round -- gates, staging, rotation, tally, archive,
    report -- can be exercised without a model call, and it is deliberately
    incapable of taste: it reads the blandness the fast loop already measured out
    of the staged sidecar and prefers the more distinctive candidate, falling back
    to a digest of the staged fragment when there is no anti-corpus to measure
    against. That makes a dry round deterministic and reproducible, and makes its
    verdicts obviously worthless as design judgement, which is the honest
    combination for a stub.
    """

    family: Family = DRY_FAMILIES[0]

    def judge(self, request: JudgeRequest) -> JudgeResponse:
        shown = request.image_paths[:2]
        left, right = (_sidecar_score(path) for path in shown) if len(shown) == 2 else (0.0, 0.0)
        winner: Choice = "A" if left > right else "B" if right > left else "tie"
        verdict = {
            "winner": winner,
            "per_axis": {axis: winner for axis in RUBRIC_AXES},
            "findings": [
                {
                    "axis": "distinctiveness",
                    "selector": "[data-span-id]",
                    "gap": (
                        "stub critic: ranked by the measured distinctiveness of the staged "
                        f"fragments ({left:.6f} vs {right:.6f}); no design judgement was made"
                    ),
                    "severity": "minor",
                }
            ],
            "confidence": 0.0,
        }
        return JudgeResponse(
            family=self.family, raw=json.dumps(verdict), parsed=verdict, rc=0
        )


def dry_panel(families: Sequence[Family] = DRY_FAMILIES) -> tuple[JudgePort, ...]:
    """One stub judge per stub family, so a dry round still needs a majority."""
    return tuple(DryJudge(family=family) for family in families)


def _sidecar_score(screenshot: Path) -> float:
    path = screenshot.with_suffix(SIDECAR_SUFFIX)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    match payload.get("blandness"):
        case float() | int() as measured:
            return float(measured)
        case _:
            return float(payload.get("digest", 0.0))


# --------------------------------------------------------------------------- #
# Blind staging                                                                #
# --------------------------------------------------------------------------- #


def _comparison_id(round_id: RoundId, left: VariantId, right: VariantId) -> str:
    """An opaque name for one comparison, stable across reruns of the same round."""
    return f"cmp-{_digest(round_id, *sorted((left, right)))}"


def _declared_span(html: str) -> SpanId | None:
    match = _SPAN_ID_ATTR.search(html)
    return match.group(1) if match else None


def _screenshot_of(report: SensorReport | None, variant: Variant) -> Path | None:
    """The best capture of *variant*: its own span, its declared span, the page.

    Three tries because the span the loop attributes by and the span the fragment
    stamps on itself are allowed to differ -- the loop keys on the variant so two
    builds of one part stay separable, the fragment keys on what it is.
    """
    if report is None:
        return None
    declared = _declared_span(variant.html)
    for span_id in (variant.span_id, *(() if declared is None else (declared,))):
        found = report.screenshot_for(span_id)
        if found is not None:
            return found
    return report.full_page_screenshot


def stage_blind(
    observation: _Observation,
    *,
    comparison_id: str,
    label: Choice,
    part: PartName,
    out_dir: Path,
    extra_tokens: Sequence[str] = (),
) -> StagedArtefact:
    """Copy one candidate under a name that says nothing about who built it.

    The layout is ``<out>/blind/<comparison-id>/<part>.<label>.<hash>.{html,png,json}``
    and the digest is scoped to the comparison, so the same variant is a different
    filename in every comparison it appears in.

    Three refusals, all of them the same refusal: the staged *path*, the staged
    *markup* and the observed *notes* are each checked against the family
    vocabulary plus this round's builder names. The first two raise -- a fragment
    that names its author is not something a round can proceed around -- and the
    notes are dropped instead, because losing the accessibility tree costs the
    critics detail while sending it would cost them their blindness.
    """
    digest = _digest(comparison_id, label, observation.variant.variant_id)
    stem = f"{part}.{label}.{digest}"
    directory = out_dir / BLIND_DIRNAME / comparison_id
    directory.mkdir(parents=True, exist_ok=True)

    html_path = directory / f"{stem}.html"
    screenshot_path = directory / f"{stem}.png"
    sidecar_path = directory / f"{stem}{SIDECAR_SUFFIX}"
    # Only the components servo generates are policed. The operator's chosen
    # --out root is theirs to name, and a machine whose scratch directory happens
    # to contain a family word (``/tmp/claude-1000/...``) says nothing about who
    # built the candidate; failing the round over it would be a false positive
    # that stops real work. The out root is checked once, separately, at startup.
    enforce_blindness(
        str(Path(BLIND_DIRNAME) / comparison_id / html_path.name),
        label="the staged artefact path",
        extra_tokens=extra_tokens,
    )

    markup = observation.variant.html
    enforce_blindness(
        markup,
        label=f"the staged markup of comparison {comparison_id} candidate {label}",
        extra_tokens=extra_tokens,
    )
    html_path.write_text(markup, encoding="utf-8")
    if observation.screenshot is not None and observation.screenshot.is_file():
        shutil.copyfile(observation.screenshot, screenshot_path)
    sidecar_path.write_text(
        json.dumps(
            {
                "blandness": observation.report.blandness_score,
                "digest": int(digest, 16) / float(1 << (DIGEST_BYTES * 8)),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return StagedArtefact(
        comparison_id=comparison_id,
        label=label,
        ref=f"cand-{digest}",
        variant_id=observation.variant.variant_id,
        builder_family=observation.variant.builder_family,
        html=html_path,
        screenshot=screenshot_path,
        sidecar=sidecar_path,
    )


def _blind_notes(observation: _Observation, extra_tokens: Sequence[str]) -> str:
    """What the critic reads besides the screenshot: the markup, then the tree.

    The markup is inlined rather than merely pointed at, because "read this
    file" is a capability, not a given. Of the three families on this box only
    some can open a local PNG; one returns an empty reply when asked to, and a
    critic that cannot see the artefact abstains, which costs the round a whole
    decorrelated vote. Text every family can read is the difference between a
    three-family panel and a one-family opinion.

    Both parts are blindness-checked. The markup was already checked at staging;
    it is checked again here because this is where it enters a prompt, and the
    accessibility tree is dropped rather than fixed if it names an author --
    losing detail costs the critics nuance, while sending it costs them their
    blindness.
    """
    parts: list[str] = []
    markup = observation.variant.html.strip()
    if markup and not blindness_violations(markup, extra_tokens=extra_tokens):
        parts.append(f"markup:\n{markup}")
    notes = observation.notes.strip()
    if notes and not blindness_violations(notes, extra_tokens=extra_tokens):
        parts.append(f"accessibility tree:\n{notes}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# The round                                                                    #
# --------------------------------------------------------------------------- #


def run_round(
    variants: Sequence[Variant],
    *,
    contract: DirectionContract,
    stores: Stores,
    sanitizer: SanitizerPort,
    sensor: SensorPort,
    judges: Sequence[JudgePort],
    round_id: RoundId,
    out_dir: Path,
    url_for: Callable[[Variant], str],
    part: PartName | None = None,
    part_spec: str = "",
    turn_id: TurnId | None = None,
    anti_corpus: Sequence[StyleVector] = (),
    style_sample_for: Callable[[Variant], StyleSample | None] | None = None,
    archive: EliteArchive | None = None,
    motion_for: Callable[[_Observation], MotionEvidence] | None = None,
    report_clock: Callable[[], str] = _utc_now,
    max_workers: int = 1,
) -> RoundResult:
    """Gate every variant, argue about the survivors, rank them, write the report.

    ``url_for`` is how a fragment becomes something a browser can visit; the round
    does not care whether that is a preview server, a static file or a staging
    deployment, only that the sensor can reach it.

    ``max_workers`` above one runs the fast loop in a thread pool. It defaults to
    one because the shipped sensor drives Playwright's *synchronous* API, which is
    bound to the thread that created it; a sensor that is safe to share -- a stub,
    an HTTP harness, one browser per thread -- can raise it, and the gates are I/O
    bound enough that it pays.

    ``style_sample_for`` is the seam where taste enters. It is a hook rather than
    something the round derives because no shipped sensor returns a
    :class:`~ui_servo.domain.variant.StyleSample` yet; without one there is no style
    vector, so there is no blandness score and no archive coordinate, and the round
    says so (``coverage["skipped_without_style_vector"]``) instead of filing
    candidates in a cell it guessed at. Gates are unaffected either way, which is
    the taste separation stated as behaviour.

    ``archive`` accumulates across rounds and is handed in for that reason: a fresh
    grid every round would make "nothing has ever reached this corner" unsayable,
    which is the one thing the archive exists to say. A round given none builds an
    empty one, so a single round still writes a frontier report.

    ``probe_signals`` are not a parameter: latency class 1 is what the in-page
    probe already reported about earlier renderings of this span, so the round
    reads them out of the evidence stock (:meth:`EvidenceStorePort.signals_for_span`)
    and hands them to the regulator. They are not written back -- they are already
    in the stock, and re-appending them every round would make the stock's own
    counts a function of how often it was read.
    """
    turn = turn_id or f"turn-{round_id}"
    resolved_part = part or (variants[0].part if variants else "unknown")
    out_dir.mkdir(parents=True, exist_ok=True)

    bar = Bar.from_contract(contract)
    spec = part_spec.strip() or f"the {resolved_part} fragment of the site"
    families = tuple(dict.fromkeys(variant.builder_family for variant in variants))
    # Checked once, before anything expensive: a bar or a part spec that names a
    # family would fail every comparison, and finding that out after fifteen
    # renders is finding it out too late.
    enforce_blindness(spec, label="the part spec")
    enforce_blindness(bar.presented(), label="the bar")

    observations = _fast_loop(
        variants,
        contract=contract,
        sanitizer=sanitizer,
        sensor=sensor,
        store=stores.evidence,
        url_for=url_for,
        turn_id=turn,
        anti_corpus=anti_corpus,
        style_sample_for=style_sample_for,
        max_workers=max_workers,
    )
    rejected = tuple(
        _rejection(observation)
        for observation in observations
        if not observation.report.passed
    )
    survivors = [observation for observation in observations if observation.report.passed]
    field_, not_argued = _tournament_field(survivors)

    comparisons, escalations = _tournament(
        field_,
        judges=judges,
        store=stores.evidence,
        round_id=round_id,
        turn_id=turn,
        part=resolved_part,
        part_spec=spec,
        bar=bar,
        out_dir=out_dir,
        extra_tokens=families,
    )
    ranked = _standings(field_, comparisons)

    grid = archive if archive is not None else EliteArchive()
    coverage = _admit_all(
        ranked,
        {observation.variant.variant_id: observation for observation in field_},
        archive=grid,
        contract=contract,
        motion_for=motion_for or _motion_evidence,
    )
    report_path = explore.frontier_report(
        grid, out_dir / REPORT_FILENAME, now=report_clock
    )
    result = RoundResult(
        round_id=round_id,
        turn_id=turn,
        part=resolved_part,
        rejected=rejected,
        ranked=ranked,
        escalations=escalations,
        comparisons=comparisons,
        not_argued=tuple(observation.variant.variant_id for observation in not_argued),
        report_path=report_path,
        result_path=out_dir / RESULT_FILENAME,
        coverage=coverage,
    )
    _write_result(result, out_dir / RESULT_FILENAME)
    stores.evidence.append(
        Signal(
            span_id=resolved_part,
            turn_id=turn,
            source=SERVO_SOURCE,
            kind=ROUND_KIND,
            ts=_utc_now(),
            payload=result.as_mapping(),
        )
    )
    return result


# -- the fast loop ---------------------------------------------------------- #


def _fast_loop(
    variants: Sequence[Variant],
    *,
    contract: DirectionContract,
    sanitizer: SanitizerPort,
    sensor: SensorPort,
    store: EvidenceStorePort,
    url_for: Callable[[Variant], str],
    turn_id: TurnId,
    anti_corpus: Sequence[StyleVector],
    style_sample_for: Callable[[Variant], StyleSample | None] | None,
    max_workers: int,
) -> tuple[_Observation, ...]:
    recorder = _RecordingSensor(inner=sensor)
    regulator = Regulator(
        sanitizer=sanitizer,
        sensor=recorder,
        contract=contract,
        anti_corpus=tuple(anti_corpus),
    )

    def regulate(variant: Variant) -> _Observation:
        probe = _probe_signals(store, variant)
        report = regulator.regulate(
            variant,
            url=url_for(variant),
            turn_id=turn_id,
            probe_signals=probe,
            style_sample=None if style_sample_for is None else style_sample_for(variant),
        )
        _persist(store, report, borrowed=probe)
        observed = recorder.seen.get(variant.span_id)
        return _Observation(
            variant=variant,
            report=report,
            screenshot=_screenshot_of(observed, variant),
            notes="" if observed is None else observed.aria_snapshot,
        )

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return tuple(pool.map(regulate, variants))
    return tuple(regulate(variant) for variant in variants)


def _probe_signals(store: EvidenceStorePort, variant: Variant) -> tuple[Signal, ...]:
    """What the in-page probe has already said about this span, from the stock."""
    try:
        held = store.signals_for_span(variant.span_id)
    except Exception:  # noqa: BLE001 -- a cold or unreadable stock is not a gate failure
        return ()
    return tuple(signal for signal in held if signal.source == "probe")


def _persist(
    store: EvidenceStorePort, report: RegulatorReport, *, borrowed: Sequence[Signal]
) -> None:
    """Append what this observation *added* to the stock, not what it read from it.

    Identity, not equality: the regulator passes the probe signals through by
    reference, so the ones it did not author are exactly the ones that are the same
    objects the store handed over. Comparing by value would need a hash of a free
    -form payload, and getting that subtly wrong would duplicate history.
    """
    already_held = {id(signal) for signal in borrowed}
    fresh = [
        signal for signal in report.evidence_span.signals if id(signal) not in already_held
    ]
    if fresh:
        store.append_many(fresh)


def _rejection(observation: _Observation) -> GateRejection:
    report = observation.report
    failures = report.failed_gates
    return GateRejection(
        variant_id=observation.variant.variant_id,
        part=observation.variant.part,
        builder_family=observation.variant.builder_family,
        failed_gates=tuple(gate.name for gate in failures),
        detail="; ".join(f"{gate.name}: {gate.detail}" for gate in failures),
        offenders=tuple(
            {"gate": gate.name, **dict(offender)}
            for gate in failures
            for offender in gate.offenders
        ),
    )


# -- the tournament --------------------------------------------------------- #


def _tournament_field(
    survivors: Sequence[_Observation],
) -> tuple[tuple[_Observation, ...], tuple[_Observation, ...]]:
    """The survivors that get argued about, and the ones the budget could not reach.

    Above :data:`MAX_TOURNAMENT_FIELD` the round robin is trimmed by *distinctiveness*
    -- the most distinctive survivors are argued about first. That is a sampling
    policy for an expensive comparator, not a gate: everyone here has already passed
    every gate, and blandness is the only ordering the fast loop measured. When there
    is no anti-corpus the ordering is the input order, which is deterministic and
    says so.
    """
    if len(survivors) <= MAX_TOURNAMENT_FIELD:
        return tuple(survivors), ()
    ordered = sorted(
        enumerate(survivors),
        key=lambda item: (
            -(item[1].report.blandness_score or 0.0),
            item[0],
        ),
    )
    chosen = {index for index, _ in ordered[:MAX_TOURNAMENT_FIELD]}
    return (
        tuple(observation for index, observation in enumerate(survivors) if index in chosen),
        tuple(observation for index, observation in enumerate(survivors) if index not in chosen),
    )


def _tournament(
    field_: Sequence[_Observation],
    *,
    judges: Sequence[JudgePort],
    store: EvidenceStorePort,
    round_id: RoundId,
    turn_id: TurnId,
    part: PartName,
    part_spec: str,
    bar: Bar,
    out_dir: Path,
    extra_tokens: Sequence[str],
) -> tuple[tuple[Comparison, ...], tuple[Escalation, ...]]:
    """Every pair, once, blind, with the rotation memory threaded through."""
    # The out root travels inside every staged path the judges are handed; it is
    # the operator's name for a directory, not evidence of authorship.
    panel = CritiquePanel(
        judges=tuple(judges),
        store=store,
        span_id=part,
        masked=(str(out_dir), str(out_dir.resolve())),
    )
    history: RotationHistory = EMPTY_HISTORY
    comparisons: list[Comparison] = []
    escalations: list[Escalation] = []

    for left, right in _pairs(field_):
        comparison_id = _comparison_id(
            round_id, left.variant.variant_id, right.variant.variant_id
        )
        staged_a = stage_blind(
            left,
            comparison_id=comparison_id,
            label="A",
            part=part,
            out_dir=out_dir,
            extra_tokens=extra_tokens,
        )
        staged_b = stage_blind(
            right,
            comparison_id=comparison_id,
            label="B",
            part=part,
            out_dir=out_dir,
            extra_tokens=extra_tokens,
        )
        for staged in (staged_a, staged_b):
            store.append(
                Signal(
                    span_id=part,
                    turn_id=turn_id,
                    source=SERVO_SOURCE,
                    kind=STAGING_KIND,
                    ts=_utc_now(),
                    payload={"round_id": round_id, **staged.as_mapping()},
                )
            )

        panel_round = panel.run_round(
            round_id=f"{round_id}:{comparison_id}",
            turn_id=turn_id,
            part_spec=part_spec,
            bar=bar,
            a=Candidate(
                ref=staged_a.ref,
                screenshot=staged_a.screenshot,
                notes=_blind_notes(left, extra_tokens),
                builder_family=left.variant.builder_family,
            ),
            b=Candidate(
                ref=staged_b.ref,
                screenshot=staged_b.screenshot,
                notes=_blind_notes(right, extra_tokens),
                builder_family=right.variant.builder_family,
            ),
            history=history,
        )
        history = panel_round.history
        comparison = _comparison(comparison_id, staged_a, staged_b, panel_round)
        comparisons.append(comparison)
        if comparison.escalated:
            escalations.append(
                Escalation(
                    comparison_id=comparison_id,
                    detail=comparison.outcome.detail,
                    tally=dict(comparison.outcome.tally),
                    voting_families=comparison.voting_families,
                    variants=(staged_a.variant_id, staged_b.variant_id),
                    screenshots=(staged_a.screenshot, staged_b.screenshot),
                )
            )
    return tuple(comparisons), tuple(escalations)


def _pairs(field_: Sequence[_Observation]) -> Iterable[tuple[_Observation, _Observation]]:
    for index, left in enumerate(field_):
        for right in field_[index + 1 :]:
            yield left, right


def _comparison(
    comparison_id: str,
    staged_a: StagedArtefact,
    staged_b: StagedArtefact,
    panel_round: PanelRound,
) -> Comparison:
    return Comparison(
        comparison_id=comparison_id,
        a=staged_a,
        b=staged_b,
        outcome=panel_round.outcome,
        voting_families=panel_round.families,
        silent_families=tuple(rejection.family for rejection in panel_round.rejections),
        findings=tuple(
            finding.model_dump(mode="python")
            for vote in panel_round.votes
            for finding in vote.verdict.findings
        ),
    )


def _standings(
    field_: Sequence[_Observation], comparisons: Sequence[Comparison]
) -> tuple[RankedVariant, ...]:
    """Copeland points over the round robin, with taste reported beside the rank.

    The tie-break is distinctiveness and then the variant id: a ranking that
    depended on dictionary order would make the frontier a different frontier on
    every run, and the archive would learn from the noise.
    """
    points: dict[VariantId, Score] = {
        observation.variant.variant_id: 0.0 for observation in field_
    }
    record: dict[VariantId, dict[str, int]] = {
        variant_id: {"wins": 0, "losses": 0, "draws": 0, "escalations": 0}
        for variant_id in points
    }
    findings: dict[VariantId, list[Mapping[str, Any]]] = {
        variant_id: [] for variant_id in points
    }

    for comparison in comparisons:
        left, right = comparison.a.variant_id, comparison.b.variant_id
        for variant_id in (left, right):
            findings[variant_id].extend(comparison.findings)
        match comparison.outcome.status, comparison.outcome.winner:
            case "winner", "A":
                points[left] += WIN_POINTS
                record[left]["wins"] += 1
                record[right]["losses"] += 1
            case "winner", "B":
                points[right] += WIN_POINTS
                record[right]["wins"] += 1
                record[left]["losses"] += 1
            case _:
                for variant_id in (left, right):
                    points[variant_id] += DRAW_POINTS
                    record[variant_id]["draws"] += 1
                    if comparison.escalated:
                        record[variant_id]["escalations"] += 1

    ordered = sorted(
        field_,
        key=lambda observation: (
            -points[observation.variant.variant_id],
            -(observation.report.blandness_score or 0.0),
            observation.variant.variant_id,
        ),
    )
    return tuple(
        RankedVariant(
            rank=position,
            variant_id=observation.variant.variant_id,
            part=observation.variant.part,
            builder_family=observation.variant.builder_family,
            score=points[observation.variant.variant_id],
            blandness=observation.report.blandness_score,
            findings=_deduped(findings[observation.variant.variant_id]),
            report=observation.report,
            **record[observation.variant.variant_id],
        )
        for position, observation in enumerate(ordered, start=1)
    )


def _deduped(findings: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    seen: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for finding in findings:
        key = (
            finding.get("axis"),
            finding.get("selector"),
            finding.get("gap"),
            finding.get("severity"),
        )
        seen.setdefault(key, finding)
    return tuple(seen.values())


# -- the archive and the report --------------------------------------------- #


def _admit_all(
    ranked: Sequence[RankedVariant],
    observations: Mapping[VariantId, _Observation],
    *,
    archive: EliteArchive,
    contract: DirectionContract,
    motion_for: Callable[[_Observation], MotionEvidence],
) -> dict[str, Any]:
    """Offer every ranked survivor to the archive and report what happened.

    Only survivors are offered, and that is a precondition rather than a filter:
    :func:`~ui_servo.control.explore.admit` raises
    :class:`~ui_servo.domain.variant.GateFailure` on a gate-failed report, because a
    broken candidate reaching the archive means the tier ordering has been violated
    somewhere upstream. It cannot happen from here -- :attr:`RoundResult.ranked` is
    built from the survivors of the fast loop -- and if it ever does, the loud
    failure is the correct outcome.

    A survivor with no style vector is *skipped* rather than forced in. The archive
    locates a candidate by what it rendered, so with no style sample there is no
    coordinate; counting the skips keeps that visible instead of letting an empty
    grid look like an unexplored one.
    """
    placed = 0
    held = 0
    skipped: list[VariantId] = []
    for entry in ranked:
        observation = observations.get(entry.variant_id)
        if observation is None or entry.report is None:
            continue
        if entry.report.style_vector is None:
            skipped.append(entry.variant_id)
            continue
        elite = explore.admit(
            entry.report,
            entry.rank,
            archive=archive,
            variant=observation.variant,
            panel_size=len(ranked),
            motion=motion_for(observation),
            contract=contract,
            findings=_finding_lines(entry.findings),
            screenshot=observation.screenshot or "",
        )
        if elite is None:
            held += 1
        else:
            placed += 1
    return {
        **archive.coverage().model_dump(mode="json"),
        "placed": placed,
        "held_by_incumbent": held,
        "skipped_without_style_vector": skipped,
    }


def _finding_lines(findings: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Panel findings as the one-line strings the frontier card renders."""
    return tuple(
        f"{finding.get('axis', '?')}: {finding.get('gap', '')} [{finding.get('selector', '')}]"
        for finding in findings
    )


def _motion_evidence(observation: _Observation) -> MotionEvidence:
    """What the fragment was observed to *move*, from the evidence already taken.

    Built from ``animation`` signals -- the probe and the harness both emit one per
    running animation with its duration -- so the motion axis is measured rather
    than assumed. ``total_elements`` stays zero because no sensor reports an element
    census today, which leaves :attr:`MotionEvidence.reach` at zero and the axis
    carried by pace alone; a caller that has a denominator can pass its own evidence
    through ``motion_for`` and get the full coordinate.
    """
    durations = [
        float(signal.payload.get("duration", 0.0) or 0.0)
        for signal in observation.report.evidence_span.signals
        if signal.kind == ANIMATION_KIND
    ]
    if not durations:
        return STILL
    return MotionEvidence(
        animated_elements=len(durations),
        total_elements=0,
        longest_duration_ms=max(durations),
    )


def _write_result(result: RoundResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.as_mapping(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# The composition root                                                         #
# --------------------------------------------------------------------------- #


DEFAULT_ANTI_CORPUS: Final[Path] = Path("tests/fixtures/blandness")
"""Where the stock-template style samples live.

Blandness is distance from the corpus median, so without a corpus there is no
number -- and a round that reports ``blandness n/a`` for every survivor has
quietly demoted taste from a measurement back to an opinion."""


def load_anti_corpus(
    directory: Path | None,
    contract: DirectionContract,
    *,
    on_error: Callable[[str], None] = lambda _message: None,
) -> tuple[StyleVector, ...]:
    """Embed every ``generic_*.json`` sample once, for this round to measure against."""
    root = directory or DEFAULT_ANTI_CORPUS
    if not root.is_dir():
        return ()
    samples = []
    for path in sorted(root.glob("generic_*.json")):
        try:
            samples.append((path.stem, StyleSample.model_validate_json(path.read_text())))
        except Exception as error:  # noqa: BLE001 -- a bad fixture must not end a round
            on_error(f"anti-corpus: skipping {path.name}: {error}")
    return tuple(build_anti_corpus(samples, contract=contract).values())


def default_contract_path() -> Path | None:
    """Where the contract lives, most explicit first: override, wheel, checkout."""
    import os
    from importlib.resources import as_file, files

    found: list[Path] = []
    match os.environ.get("UI_SERVO_CONTRACT"):
        case None | "":
            pass
        case override:
            found.append(Path(override))
    try:
        with as_file(files("ui_servo.domain") / "data" / "direction.toml") as packaged:
            found.append(packaged)
    except (FileNotFoundError, ModuleNotFoundError, TypeError):
        pass
    found.append(Path(__file__).resolve().parents[2] / "direction" / "direction.toml")
    return next((path for path in found if path.is_file()), None)
