"""Verdicts: the only shape a critic's answer is allowed to take.

The slow loop's comparator is a panel of models, and a panel is only worth its
latency if what it returns can be *aggregated*. Three essays about taste cannot
be tallied, cannot be compared to last week's, and cannot be shown to a human as
"here is where the families disagreed"; three :class:`PairwiseVerdict` values
can. This module is therefore the narrow waist between prose and control: a
critic may reason however it likes, but what leaves it is a winner, a per-axis
breakdown, and findings that each name a place in the artefact.

Three properties are load-bearing and each is a Goodhart guard.

**The rubric is multi-axis and every axis must be answered.** A single scalar
score is the easiest thing in the world to optimise against and the least
informative thing to receive; requiring all of :data:`RUBRIC_AXES` means a
candidate that wins by being loud loses on ``direction_conformance``, and the
disagreement survives into the record instead of being averaged away.

**Every finding cites a selector.** "The hierarchy feels off" is unactionable
and unfalsifiable; ``h2 + p`` is both. The required :attr:`Finding.selector` is
what turns a critique into a work item and what lets a later turn check whether
the gap was actually closed.

**A verdict is order-symmetric.** ``A`` and ``B`` are positions in one prompt,
not identities: the panel randomises which candidate is shown first precisely
because models carry position bias, so :meth:`PairwiseVerdict.swapped` exists to
map an answer back and is an involution -- swapping twice is the identity, which
is the property that makes derandomisation safe to apply blindly.

Pure domain: standard library plus pydantic. Which judges are asked, in which
order, and what is done with the tally are policy
(:mod:`ui_servo.domain.policy`); how the question travels is a port.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    WrapSerializer,
    field_validator,
    model_validator,
)

type RubricAxis = Literal[
    "hierarchy",
    "typography",
    "color",
    "motion",
    "density",
    "direction_conformance",
    "distinctiveness",
]
type Choice = Literal["A", "B", "tie"]
type Severity = Literal["major", "minor"]
type Selector = str
type Confidence = float
type JsonSchema = dict[str, Any]

RUBRIC_AXES: Final[tuple[RubricAxis, ...]] = (
    "hierarchy",
    "typography",
    "color",
    "motion",
    "density",
    "direction_conformance",
    "distinctiveness",
)
"""The axes a critic must answer, in the order they are asked.

