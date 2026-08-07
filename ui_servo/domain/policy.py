"""Policy: the governance rules the comparator is run under, as pure functions.

Everything here is a rule someone could argue with, which is exactly why it is
one module of testable functions rather than a scatter of conditionals inside the
loop. The evidence layer deliberately holds no opinion about which observations
are failures, and the verdict layer holds no opinion about which critics may
speak; both of those opinions live here, where a change to them is a diff in one
file with tests attached rather than a quiet drift in behaviour.

Four governance rules, each guarding a different failure of a model panel:

* **Self-preference.** A family that built a candidate cannot judge it
  (:func:`eligible_judges`). Models prefer their own output, so a panel that
  lets the builder vote is measuring authorship, not taste. This is the Gauntlet
  Loop's "the implementer never grades itself", enforced at the family level
  because two calls to one family are one opinion sampled twice.
* **Rotation and freshness.** Critics are rotated deterministically and never
  re-judge a pair they have already seen (:func:`rotate`). Repetition turns a
  panel into a single model's house style applied N times; memory of a previous
  round turns a fresh look into an anchored one.
* **Position bias.** Which candidate is shown first is randomised per judge from
  a seed derived from the round (:func:`flip_for`) and mapped back afterwards
  (:func:`derandomize`). Randomised because models favour a position; seeded
  because a round nobody can replay is not evidence.
* **Blindness.** No prompt may name a model family (:func:`blindness_violations`).
  A critic that can infer who wrote a candidate is judging the author. Staging
  candidates under opaque names is the caller's job; refusing to send a prompt
  that leaks one is this module's.

And the aggregation rule: :func:`panel_outcome` requires a majority of *families*
and escalates otherwise, because "the models disagreed" is information the human
is owed rather than a tie to be broken by an average.

Pure domain: standard library plus pydantic, no ports, no I/O. Judges are handled
structurally (anything with a ``family`` attribute, or a bare family string), so
this module never learns what a judge transport is.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import blake2b
from random import Random
from typing import Any, Final, Literal, Self

from pydantic import ValidationError

from ui_servo.domain.evidence import Signal, SignalKind, failures_only
from ui_servo.domain.verdict import (
    CHOICES,
    MIN_FINDINGS,
    RUBRIC_AXES,
    SEVERITIES,
    VERDICT_RESPONSE_SCHEMA,
    Choice,
    Confidence,
    Finding,
    PairwiseVerdict,
    RubricAxis,
    Selector,
    Severity,
    opposite,
    tally,
    verdict_response_schema,
)

__all__ = [
    "CHOICES",
    "CONFORMANCE_FAILURE_KINDS",
    "CORRECTNESS_FAILURE_KINDS",
    "FAILURE_KINDS",
    "FAMILY_TOKENS",
    "JUDGE_FAILURE_KINDS",
    "LAYOUT_FAILURE_KINDS",
    "MIN_FINDINGS",
    "MIN_PANEL_FAMILIES",
    "RUBRIC_AXES",
    "SEVERITIES",
    "VERDICT_RESPONSE_SCHEMA",
    "BlindnessError",
    "Choice",
    "Confidence",
    "Finding",
    "PairwiseVerdict",
    "PanelOutcome",
    "RotationHistory",
    "RubricAxis",
    "Selector",
    "Severity",
    "Vote",
    "blindness_violations",
    "eligible_judges",
    "derandomize",
    "enforce_blindness",
    "failing_signals",
    "family_of",
    "flip_for",
    "is_blind",
    "opposite",
    "pair_key",
    "panel_outcome",
    "rotate",
    "tally",
    "validate_verdict",
    "verdict_response_schema",
]

type Family = str
type PairKey = str
type RoundId = str
type CandidateRef = str
type Violations = list[str]
type PanelStatus = Literal["winner", "escalate"]

CORRECTNESS_FAILURE_KINDS: Final[frozenset[SignalKind]] = frozenset(
    {
        "swap-error",
        "js-error",
        "rejection",
        "csp",
        "ce-error",
        "wasm-error",
        "wasm-panic",
        "axe-violation",
        "malformed-fragment",
        "disallowed-tag",
        "disallowed-attribute",
        "disallowed-url",
        "missing-span-id",
    }
)
"""Departures that are not matters of taste: it broke, or it was unsafe."""

CONFORMANCE_FAILURE_KINDS: Final[frozenset[SignalKind]] = frozenset(
    {
        "motion-violation",
        "unknown-class",
        "unknown-attribute",
        "invalid-attribute-value",
    }
)
"""Departures from the direction contract that a machine can check exactly."""

LAYOUT_FAILURE_KINDS: Final[frozenset[SignalKind]] = frozenset({"overflow", "layout-shift"})
"""Rendering the user feels before anyone has an opinion about the design."""

JUDGE_FAILURE_KINDS: Final[frozenset[SignalKind]] = frozenset({"judge-error"})
"""A critic that could not be heard from. Not a verdict against the artefact."""

FAILURE_KINDS: Final[frozenset[SignalKind]] = (
    CORRECTNESS_FAILURE_KINDS
    | CONFORMANCE_FAILURE_KINDS
    | LAYOUT_FAILURE_KINDS
    | JUDGE_FAILURE_KINDS
)
"""The canonical set of signal kinds this build counts as a departure.

