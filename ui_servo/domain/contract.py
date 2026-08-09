"""The direction contract: the reference signal of the ui-servo control loop.

In control terms every other part of the system is defined against this module.
The contract is the *setpoint* a regulator compares observation to; the gates and
critics are comparators that measure error against it; the builders are actuators
that move the artefact toward it. Conant-Ashby says a good regulator must contain
a model of the system it regulates -- for taste, that model is exactly this file
plus the exemplars the human has picked, which is why the contract is versioned
data rather than prose in a prompt.

The module is pure domain: standard library plus pydantic, no ports, no adapters,
no filesystem. It accepts already-parsed mappings or TOML *text*; reading bytes
off a disk is an adapter concern and lives in the ``__main__`` block below.

Three derivations leave this module, one per consumer:

* :meth:`DirectionContract.to_css_custom_properties` -- the token sheet the site
  is built from, so "on-contract" is the path of least resistance for a builder.
* :meth:`DirectionContract.motion_table` -- the machine-checkable subset the
  in-browser probe and the pre-ship gates enforce; motion is where generated UI
  drifts first and it is cheap to check exactly.
* :meth:`DirectionContract.class_allowlist_seed` -- the seed vocabulary for the
  sanitiser's class allowlist, so unknown utility classes fail closed.

Two properties the rest of the system relies on. **The parsed contract is deeply
immutable**: nested mappings are closed over on validation, because a setpoint
that can be edited after validation is not a setpoint. And **contract text that
reaches CSS is treated as untrusted**: strings destined for the token sheet are
pattern-restricted at parse time and escaped again at emission, so the reference
signal cannot become an injection vector into the artefact it governs.

The CLI resolves its contract from ``--contract``, then ``$UI_SERVO_CONTRACT``
(when set and non-empty), then the copy hatch force-includes into the wheel at
``ui_servo/domain/data/direction.toml``, then ``direction/direction.toml`` in a
source checkout; with none of those it exits 2 and asks for ``--contract``. An
explicit choice is never quietly downgraded: if ``--contract`` or the environment
variable names something unreadable, the CLI exits rather than validating a
different document and reporting "ok" for a contract nobody asked about.
"""

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    StringConstraints,
    WrapSerializer,
    field_validator,
    model_validator,
)