Five of them are craft; ``direction_conformance`` is agreement with the
contract, and ``distinctiveness`` is the anti-blandness axis that exists because
every other axis can be satisfied by a competent generic page.
"""

CHOICES: Final[tuple[Choice, ...]] = ("A", "B", "tie")
SEVERITIES: Final[tuple[Severity, ...]] = ("major", "minor")

MIN_FINDINGS: Final[int] = 1
"""A verdict with no findings is a preference, not a critique."""

_PLACEHOLDER_SELECTORS: Final[frozenset[str]] = frozenset(
    {"n/a", "na", "none", "null", "-", "--", "?", "tbd", "unknown", "various", "multiple", "all"}
)
"""What a model writes when it wants to skip the citation requirement."""

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")


def _freeze[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(value))


def _thaw[K, V](value: Mapping[K, V], handler: SerializerFunctionWrapHandler) -> dict[K, V]:
    return handler(dict(value))


type FrozenMap[K, V] = Annotated[Mapping[K, V], AfterValidator(_freeze), WrapSerializer(_thaw)]


def opposite(choice: Choice) -> Choice:
    """The same answer read from the other side of the prompt."""
    match choice:
        case "A":
            return "B"
        case "B":
            return "A"
        case _:
            return "tie"


class Finding(BaseModel):
    """One named gap, in one place, on one axis.

    The unit of flow out of the slow loop. ``gap`` says what is wrong against the
    bar, ``selector`` says where, ``severity`` says whether it blocks; the triple
    is what a builder can act on without re-deriving the critic's reasoning, and
    what a later turn can check.
    """

    model_config = _MODEL_CONFIG

    axis: RubricAxis
    selector: Selector = Field(min_length=1)
    gap: str = Field(min_length=1)
    severity: Severity

    @field_validator("selector")
    @classmethod
    def _cites_a_place(cls, selector: str) -> str:
        stripped = selector.strip()
        if not stripped:
            raise ValueError("selector must name a place in the artefact")
        if stripped.casefold() in _PLACEHOLDER_SELECTORS:
            raise ValueError(
                f"selector {selector!r} is a placeholder; cite a CSS selector for the element"
            )
        return stripped

    @field_validator("gap")
    @classmethod
    def _says_something(cls, gap: str) -> str:
        stripped = gap.strip()
        if not stripped:
            raise ValueError("gap must state the departure from the bar")
        return stripped

    @property
    def blocking(self) -> bool:
        return self.severity == "major"


class PairwiseVerdict(BaseModel):
    """One critic's answer to one side-by-side comparison.

    Immutable, because it is filed into the append-only evidence stock the moment
    it arrives: a verdict that could be edited after the tally would make the
    panel a record of current opinion rather than of what was said.
    """

    model_config = _MODEL_CONFIG

    winner: Choice
    per_axis: FrozenMap[RubricAxis, Choice]
    findings: tuple[Finding, ...] = Field(min_length=MIN_FINDINGS)
    confidence: Confidence = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _rubric_is_complete(self) -> Self:
        missing = [axis for axis in RUBRIC_AXES if axis not in self.per_axis]
        if missing:
            raise ValueError(
                f"per_axis is missing {missing}; every rubric axis must be answered so that "
                "no single axis can stand in for the whole judgement"
            )
        return self

    def swapped(self) -> Self:
        """The same judgement as if the candidates had been shown the other way.

        An involution: ``verdict.swapped().swapped() == verdict``. Findings are
        carried across untouched because a finding names an axis and a selector,
        never a side; any A/B wording inside ``gap`` prose is left exactly as the
        critic wrote it, and the round record keeps the flip flag beside it so a
        reader can orient without the text having been rewritten under them.
        """
        return type(self)(
            winner=opposite(self.winner),
            per_axis={axis: opposite(choice) for axis, choice in self.per_axis.items()},
            findings=self.findings,
            confidence=self.confidence,
        )

    def axis_tally(self) -> dict[Choice, int]:
        """How the axes split, which is where a 'weak win' becomes visible."""
        counts: dict[Choice, int] = {choice: 0 for choice in CHOICES}
        for choice in self.per_axis.values():
            counts[choice] += 1
        return counts

    def axes_favouring(self, choice: Choice) -> tuple[RubricAxis, ...]:
        return tuple(axis for axis in RUBRIC_AXES if self.per_axis[axis] == choice)

    def findings_on(self, axis: RubricAxis) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.axis == axis)

    @property
    def blocking_findings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def selectors(self) -> tuple[Selector, ...]:
        """Every place this critic pointed at, first-seen order."""
        return tuple(dict.fromkeys(finding.selector for finding in self.findings))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        return cls.model_validate(dict(data))

    def to_mapping(self) -> dict[str, Any]:
        """Plain data for the evidence payload; no consumer touches pydantic."""
        return self.model_dump(mode="python")


def _choice_schema(description: str) -> JsonSchema:
    return {"type": "string", "enum": list(CHOICES), "description": description}


def verdict_response_schema() -> JsonSchema:
    """A fresh JSON Schema for :attr:`JudgeRequest.response_schema`.

    Built from :data:`RUBRIC_AXES` rather than written out beside it, so a new
    axis cannot be added to the rubric while the critics keep being asked the old
    question. Deliberately flat -- no ``$ref``, no ``$defs`` -- because it is
    handed to CLI agents whose structured-output modes accept only a restricted
    subset of JSON Schema, and a schema one family silently ignores is a schema
    that buys nothing.

    Returned fresh each call: adapters serialise, wrap and occasionally annotate
    it, and a shared nested dict would let one family's adapter edit the question
    the next family is asked.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["winner", "per_axis", "findings", "confidence"],
        "properties": {
            "winner": _choice_schema(
                "Which candidate is closer to the bar overall: 'A', 'B', or 'tie'."
            ),
            "per_axis": {
                "type": "object",
                "additionalProperties": False,
                "required": list(RUBRIC_AXES),
                "properties": {
                    axis: _choice_schema(f"Which candidate is stronger on {axis}.")
                    for axis in RUBRIC_AXES
                },
                "description": "Every axis must be answered; no axis may be omitted.",
            },
            "findings": {
                "type": "array",
                "minItems": MIN_FINDINGS,
                "description": (
                    "The concrete gaps against the bar. Every finding must cite a CSS "
                    "selector for the element it is about."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["axis", "selector", "gap", "severity"],
                    "properties": {
                        "axis": {"type": "string", "enum": list(RUBRIC_AXES)},
                        "selector": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "CSS selector for the offending element, e.g. "
                                "'header nav a' or 'section:nth-of-type(2) h2'. "
                                "Required; 'n/a' is not acceptable."
                            ),
                        },
                        "gap": {
                            "type": "string",
                            "minLength": 1,
                            "description": "What departs from the bar, stated concretely.",
                        },
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                    },
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "How sure you are, 0 to 1.",
            },
        },
    }


VERDICT_RESPONSE_SCHEMA: Final[JsonSchema] = verdict_response_schema()
"""The canonical response schema, for call sites that only read it.

Anything that serialises, mutates or hands it to an adapter should call
:func:`verdict_response_schema` for its own copy.
"""


def schema_axes(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """The axes *schema* actually demands, for asserting it tracks the rubric."""
    per_axis = schema.get("properties", {}).get("per_axis", {})
    return tuple(per_axis.get("required", ()))


def tally(choices: Iterable[Choice]) -> dict[Choice, int]:
    """Count *choices* over the full vocabulary, so a zero is stated not implied."""
    counts: dict[Choice, int] = {choice: 0 for choice in CHOICES}
    for choice in choices:
        counts[choice] += 1
    return counts