:mod:`ui_servo.domain.evidence` refuses to hold this set on purpose: which kinds
are failures depends on the gate configuration and the rubric, both of which move
on their own schedule, and a default buried in the evidence layer would be a
policy nobody could see change. This is that policy, stated once, versioned with
the rest of the governance rules.

Observation kinds deliberately absent are absent because they are *measurements*
rather than departures -- ``longtask``, ``pixel-diff``, ``frame-timing``,
``animation``, ``interaction``, ``element-timing``, ``screenshot``,
``aria-snapshot``, ``reduced-motion``, ``swap-ok``, ``judge-response``. A
measurement becomes a failure only when a budget is set against it, and a budget
is a separate policy with a separate argument.
"""

MIN_PANEL_FAMILIES: Final[int] = 2
"""Below two families there is no panel, only a second opinion from one model."""

FAMILY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "agy",
        "anthropic",
        "bard",
        "chatgpt",
        "claude",
        "codex",
        "copilot",
        "deepseek",
        "gemini",
        "gpt",
        "google",
        "grok",
        "haiku",
        "llama",
        "mistral",
        "openai",
        "opus",
        "qwen",
        "sonnet",
        "xai",
    }
)
"""Words that would tell a critic who it is judging, or who it is.

Word-boundary matched, so a staged path such as ``/tmp/claude-a.png`` trips it as
readily as prose does -- which is the point, because the usual way blindness dies
is not a sentence naming a vendor but a filename nobody thought of as text.
"""

_FAMILY_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, FAMILY_TOKENS))) + r")\b", re.IGNORECASE
)

_SEED_BYTES: Final[int] = 8


class BlindnessError(ValueError):
    """A prompt named a model family. Raised instead of sending it.

    Not a judge failure and not recoverable at run time: it means the caller
    staged an artefact under a name that leaks its author, and the round is
    worthless until that is fixed.
    """


def family_of(judge: Any) -> Family:
    """The family of *judge*, which may be a bare family string or a port.

    Structural on purpose: policy must be usable against real judges, test
    doubles and plain strings without :mod:`ui_servo.domain` learning what a
    :class:`~ui_servo.ports.judge.JudgePort` is.
    """
    match judge:
        case str() as name:
            return name
        case _ if hasattr(judge, "family"):
            return str(judge.family)
        case _:
            raise TypeError(f"{judge!r} is neither a family name nor something with a .family")


def _normalised(family: Family) -> Family:
    return family.strip().casefold()


def eligible_judges[J](
    builder_family: Family | Collection[Family] | None, judges: Iterable[J]
) -> list[J]:
    """*judges* minus every judge drawn from a family that built a candidate.

    Accepts one family or several, because a pairwise round has two authors and
    both are disqualified: if either candidate's family sits on the panel, the
    comparison measures self-preference on one side of the pair.

    Input order is preserved, and the judges themselves are returned rather than
    their names, so the caller can hand the result straight on.
    """
    match builder_family:
        case None:
            excluded: set[Family] = set()
        case str() as single:
            excluded = {_normalised(single)}
        case several:
            excluded = {_normalised(name) for name in several}
    return [judge for judge in judges if _normalised(family_of(judge)) not in excluded]


def pair_key(left: CandidateRef, right: CandidateRef) -> PairKey:
    """An order-independent name for one comparison.

    Order-independent because the panel deliberately flips which candidate is
    shown first: keyed on presentation order, the rotation memory would think
    ``(a, b)`` and ``(b, a)`` were different pairs and let the same critic judge
    the same two artefacts twice.
    """
    first, second = sorted((left.strip(), right.strip()))
    if not first or not second:
        raise ValueError(f"a pair needs two named candidates, got {(left, right)!r}")
    if first == second:
        raise ValueError(f"a pair needs two distinct candidates, both are {first!r}")
    return f"{first}|{second}"


def _seed(*parts: str) -> int:
    digest = blake2b("\x00".join(parts).encode("utf-8"), digest_size=_SEED_BYTES).digest()
    return int.from_bytes(digest, "big")


def flip_for(round_id: RoundId, pair: PairKey, family: Family) -> bool:
    """Whether *family* sees this pair in flipped order this round.

    Randomised per judge because position bias is per model, and seeded from
    ``(round_id, pair, family)`` because a randomisation nobody can reproduce
    makes the round unauditable: given the same round the same critic sees the
    same order, on any machine, in any process.
    """
    return Random(_seed(round_id, pair, _normalised(family))).getrandbits(1) == 1


@dataclass(frozen=True, slots=True)
class RotationHistory:
    """Which family has already judged which pair.

    The panel's memory, held by the caller and threaded through rounds, so the
    freshness rule is a value that can be persisted and inspected rather than
    state hidden inside a loop.
    """

    seen: frozenset[tuple[Family, PairKey]] = frozenset()

    def has_seen(self, judge: Any, pair: PairKey) -> bool:
        return (_normalised(family_of(judge)), pair) in self.seen

    def remembering(self, judges: Any | Iterable[Any], pair: PairKey) -> Self:
        """This history plus *judges* having now seen *pair*."""
        listed = [judges] if isinstance(judges, str) or hasattr(judges, "family") else list(judges)
        return type(self)(
            seen=self.seen | {(_normalised(family_of(judge)), pair) for judge in listed}
        )

    def unseen[J](self, judges: Iterable[J], pair: PairKey) -> list[J]:
        return [judge for judge in judges if not self.has_seen(judge, pair)]

    def families_for(self, pair: PairKey) -> tuple[Family, ...]:
        return tuple(sorted(family for family, key in self.seen if key == pair))

    def __len__(self) -> int:
        return len(self.seen)


EMPTY_HISTORY: Final[RotationHistory] = RotationHistory()


def rotate[J](
    judges: Sequence[J],
    round_id: RoundId,
    *,
    pair: PairKey = "",
    history: RotationHistory = EMPTY_HISTORY,
    panel_size: int | None = None,
) -> list[J]:
    """The panel for this round: rotated deterministically, minus repeat viewers.

    Rotation is applied to the whole roster first and the memory filter second,
    so which families lead a round depends only on the round -- adding a judge
    that has already seen this pair cannot silently reshuffle everyone else.

    ``panel_size`` truncates after filtering; passing ``None`` keeps every
    eligible judge, which is what a small local roster wants.
    """
    roster = list(judges)
    if not roster:
        return []
    offset = _seed(round_id, pair) % len(roster)
    rotated = roster[offset:] + roster[:offset]
    fresh = history.unseen(rotated, pair) if pair else rotated
    return fresh[:panel_size] if panel_size is not None else fresh


def derandomize(verdict: PairwiseVerdict, was_flipped: bool) -> PairwiseVerdict:
    """Map *verdict* back onto the caller's A/B, undoing the ask-time flip.

    Applied unconditionally at the boundary rather than remembered downstream:
    every verdict that leaves the panel is already in the caller's frame, so no
    later reader has to know whether a particular critic was shown the pair
    backwards.
    """
    return verdict.swapped() if was_flipped else verdict


@dataclass(frozen=True, slots=True)
class Vote:
    """One family's derandomised verdict. Families vote, calls do not."""

    family: Family
    verdict: PairwiseVerdict

    @property
    def winner(self) -> Choice:
        return self.verdict.winner