def _freeze[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    """Close a validated mapping so the setpoint cannot be edited after the fact.

    Validation that can be walked around after construction is decoration. A
    contract field left as a plain ``dict`` lets any caller write a 5000 ms
    duration straight past the validator and into every derivation.
    """
    return MappingProxyType(dict(value))


def _thaw[K, V](value: Mapping[K, V], handler: SerializerFunctionWrapHandler) -> dict[K, V]:
    return handler(dict(value))


def _real_number(value: Any) -> Any:
    """Reject anything that is not already a number.

    Coercion is the enemy of a reference signal: ``version = "1"`` or
    ``ratio = true`` silently becoming valid means the contract the operator
    wrote and the contract the loop steers by are two different documents.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    return value


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("value must be finite (nan and inf are not steerable setpoints)")
    return value


type CssText = str
type ClassName = str
type ColorName = str
type Milliseconds = StrictInt
type RawMapping = Mapping[str, Any]
type Number = Annotated[float, BeforeValidator(_real_number), AfterValidator(_finite)]
type StepExponent = Annotated[StrictInt, Field(ge=-16, le=16)]
type FrozenMap[K, V] = Annotated[Mapping[K, V], AfterValidator(_freeze), WrapSerializer(_thaw)]
type TokenKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
type CommentSafeText = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._+:-]*$", max_length=120)
]
type FontFamilyName = Annotated[
    str, StringConstraints(pattern=r"^-?[A-Za-z0-9][A-Za-z0-9 _-]*$", max_length=64)
]

SUPPORTED_VERSION: Literal[2] = 2
ANIMATABLE_PROPERTIES: frozenset[str] = frozenset({"transform", "opacity"})
EASING_KEYWORDS: frozenset[str] = frozenset({"linear"})
ANCHOR_STEP: str = "base"
MAX_DURATION_MS: int = 600
MAX_AXIS_VALUE: float = 10_000.0
MIN_TEXT_BACKGROUND_LIGHTNESS_GAP: float = 0.45

_NUMBER = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_CUBIC_BEZIER = re.compile(
    rf"^cubic-bezier\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)$"
)
_CSS_IDENT = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")
_CSS_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_CSS_UNSAFE_IN_VALUE = re.compile(r"[^A-Za-z0-9 ().,%-]")
_CSS_UNSAFE_IN_IDENT = re.compile(r"[^A-Za-z0-9_-]")
_SPACING_PREFIXES: tuple[str, ...] = ("p", "px", "py", "m", "mx", "my", "gap")

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


def _css_comment_text(text: str) -> str:
    """Neutralise any sequence that could close the comment it is embedded in.

    The token sheet is generated, so contract text reaches a stylesheet. Text
    that can terminate a comment can open a rule, which turns the reference
    signal into an injection vector. Validators reject this shape at parse time;
    this is the second wall, for models built by any other route.
    """
    stripped = _CSS_CONTROL_CHARS.sub(" ", text)
    return stripped.replace("*/", "* /").replace("/*", "/ *")


def _css_string(text: str) -> str:
    """Render *text* as a CSS string literal, escaped so it cannot break out."""
    escaped = _CSS_CONTROL_CHARS.sub("", text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _css_ident(name: str) -> str:
    """Reduce a token name to a bare CSS identifier.

    Names become custom-property names and class names, so a name is as much an
    injection surface as a value. The parse-time pattern already forbids
    anything but ``[a-z0-9-]``; this is the emission-time wall that holds even
    for a model assembled by a route that skipped validation.
    """
    return _CSS_UNSAFE_IN_IDENT.sub("", name) or "invalid"


def _css_safe_value(text: str) -> str:
    """Strip anything that could end a declaration or open a block.

    A no-op on every value the validators admit (easings, keyword lists); the
    point is that it stays a no-op even if a model is built by a route that
    skipped them.
    """
    return _CSS_UNSAFE_IN_VALUE.sub("", text)


def _fmt(value: float) -> str:
    """Render a float for CSS: fixed precision, no trailing zeros, no ``-0``."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-", "-0"} else text


def _length(px: float, rem_base_px: float) -> str:
    match px:
        case 0:
            return "0"
        case _:
            return f"{_fmt(px / rem_base_px)}rem"


class LchColor(BaseModel):
    """A colour held in perceptual (OK)LCH space.

    Lightness and chroma are separable here, so a critic can say "same hue,
    less chroma" and a builder can act on it -- the reason the contract does not
    store hex.
    """

    model_config = _MODEL_CONFIG

    lightness: Number = Field(alias="l", ge=0.0, le=1.0)
    chroma: Number = Field(alias="c", ge=0.0, le=0.5)
    hue: Number = Field(alias="h", ge=0.0, lt=360.0)
    alpha: Number = Field(default=1.0, ge=0.0, le=1.0)

    def to_css(self) -> CssText:
        channels = f"{_fmt(self.lightness)} {_fmt(self.chroma)} {_fmt(self.hue)}"
        match self.alpha:
            case 1:
                return f"oklch({channels})"
            case alpha:
                return f"oklch({channels} / {_fmt(alpha)})"


