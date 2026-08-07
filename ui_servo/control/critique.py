"""The slow loop: one blind, rotated, cross-family panel round over one pair.

This is the comparator the deterministic gates cannot be. Tier 0 through 2 answer
"is it broken"; nothing they measure answers "is it any good", and the only
instrument available for that question is a set of models with different priors,
asked the same question about the same two artefacts and made to disagree in
public. Everything in this module exists to keep that instrument honest.

The round, in order: drop the families that built either candidate, rotate the
rest deterministically and drop any that judged this pair before, randomise per
judge which candidate is shown first, ask, validate, re-ask exactly once if the
answer broke the rubric, map the answer back into the caller's A/B, and tally by
family. Every one of those steps is a rule in :mod:`ui_servo.domain.policy`; this
module is the sequencing and the plumbing, not the governance.

**Blindness is structural here, not aspirational.** A :class:`Candidate` carries
an opaque reference, a screenshot path and optional blind notes -- there is no
field for who built it or why, so builder reasoning cannot leak by accident
because there is nowhere to put it. Staging the artefacts under opaque names is
the caller's job (this module never touches the filesystem: it reads no
screenshot, resolves no path, and copies nothing). What this module does is
refuse: every prompt is checked with
:func:`~ui_servo.domain.policy.enforce_blindness` before it is sent, and a prompt
that names a model family raises rather than travelling.

**A judge that fails costs its vote, never the round.** Judges do not raise
(:mod:`ui_servo.ports.judge`), so a timeout, a logged-out CLI or two unparseable
replies in a row is recorded as a ``judge-error`` signal and the panel proceeds
with the families that answered -- with :func:`~ui_servo.domain.policy.panel_outcome`
escalating if too few of them did.

Standard library plus :mod:`ui_servo.domain` and :mod:`ui_servo.ports`. The whole
round runs against
:class:`~ui_servo.ports.judge.ScriptedJudge` with no process, no network and no
screenshot on disk.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, Self

from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.domain.policy import (
    EMPTY_HISTORY,
    RUBRIC_AXES,
    CandidateRef,
    Family,
    PairKey,
    PanelOutcome,
    PairwiseVerdict,
    RotationHistory,
    RoundId,
    Violations,
    Vote,
    derandomize,
    eligible_judges,
    enforce_blindness,
    flip_for,
    pair_key,
    panel_outcome,
    rotate,
    validate_verdict,
    verdict_response_schema,
)
from ui_servo.ports.judge import DEFAULT_TIMEOUT_S, JudgePort, JudgeRequest, JudgeResponse
from ui_servo.ports.store import EvidenceStorePort

JUDGE_SOURCE: Final[str] = "judge"

VERDICT_KIND: Final[str] = "panel-verdict"
"""One family's accepted, derandomised verdict on one pair."""

REASK_KIND: Final[str] = "panel-reask"
"""A verdict that broke the rubric, with the violations it was re-asked with."""

REJECTED_KIND: Final[str] = "judge-error"
"""A family that produced no usable vote. A policy failure kind, deliberately."""

OUTCOME_KIND: Final[str] = "panel-outcome"
"""The tally: what the round decided, or that it escalated."""

DEFAULT_SPAN: Final[SpanId] = "panel"
MAX_REASKS: Final[int] = 1
"""One retry. A family that cannot answer the rubric twice is telling the panel
something, and further turns would spend the round's budget on its worst critic."""

_PROMPT_HEADER: Final[str] = (
    "You are a hostile, independent design critic. Two implementations of the same "
    "UI part are shown side by side. Judge which one is closer to the bar below, "
    "and name the concrete gaps.\n"
    "You are not told who wrote either candidate and you must not speculate about "
    "authorship, tooling or provenance. Judge only what is in front of you."
)

_HOW_TO_LOOK: Final[str] = (
    "How to look:\n"
    "- Compare the two screenshots against the references, not against generic "
    "good practice.\n"
    "- Anti-references are failure modes: resembling one is a gap, however "
    "competent the execution.\n"
    "- Answer every rubric axis. A candidate that wins on craft and loses on "
    "direction conformance has not won.\n"
    "- 'distinctiveness' asks whether this could be any product's page. If it "
    "could, say so.\n"
    "- Every finding must cite a CSS selector for the element it is about. A "
    "finding without a selector is not usable and will be rejected."
)