@dataclass(frozen=True, slots=True)
class PanelOutcome:
    """What the panel decided, or that it could not.

    ``escalate`` is a first-class result, not an error path. Disagreement between
    decorrelated critics is the most informative thing a panel produces -- it is
    the signal that the bar is ambiguous here -- and averaging it into a winner
    would spend the panel's whole variety to manufacture a decision nobody made.
    """

    status: PanelStatus
    winner: Choice | None
    detail: str
    tally: Mapping[Choice, int] = field(default_factory=dict)
    voting_families: tuple[Family, ...] = ()

    def __bool__(self) -> bool:
        return self.status == "winner"

    @property
    def escalated(self) -> bool:
        return self.status == "escalate"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "winner": self.winner,
            "detail": self.detail,
            "tally": dict(self.tally),
            "voting_families": list(self.voting_families),
        }


def _as_votes(verdicts: Iterable[Vote | PairwiseVerdict]) -> list[Vote]:
    votes: list[Vote] = []
    for index, item in enumerate(verdicts):
        match item:
            case Vote():
                votes.append(item)
            case PairwiseVerdict():
                votes.append(Vote(family=f"anonymous-{index}", verdict=item))
            case other:
                raise TypeError(f"{other!r} is neither a Vote nor a PairwiseVerdict")
    return votes