class Palette(BaseModel):
    """The colour axis of the contract.

    Roles, not swatch names: a builder asks for ``surface``, never for "the dark
    grey", which keeps the mapping from contract to artefact one-to-one and
    therefore checkable.
    """

    model_config = _MODEL_CONFIG

    space: Literal["oklch"] = "oklch"
    background: LchColor
    surface: LchColor
    text: LchColor
    muted: LchColor
    border: LchColor
    accent: LchColor
    accent_2: LchColor = Field(alias="accent-2")
    critical: LchColor

    @model_validator(mode="after")
    def _legible(self) -> Self:
        gap = abs(self.text.lightness - self.background.lightness)
        if gap < MIN_TEXT_BACKGROUND_LIGHTNESS_GAP:
            raise ValueError(
                f"text/background lightness gap {gap:.3f} < {MIN_TEXT_BACKGROUND_LIGHTNESS_GAP}: "
                "legibility is correctness and may not be traded against taste"
            )
        if self.surface == self.background:
            raise ValueError("surface must be distinguishable from background")
        return self

    def entries(self) -> tuple[tuple[ColorName, LchColor], ...]:
        """Colour roles in declaration order, using contract (kebab) names."""
        return (
            ("background", self.background),
            ("surface", self.surface),
            ("text", self.text),
            ("muted", self.muted),
            ("border", self.border),
            ("accent", self.accent),
            ("accent-2", self.accent_2),
            ("critical", self.critical),
        )

    def color_scheme(self) -> Literal["dark", "light"]:
        return "dark" if self.background.lightness < 0.5 else "light"


class FontFamily(BaseModel):
    """One family plus its fallback stack, kept together so no builder invents one."""

    model_config = _MODEL_CONFIG

    name: FontFamilyName
    fallbacks: tuple[FontFamilyName, ...] = Field(min_length=1)

    def to_css_stack(self) -> CssText:
        return ", ".join(_quote_family(part) for part in (self.name, *self.fallbacks))


def _quote_family(name: str) -> str:
    return name if _CSS_IDENT.fullmatch(name) else _css_string(name)


class ModularScale(BaseModel):
    """A geometric scale: named steps as exponents of one ratio over one base.

    Storing exponents rather than pixel values is what makes the scale a *scale*
    -- retuning ``ratio`` moves every token coherently, so drift shows up as a
    contract revision instead of a pile of one-off magic numbers.
    """

    model_config = _MODEL_CONFIG

    base_px: Number = Field(gt=0.0, le=200.0)
    ratio: Number = Field(gt=1.0, le=2.5)
    steps: FrozenMap[TokenKey, StepExponent] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _anchored(cls, steps: Mapping[str, int]) -> Mapping[str, int]:
        if steps.get(ANCHOR_STEP) != 0:
            raise ValueError(f"scale must define a {ANCHOR_STEP!r} step with exponent 0")
        if len(set(steps.values())) != len(steps):
            raise ValueError("scale steps must have distinct exponents")
        return steps

    def size_px(self, step: str) -> float:
        try:
            exponent = self.steps[step]
        except KeyError:
            raise KeyError(f"unknown step {step!r}; known: {sorted(self.steps)}") from None
        return self.base_px * self.ratio**exponent


class TypeScale(ModularScale):
    """The type axis: the modular scale plus the faces it is set in."""

    rem_base_px: Number = Field(default=16.0, ge=1.0, le=100.0)
    display_family: FontFamily
    text_family: FontFamily
    mono_family: FontFamily | None = None

    def families(self) -> tuple[tuple[str, FontFamily], ...]:
        named = (("display", self.display_family), ("text", self.text_family))
        match self.mono_family:
            case None:
                return named
            case mono:
                return (*named, ("mono", mono))