_REASK_TEMPLATE: Final[str] = (
    "Your previous answer was rejected because it broke the rubric:\n"
    "{violations}\n\n"
    "Answer the original question again, fixing exactly those problems.\n\n"
    "--- original question ---\n{original}\n--- end original question ---\n\n"
    "Reply with ONLY the JSON verdict object. Start with '{{' and end with '}}'."
)


class ContractLike(Protocol):
    """The slice of the direction contract the bar is built from.

    Structural rather than an import of
    :class:`~ui_servo.domain.contract.DirectionContract` so that a caller can
    hand in a trimmed or synthetic bar -- the panel is a comparator against a
    reference, and which reference is the caller's decision.
    """

    def summary(self) -> str: ...

    @property
    def references(self) -> Sequence[Any]: ...

    @property
    def anti_references(self) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """One artefact under comparison, stripped to what a blind critic may see.

    There is no author field, no rationale field and no prior-critique field, and
    that absence is the design: builder reasoning cannot leak into a prompt from
    a value that has nowhere to carry it. ``builder_family`` is the one exception
    and it is metadata about the *panel* -- it decides who is disqualified from
    judging (:func:`~ui_servo.domain.policy.eligible_judges`) and never reaches a
    prompt.

    ``ref`` and ``screenshot`` must already be opaque: the caller stages them
    under names that say nothing about provenance. This module checks that they
    are blind and refuses otherwise, but it cannot make them so.
    """

    ref: CandidateRef
    screenshot: Path
    notes: str = ""
    builder_family: Family | None = None

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("a candidate needs an opaque reference")
        object.__setattr__(self, "screenshot", Path(self.screenshot))

    def presented(self, label: str) -> str:
        """This candidate as the prompt shows it, under a positional label."""
        lines = [f"CANDIDATE {label}", f"screenshot (read this file): {self.screenshot}"]
        if self.notes.strip():
            lines.append(f"observed markup and accessibility tree:\n{self.notes.strip()}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Bar:
    """The concrete reference the comparison is made against.

    The Gauntlet Loop invariant "the bar is concrete" in the form a prompt can
    carry: the contract's own summary plus the named references and
    anti-references. Prose about quality is exactly what this replaces.
    """

    summary: str
    references: tuple[str, ...] = ()
    anti_references: tuple[str, ...] = ()

    @classmethod
    def from_contract(cls, contract: ContractLike) -> Self:
        return cls(
            summary=contract.summary(),
            references=tuple(_named(reference) for reference in contract.references),
            anti_references=tuple(_named(reference) for reference in contract.anti_references),
        )

    def presented(self) -> str:
        lines = ["THE BAR", self.summary.strip()]
        if self.references:
            lines.append("references to match: " + ", ".join(self.references))
        if self.anti_references:
            lines.append("anti-references to avoid: " + ", ".join(self.anti_references))
        return "\n".join(lines)


def _named(reference: Any) -> str:
    name = getattr(reference, "name", None) or str(reference)
    note = getattr(reference, "note", None)
    return f"{name} ({note})" if note else str(name)


@dataclass(frozen=True, slots=True)
class Ask:
    """One question actually put to one family, with the flip that produced it."""

    family: Family
    prompt: str
    was_flipped: bool
    attempt: int

    @property
    def order(self) -> str:
        return "B,A" if self.was_flipped else "A,B"


@dataclass(frozen=True, slots=True)
class Rejection:
    """A family whose vote was not counted, and why.

    Kept as data rather than a log line because "which families could not be
    heard from" is the difference between a panel that agreed and a panel that
    was down to one voice.
    """

    family: Family
    reason: str
    violations: tuple[str, ...] = ()
    rc: int = 0


@dataclass(frozen=True, slots=True)
class PanelRound:
    """Everything one round produced: the decision and its whole paper trail."""

    round_id: RoundId
    pair: PairKey
    outcome: PanelOutcome
    votes: tuple[Vote, ...] = ()
    asks: tuple[Ask, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    signals: tuple[Signal, ...] = ()
    history: RotationHistory = EMPTY_HISTORY

    @property
    def prompts(self) -> tuple[str, ...]:
        """Every prompt sent this round, so blindness is auditable after the fact."""
        return tuple(ask.prompt for ask in self.asks)

    @property
    def families(self) -> tuple[Family, ...]:
        return tuple(vote.family for vote in self.votes)

    def __bool__(self) -> bool:
        return bool(self.outcome)


def build_prompt(part_spec: str, bar: Bar, first: Candidate, second: Candidate) -> str:
    """The blind prompt, in presentation order.

    *first* is what the critic is told to call ``A``. The caller's own A/B is not
    represented here at all: the flip happens before this function and is undone
    after the answer, so the prompt is simply the truth about what was shown.

    Only the paths handed in are referenced. Nothing is read, resolved or
    globbed: if a screenshot is staged under a name that leaks its author, this
    function reproduces that leak into the prompt where
    :func:`~ui_servo.domain.policy.enforce_blindness` can catch it, which is the
    behaviour that makes the check meaningful.
    """
    axes = ", ".join(RUBRIC_AXES)
    return "\n\n".join(
        [
            _PROMPT_HEADER,
            f"PART SPEC\n{part_spec.strip()}",
            bar.presented(),
            first.presented("A"),
            second.presented("B"),
            f"RUBRIC AXES\n{axes}",
            _HOW_TO_LOOK,
        ]
    )


def reask_prompt(original: str, violations: Iterable[str]) -> str:
    """The single retry: name the broken rules, then ask the same question."""
    listed = "\n".join(f"- {violation}" for violation in violations)
    return _REASK_TEMPLATE.format(violations=listed, original=original)


@dataclass(frozen=True, slots=True)
class CritiquePanel:
    """A configured panel: a roster of judges and the rules they are run under.

    Frozen and reusable across rounds; all per-round state (the pair, the
    rotation memory) is passed in and handed back, so two rounds can be run
    concurrently against one panel without sharing anything.
    """

    judges: tuple[JudgePort, ...]
    store: EvidenceStorePort | None = None
    span_id: SpanId = DEFAULT_SPAN
    timeout_s: int = DEFAULT_TIMEOUT_S
    panel_size: int | None = None
    #: Literal substrings the caller has already judged authorship-free -- in
    #: practice the operator's ``--out`` root, which is embedded in every staged
    #: artefact path a judge is asked to read. Masked before the blindness scan
    #: so a scratch directory that happens to contain a vendor word cannot fail
    #: an otherwise blind round. See :func:`~ui_servo.domain.policy.blindness_violations`.
    masked: tuple[str, ...] = ()
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), compare=False)

    def run_round(
        self,
        *,
        round_id: RoundId,
        turn_id: TurnId,
        part_spec: str,
        bar: Bar,
        a: Candidate,
        b: Candidate,
        history: RotationHistory = EMPTY_HISTORY,
    ) -> PanelRound:
        """Ask one pair of candidates of one rotated, blind panel.

        The A/B of the arguments is the caller's frame and the frame every
        verdict comes back in; which of them any given critic saw first is
        decided by :func:`~ui_servo.domain.policy.flip_for` and undone before the
        verdict is recorded.
        """
        pair = pair_key(a.ref, b.ref)
        builders = [
            candidate.builder_family
            for candidate in (a, b)
            if candidate.builder_family is not None
        ]
        panel = rotate(
            eligible_judges(builders, self.judges),
            round_id,
            pair=pair,
            history=history,
            panel_size=self.panel_size,
        )

        votes: list[Vote] = []
        asks: list[Ask] = []
        rejections: list[Rejection] = []
        signals: list[Signal] = []
        updated = history

        def emit(kind: str, payload: Mapping[str, Any]) -> None:
            signal = Signal(
                span_id=self.span_id,
                turn_id=turn_id,
                source=JUDGE_SOURCE,
                kind=kind,
                ts=self.now().isoformat(),
                payload={"round_id": round_id, "pair": pair, **dict(payload)},
            )
            signals.append(signal)
            if self.store is not None:
                self.store.append(signal)

        for judge in panel:
            family = judge.family
            flipped = flip_for(round_id, pair, family)
            first, second = (b, a) if flipped else (a, b)
            prompt = enforce_blindness(
                build_prompt(part_spec, bar, first, second),
                label=f"the round-{round_id} prompt",
                masked=self.masked,
            )
            request = JudgeRequest.of(
                prompt,
                images=[first.screenshot, second.screenshot],
                response_schema=verdict_response_schema(),
                timeout_s=self.timeout_s,
            )
            asks.append(Ask(family=family, prompt=prompt, was_flipped=flipped, attempt=1))
            updated = updated.remembering(family, pair)

            outcome = self._ask(judge, request, prompt, flipped=flipped, asks=asks, emit=emit)
            match outcome:
                case Vote() as vote:
                    votes.append(vote)
                    emit(
                        VERDICT_KIND,
                        {
                            "family": family,
                            "was_flipped": flipped,
                            "verdict": vote.verdict.to_mapping(),
                        },
                    )
                case Rejection() as rejection:
                    rejections.append(rejection)
                    emit(
                        REJECTED_KIND,
                        {
                            "family": family,
                            "was_flipped": flipped,
                            "reason": rejection.reason,
                            "violations": list(rejection.violations),
                            "rc": rejection.rc,
                        },
                    )

        result = panel_outcome(votes)
        emit(
            OUTCOME_KIND,
            {
                **result.as_mapping(),
                "candidates": {"A": a.ref, "B": b.ref},
                "asked": [ask.family for ask in asks],
                "rejected": [rejection.family for rejection in rejections],
            },
        )
        return PanelRound(
            round_id=round_id,
            pair=pair,
            outcome=result,
            votes=tuple(votes),
            asks=tuple(asks),
            rejections=tuple(rejections),
            signals=tuple(signals),
            history=updated,
        )

    def _ask(
        self,
        judge: JudgePort,
        request: JudgeRequest,
        prompt: str,
        *,
        flipped: bool,
        asks: list[Ask],
        emit: Callable[[str, Mapping[str, Any]], None],
    ) -> Vote | Rejection:
        """One family's turn: ask, validate, re-ask at most once, then decide.

        Returns a :class:`Vote` or a :class:`Rejection`; it never raises on a
        judge's behalf, because the port's whole contract is that a failing
        family costs the panel one voice rather than the round.
        """
        family = judge.family
        response = judge.judge(request)
        if not response.ok:
            return Rejection(family=family, reason=_transport_reason(response), rc=response.rc)

        parsed = validate_verdict(response.parsed)
        if isinstance(parsed, PairwiseVerdict):
            return Vote(family=family, verdict=derandomize(parsed, flipped))

        violations: Violations = parsed
        emit(REASK_KIND, {"family": family, "violations": list(violations), "attempt": 1})
        retry_prompt = enforce_blindness(
            reask_prompt(prompt, violations), label="the re-ask prompt"
        )
        asks.append(
            Ask(
                family=family,
                prompt=retry_prompt,
                was_flipped=flipped,
                attempt=MAX_REASKS + 1,
            )
        )
        retried = judge.judge(request.with_prompt(retry_prompt))
        if not retried.ok:
            return Rejection(
                family=family,
                reason=_transport_reason(retried),
                violations=tuple(violations),
                rc=retried.rc,
            )
        second = validate_verdict(retried.parsed)
        if isinstance(second, PairwiseVerdict):
            return Vote(family=family, verdict=derandomize(second, flipped))
        return Rejection(
            family=family,
            reason=f"rejected twice: {'; '.join(second)}",
            violations=tuple(second),
            rc=retried.rc,
        )


def _transport_reason(response: JudgeResponse) -> str:
    detail = response.raw.strip().replace("\n", " ")[:200]
    return f"judge returned rc={response.rc}" + (f": {detail}" if detail else "")


def run_pairwise_round(
    judges: Iterable[JudgePort],
    *,
    round_id: RoundId,
    turn_id: TurnId,
    part_spec: str,
    bar: Bar,
    a: Candidate,
    b: Candidate,
    store: EvidenceStorePort | None = None,
    history: RotationHistory = EMPTY_HISTORY,
    span_id: SpanId = DEFAULT_SPAN,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> PanelRound:
    """One round without assembling a :class:`CritiquePanel` first."""
    panel = CritiquePanel(
        judges=tuple(judges), store=store, span_id=span_id, timeout_s=timeout_s
    )
    return panel.run_round(
        round_id=round_id,
        turn_id=turn_id,
        part_spec=part_spec,
        bar=bar,
        a=a,
        b=b,
        history=history,
    )


def with_judges(panel: CritiquePanel, judges: Iterable[JudgePort]) -> CritiquePanel:
    """The same rules over a different roster, for a rotation across rounds."""
    return replace(panel, judges=tuple(judges))