def panel_outcome(verdicts: Iterable[Vote | PairwiseVerdict]) -> PanelOutcome:
    """Aggregate a round: a majority of families wins, anything else escalates.

    A strict majority of the families that actually voted -- two of three, three
    of four -- because the panel's authority comes from decorrelation, and a
    plurality among three disagreeing families is one model's opinion wearing a
    crown.

    Duplicate families are collapsed to their first vote: two calls to one family
    are one opinion sampled twice, and counting both would let a roster with two
    instances of the same model outvote the rest of the panel.

    A majority for ``tie`` escalates rather than winning. "They are equally close
    to the bar" is not something the loop can act on, and the human is the brake.
    """
    votes = _as_votes(verdicts)
    deduped: list[Vote] = []
    duplicates: list[Family] = []
    for vote in votes:
        if any(_normalised(vote.family) == _normalised(seen.family) for seen in deduped):
            duplicates.append(vote.family)
        else:
            deduped.append(vote)

    families = tuple(vote.family for vote in deduped)
    counts = tally(vote.winner for vote in deduped)
    note = f"; ignored repeat families {sorted(duplicates)}" if duplicates else ""
    spread = ", ".join(f"{choice}={counts[choice]}" for choice in CHOICES)

    if len(deduped) < MIN_PANEL_FAMILIES:
        return PanelOutcome(
            status="escalate",
            winner=None,
            detail=(
                f"only {len(deduped)} family voted; a panel needs at least "
                f"{MIN_PANEL_FAMILIES} decorrelated critics{note}"
            ),
            tally=counts,
            voting_families=families,
        )

    needed = len(deduped) // 2 + 1
    top = max(counts.values())
    leaders = [choice for choice in CHOICES if counts[choice] == top]
    if len(leaders) > 1 or top < needed:
        return PanelOutcome(
            status="escalate",
            winner=None,
            detail=(
                f"no majority among {len(deduped)} families ({spread}); "
                f"{needed} agreeing votes were needed{note}"
            ),
            tally=counts,
            voting_families=families,
        )
    if leaders[0] == "tie":
        return PanelOutcome(
            status="escalate",
            winner=None,
            detail=(
                f"{top} of {len(deduped)} families called it a tie ({spread}); "
                f"a tie is a question for the human, not a winner{note}"
            ),
            tally=counts,
            voting_families=families,
        )
    return PanelOutcome(
        status="winner",
        winner=leaders[0],
        detail=f"{top} of {len(deduped)} families chose {leaders[0]} ({spread}){note}",
        tally=counts,
        voting_families=families,
    )