class Motion(BaseModel):
    """The motion axis, and the only axis the runtime can be policed on exactly.

    ``getAnimations()`` in the probe returns durations, easings and animated
    properties; comparing that observation to :meth:`MotionTable` is a pure
    set-membership test, which is why motion sits in the fast deterministic loop
    rather than the slow critic loop.

    An easing is either a ``cubic-bezier()`` with both x control points inside
    ``0..1``, or one of the keywords in :data:`EASING_KEYWORDS` -- which today
    means ``linear`` and nothing else. ``linear`` is admitted deliberately, not
    by oversight: it is what the browser reports for an unshaped transition, so
    the probe needs it expressible in this table to compare against. The named
    keywords (``ease``, ``ease-in-out``, ...) stay out, because their curves are
    unstated and the whole point of the motion axis is that the curve is a
    number the gates can check.
    """

    model_config = _MODEL_CONFIG

    durations_ms: FrozenMap[TokenKey, Milliseconds] = Field(min_length=1)
    easings: FrozenMap[TokenKey, str] = Field(min_length=1)
    animatable_properties: tuple[str, ...] = Field(min_length=1)
    reduced_motion_required: StrictBool

    @field_validator("durations_ms")
    @classmethod
    def _sane_durations(cls, durations: Mapping[str, int]) -> Mapping[str, int]:
        for name, value in durations.items():
            if not 0 <= value <= MAX_DURATION_MS:
                raise ValueError(f"duration {name!r}={value}ms outside 0..{MAX_DURATION_MS}ms")
        return durations

    @field_validator("easings")
    @classmethod
    def _valid_easings(cls, easings: Mapping[str, str]) -> Mapping[str, str]:
        for name, value in easings.items():
            if value in EASING_KEYWORDS:
                continue
            match _CUBIC_BEZIER.fullmatch(value):
                case None:
                    raise ValueError(
                        f"easing {name!r}={value!r} is neither a cubic-bezier() nor one of "
                        f"the permitted keywords {sorted(EASING_KEYWORDS)}"
                    )
                case found:
                    x1, _, x2, _ = (float(group) for group in found.groups())
                    if not (0.0 <= x1 <= 1.0 and 0.0 <= x2 <= 1.0):
                        raise ValueError(
                            f"easing {name!r}={value!r} has an x control point outside 0..1"
                        )
        return easings

    @field_validator("animatable_properties")
    @classmethod
    def _compositor_only(cls, properties: tuple[str, ...]) -> tuple[str, ...]:
        illegal = sorted(set(properties) - ANIMATABLE_PROPERTIES)
        if illegal:
            raise ValueError(
                f"animatable properties {illegal} are not compositor-only; "
                f"allowed: {sorted(ANIMATABLE_PROPERTIES)}"
            )
        return properties

    @field_validator("reduced_motion_required")
    @classmethod
    def _reduced_motion_is_not_optional(cls, required: bool) -> bool:
        if not required:
            raise ValueError("a v1 contract must require a prefers-reduced-motion branch")
        return required


@dataclass(frozen=True, slots=True)
class MotionTable:
    """The flattened, comparator-facing view of :class:`Motion`.

    Deliberately not a pydantic model: this crosses into the gates and the probe
    beacon decoder as a plain immutable value with set semantics, and nothing
    downstream should be able to mutate the setpoint it is judging against.
    """

    durations_ms: frozenset[Milliseconds]
    easings: frozenset[str]
    animatable_properties: frozenset[str]
    reduced_motion_required: bool

    @property
    def max_duration_ms(self) -> Milliseconds:
        return max(self.durations_ms)

    def violations(
        self, *, duration_ms: Milliseconds, easing: str, property_name: str
    ) -> tuple[str, ...]:
        """Every way one observed animation departs from the contract."""
        found: list[str] = []
        if duration_ms not in self.durations_ms:
            found.append(f"duration {duration_ms}ms not in {sorted(self.durations_ms)}")
        if easing not in self.easings:
            found.append(f"easing {easing!r} not in contract")
        if property_name not in self.animatable_properties:
            found.append(f"property {property_name!r} is not animatable under this contract")
        return tuple(found)

    def permits(self, *, duration_ms: Milliseconds, easing: str, property_name: str) -> bool:
        return not self.violations(
            duration_ms=duration_ms, easing=easing, property_name=property_name
        )


class AxisBounds(BaseModel):
    """Bounds for one quality-diversity axis.

    MAP-Elites needs a behaviour space to spread over; these are its walls. The
    default is the on-contract centre, the range is how far exploration may go
    before it is off-contract by definition rather than by opinion.
    """

    model_config = _MODEL_CONFIG

    min: Number = Field(ge=-MAX_AXIS_VALUE, le=MAX_AXIS_VALUE)
    max: Number = Field(ge=-MAX_AXIS_VALUE, le=MAX_AXIS_VALUE)
    default: Number = Field(ge=-MAX_AXIS_VALUE, le=MAX_AXIS_VALUE)
    unit: Literal["", "ch", "px", "rem", "ms"] = ""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.min < self.max:
            raise ValueError(f"axis bounds require min < max, got {self.min} >= {self.max}")
        if not self.min <= self.default <= self.max:
            raise ValueError(f"axis default {self.default} outside [{self.min}, {self.max}]")
        return self

    def clamp(self, value: float) -> float:
        return min(max(value, self.min), self.max)

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max

    def to_css_value(self) -> CssText:
        return f"{_fmt(self.default)}{self.unit}"


class Reference(BaseModel):
    """A named artefact the bar is set against (or explicitly away from).

    The Gauntlet Loop invariant "the bar is concrete" is enforced here: a critic
    is pointed at these, never asked whether something is "good UI".
    """

    model_config = _MODEL_CONFIG

    name: str = Field(min_length=1)
    note: str = Field(min_length=1)
    url: str | None = None
    axes: tuple[str, ...] = ()


class Meta(BaseModel):
    """Identity and version of the reference signal. Changing taste = bumping this."""

    model_config = _MODEL_CONFIG

    version: StrictInt = Field(ge=1)
    name: TokenKey
    revision: CommentSafeText
    owner: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("version")
    @classmethod
    def _supported(cls, version: int) -> int:
        match version:
            case 2:
                return version
            case other:
                raise ValueError(f"contract version {other} unsupported; this build reads v{SUPPORTED_VERSION}")