def _finding_violations(findings: Any) -> Violations:
    """The citation rule, checked before pydantic so its message always leads."""
    if not isinstance(findings, list) or not findings:
        return [
            f"'findings' must be a non-empty array of at least {MIN_FINDINGS} finding "
            "object(s); name the largest gap you can see"
        ]
    found: Violations = []
    for index, finding in enumerate(findings):
        position = f"findings[{index}]"
        if not isinstance(finding, Mapping):
            found.append(f"{position} must be an object with axis, selector, gap and severity")
            continue
        selector = finding.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            found.append(
                f"{position} cites no selector; every finding must name the element it is "
                "about with a CSS selector (e.g. 'header nav a')"
            )
    return found


def _readable(error: ValidationError) -> Violations:
    found: Violations = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        message = detail["msg"].removeprefix("Value error, ")
        found.append(f"{location}: {message}")
    return found


def validate_verdict(payload: Mapping[str, Any] | None) -> PairwiseVerdict | Violations:
    """Parse a critic's JSON into a verdict, or say exactly what was wrong.

    The violation list is written to be pasted straight into a re-ask, which is
    why it is prose about the answer rather than a pydantic dump: the reader is a
    model that has one more chance to comply, and "findings.0.selector: Field
    required" is a worse instruction than naming the rule it broke.
    """
    if payload is None:
        return ["no JSON object was returned; reply with the JSON verdict object and nothing else"]
    if not isinstance(payload, Mapping):
        return [f"the reply was a {type(payload).__name__}, not a JSON object"]

    data = dict(payload)
    violations = _finding_violations(data.get("findings"))
    try:
        return PairwiseVerdict.model_validate(data)
    except ValidationError as error:
        schema_violations = [message for message in _readable(error) if message not in violations]
        return violations + schema_violations or ["the reply did not match the verdict schema"]


def blindness_violations(
    text: str, *, extra_tokens: Iterable[str] = (), masked: Iterable[str] = ()
) -> tuple[str, ...]:
    """Every place *text* names a model family, in reading order.

    ``extra_tokens`` is for the caller's own leaks -- a builder subagent's name, a
    branch, a directory -- because the tokens this module knows about are the
    vendors, and the ones that actually get people are local.

    ``masked`` is the opposite: literal substrings the caller has already judged
    to carry no authorship (an operator's ``--out`` root, a scratch directory
    named ``/tmp/claude-1000/``). They are blanked before scanning, because a
    guard that fires on the machine's own directory names is a guard people
    switch off. Masking is literal and length-preserving, so reported offsets
    still point into the original text.
    """
    literals = sorted(
        (item for item in (str(m) for m in masked) if item), key=len, reverse=True
    )
    for literal in literals:
        text = text.replace(literal, "\0" * len(literal))
    pattern = _FAMILY_TOKEN_PATTERN
    extra = [token for token in (item.strip() for item in extra_tokens) if token]
    if extra:
        pattern = re.compile(
            r"\b(?:" + "|".join(sorted(map(re.escape, FAMILY_TOKENS | set(extra)))) + r")\b",
            re.IGNORECASE,
        )
    return tuple(
        f"names {match.group(0)!r} at offset {match.start()}" for match in pattern.finditer(text)
    )


def is_blind(
    text: str, *, extra_tokens: Iterable[str] = (), masked: Iterable[str] = ()
) -> bool:
    return not blindness_violations(text, extra_tokens=extra_tokens, masked=masked)


def enforce_blindness(
    text: str,
    *,
    label: str = "prompt",
    extra_tokens: Iterable[str] = (),
    masked: Iterable[str] = (),
) -> str:
    """Return *text* if it is blind; raise :class:`BlindnessError` otherwise."""
    violations = blindness_violations(text, extra_tokens=extra_tokens, masked=masked)
    if violations:
        raise BlindnessError(
            f"{label} would tell the critic which model family it is judging: "
            + "; ".join(violations)
        )
    return text


def failing_signals(
    signals: Iterable[Signal], *, failure_kinds: Collection[SignalKind] = FAILURE_KINDS
) -> tuple[Signal, ...]:
    """:func:`~ui_servo.domain.evidence.failures_only` bound to this policy's set.

    The evidence layer requires the set to be passed; this is the one place that
    knows what it is, so call sites get the default from policy rather than
    inventing their own.
    """
    return failures_only(signals, failure_kinds=failure_kinds)