class DirectionContract(BaseModel):
    """The whole reference signal, parsed and validated.

    Construct it from parsed data (:meth:`from_mapping`) or from TOML text
    (:meth:`from_toml`); it never touches a filesystem itself.
    """

    model_config = _MODEL_CONFIG

    meta: Meta
    palette: Palette
    type_scale: TypeScale = Field(alias="type")
    spacing: ModularScale
    motion: Motion
    density: FrozenMap[TokenKey, AxisBounds] = Field(min_length=1)
    references: tuple[Reference, ...] = Field(min_length=1)
    anti_references: tuple[Reference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bar_is_unambiguous(self) -> Self:
        overlap = {reference.name for reference in self.references} & {
            reference.name for reference in self.anti_references
        }
        if overlap:
            raise ValueError(f"{sorted(overlap)} listed as both reference and anti-reference")
        return self

    @classmethod
    def from_toml(cls, text: str) -> Self:
        """Parse TOML *text*. Getting that text is the caller's (adapter's) problem."""
        return cls.model_validate(tomllib.loads(text))

    @classmethod
    def from_mapping(cls, data: RawMapping) -> Self:
        return cls.model_validate(dict(data))

    def to_mapping(self) -> dict[str, Any]:
        """Round-trip partner of :meth:`from_mapping`, keyed by contract names."""
        return self.model_dump(mode="python", by_alias=True)

    def motion_table(self) -> MotionTable:
        return MotionTable(
            durations_ms=frozenset(self.motion.durations_ms.values()),
            easings=frozenset(self.motion.easings.values()),
            animatable_properties=frozenset(self.motion.animatable_properties),
            reduced_motion_required=self.motion.reduced_motion_required,
        )

    def class_allowlist_seed(self) -> frozenset[ClassName]:
        """Utility classes implied by the tokens.

        Fail-closed vocabulary for the Tier 0 sanitiser: a class that no token
        can justify is not an aesthetic disagreement, it is an unreviewed
        escape hatch out of the contract.
        """
        names: set[ClassName] = set()
        for color, _ in self.palette.entries():
            role = _css_ident(color)
            names.update({f"bg-{role}", f"text-{role}", f"border-{role}"})
        names.update(f"type-{_css_ident(step)}" for step in self.type_scale.steps)
        names.update(
            f"{prefix}-{_css_ident(step)}"
            for step in self.spacing.steps
            for prefix in _SPACING_PREFIXES
        )
        names.update(f"duration-{_css_ident(name)}" for name in self.motion.durations_ms)
        names.update(f"ease-{_css_ident(name)}" for name in self.motion.easings)
        return frozenset(names)

    def to_css_custom_properties(self) -> CssText:
        """Emit ``tokens.css``: the contract as the site's only source of values.

        Tokens are the actuator's affordance. If every colour, size, duration and
        easing a builder can reach for is already on-contract, conformance stops
        being a thing to police after the fact.
        """
        rem_base = self.type_scale.rem_base_px
        lines: list[str] = [
            "/* Generated by ui_servo.domain.contract from direction/direction.toml. */",
            f"/* Contract: {_css_comment_text(self.meta.name)} v{self.meta.version:d} "
            f"(rev {_css_comment_text(self.meta.revision)}). */",
            "/* Do not edit by hand; edit the contract and re-emit. */",
            "",
            ":root {",
            f"  color-scheme: {self.palette.color_scheme()};",
            "",
            "  /* colour */",
        ]
        lines += [
            f"  --color-{_css_ident(name)}: {color.to_css()};"
            for name, color in self.palette.entries()
        ]

        lines += ["", "  /* type */"]
        lines += [
            f"  --type-family-{_css_ident(role)}: {family.to_css_stack()};"
            for role, family in self.type_scale.families()
        ]
        lines.append(f"  --type-ratio: {_fmt(self.type_scale.ratio)};")
        lines += [
            f"  --type-{_css_ident(step)}: {_length(self.type_scale.size_px(step), rem_base)};"
            for step in self.type_scale.steps
        ]

        lines += ["", "  /* spacing */"]
        lines += [
            f"  --space-{_css_ident(step)}: {_length(self.spacing.size_px(step), rem_base)};"
            for step in self.spacing.steps
        ]

        lines += ["", "  /* motion */"]
        lines += [
            f"  --motion-duration-{_css_ident(name)}: {value:d}ms;"
            for name, value in self.motion.durations_ms.items()
        ]
        lines += [
            f"  --motion-ease-{_css_ident(name)}: {_css_safe_value(value)};"
            for name, value in self.motion.easings.items()
        ]
        lines.append(
            "  --motion-animatable: "
            f"{', '.join(_css_safe_value(name) for name in self.motion.animatable_properties)};"
        )

        lines += ["", "  /* density (quality-diversity axis defaults) */"]
        lines += [
            f"  --density-{_css_ident(axis)}: {bounds.to_css_value()};"
            for axis, bounds in self.density.items()
        ]
        lines.append("}")

        if self.motion.reduced_motion_required:
            lines += [
                "",
                "@media (prefers-reduced-motion: reduce) {",
                "  :root {",
            ]
            lines += [
                f"    --motion-duration-{_css_ident(name)}: 0ms;"
                for name in self.motion.durations_ms
            ]
            lines += ["  }", "}"]

        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        """One screen of what the loop is currently steering toward."""
        table = self.motion_table()
        return "\n".join(
            [
                f"contract     {self.meta.name} v{self.meta.version} (rev {self.meta.revision}, owner {self.meta.owner})",
                f"description  {self.meta.description}",
                f"palette      {self.palette.space}, {self.palette.color_scheme()} scheme, "
                f"{len(self.palette.entries())} roles, accent {self.palette.accent.to_css()}",
                f"type         ratio {_fmt(self.type_scale.ratio)} on {_fmt(self.type_scale.base_px)}px, "
                f"{len(self.type_scale.steps)} steps, display {self.type_scale.display_family.name!r}",
                f"spacing      ratio {_fmt(self.spacing.ratio)} on {_fmt(self.spacing.base_px)}px, "
                f"{len(self.spacing.steps)} steps",
                f"motion       {sorted(table.durations_ms)} ms, {len(table.easings)} easings, "
                f"animatable {sorted(table.animatable_properties)}, "
                f"reduced-motion required {table.reduced_motion_required}",
                f"density      {', '.join(f'{axis} [{_fmt(b.min)}..{_fmt(b.max)}]' for axis, b in self.density.items())}",
                f"references   {', '.join(r.name for r in self.references)}",
                f"anti         {', '.join(r.name for r in self.anti_references)}",
                f"allowlist    {len(self.class_allowlist_seed())} seed classes",
            ]
        )


if __name__ == "__main__":
    import argparse
    import os
    import sys
    from importlib.resources import files
    from pathlib import Path
    from typing import NoReturn

    ENV_VAR = "UI_SERVO_CONTRACT"
    PACKAGED_LABEL = "ui_servo.domain/data/direction.toml (packaged)"
    CHECKOUT_CONTRACT = Path(__file__).resolve().parents[2] / "direction" / "direction.toml"

    def _fail(message: str, code: int) -> NoReturn:
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(code)

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            _fail(f"cannot read contract {path}: {error}", 2)

    def _packaged_contract() -> tuple[str, str] | None:
        """The build-time copy that ships inside the wheel, if this is a wheel.

        Read through the ``Traversable`` rather than a filesystem path: the
        resource is not guaranteed to be a real file, and a path borrowed from a
        context manager is only valid inside it.
        """
        try:
            resource = files("ui_servo.domain") / "data" / "direction.toml"
            if not resource.is_file():
                return None
            return PACKAGED_LABEL, resource.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ModuleNotFoundError, TypeError):
            return None

    def _resolve_contract(explicit: Path | None) -> tuple[str, str]:
        """Find the contract to validate, and say exactly which one it was.

        Precedence is by explicitness, and an explicit choice is never silently
        downgraded: if ``--contract`` or ``$UI_SERVO_CONTRACT`` names a file that
        cannot be read, this exits rather than validating some other document
        the operator did not name. Falling through would mean reporting "ok" for
        a contract nobody asked about, which is worse than failing.
        """
        match explicit:
            case None:
                pass
            case path:
                return str(path), _read(path)
        match os.environ.get(ENV_VAR):
            case None | "":
                pass
            case override:
                path = Path(override)
                if not path.is_file():
                    _fail(f"{ENV_VAR}={override!r} does not name a readable file", 2)
                return str(path), _read(path)
        match _packaged_contract():
            case None:
                pass
            case packaged:
                return packaged
        if CHECKOUT_CONTRACT.is_file():
            return str(CHECKOUT_CONTRACT), _read(CHECKOUT_CONTRACT)
        _fail(
            f"no direction contract found. Pass --contract PATH or set {ENV_VAR}. "
            f"Searched: {PACKAGED_LABEL}, {CHECKOUT_CONTRACT}",
            2,
        )

    parser = argparse.ArgumentParser(
        prog="python -m ui_servo.domain.contract",
        description="Validate the direction contract and derive its artefacts.",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=None,
        help=(
            f"path to the contract TOML; without it, ${ENV_VAR} (when set and non-empty) "
            "is authoritative, then the packaged copy, then direction/direction.toml in a "
            "source checkout"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="parse, round-trip through a plain mapping, and print a summary",
    )
    parser.add_argument(
        "--emit-css",
        type=Path,
        metavar="PATH",
        help="write the token sheet to PATH ('-' for stdout)",
    )
    args = parser.parse_args()

    label, source = _resolve_contract(args.contract)

    try:
        contract = DirectionContract.from_toml(source)
    except (ValueError, tomllib.TOMLDecodeError) as error:
        _fail(f"invalid contract {label}:\n{error}", 1)

    if args.check or args.emit_css is None:
        try:
            round_tripped = DirectionContract.from_mapping(contract.to_mapping())
        except ValueError as error:
            _fail(f"contract does not round-trip:\n{error}", 1)
        if round_tripped != contract:
            _fail("round-trip produced a different contract", 1)
        print(f"ok {label}")
        print(contract.summary())

    match args.emit_css:
        case None:
            pass
        case destination if str(destination) == "-":
            sys.stdout.write(contract.to_css_custom_properties())
        case destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(contract.to_css_custom_properties(), encoding="utf-8")
            print(f"wrote {destination}")

    raise SystemExit(0)
