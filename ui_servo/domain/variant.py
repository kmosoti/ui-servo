"""Variants and the style vector: what a candidate *is*, and where it sits in style space.

A :class:`Variant` is one actuator output -- one builder's answer to one brief for
one part of the site. It is deliberately inert: html plus provenance, no verdict,
no score. Everything that judges it lives elsewhere, because a candidate that
carried its own grade would let the implementer self-grade, which is the one thing
the gauntlet forbids.

:class:`StyleVector` is the measurement side. The direction contract says what the
site should look like, but "looks generic" is not a rule you can write down as a
predicate -- it is a *distance*. The vector turns one rendered fragment into a
fixed-length, deterministic point so that distance becomes arithmetic: an
anti-corpus of stock templates is embedded once, and any candidate can be asked how
far it stands from the nearest one. :func:`blandness` is exactly that question, and
a LOW answer means bland -- the candidate landed on top of the corpus median.

The embedding is deliberately coarse and perceptual rather than pixel-exact. It bins
colour in OKLCH (a space where equal steps look equal), quantises spacing and type
against the contract's own modular scales so that "off-scale" is a measurable event
rather than an opinion, buckets corner radius, and summarises contrast. Fine detail
is what a critic panel is for; this vector exists to answer one cheap question --
*have we seen this before?* -- for every candidate, in microseconds, with no model
in the loop.

**Why the vector is not a verdict.** Blandness is taste. It never enters
:attr:`~ui_servo.control.regulator.RegulatorReport.passed`, which is derived from
hard gates only. A distinctive fragment with a contrast failure is rejected; a
correct but median fragment survives the fast loop and gets argued about in the slow
one. Keeping those two on separate wires is what stops the loop from Goodharting
itself into weird-but-broken UI.

Pure domain: standard library plus pydantic. No browser, no image library, no colour
science -- the sRGB-to-OKLCH conversion and the pixel histogram both happen in the
sensor adapter, which is the only place that has the pixels.

The MAP-Elites archive lives in the second half of this module, below the seam:
:class:`StyleAxes` locates a rendered fragment in a three-axis *behaviour* space
derived from the vector's public surface, and :class:`EliteArchive` keeps the
fittest variant per cell. It sits here rather than in its own module because it is
the same measurement read a second way -- style space asks how far apart two
candidates are, behaviour space asks which corner each of them is in -- and the
archive's one hard rule (gates are a precondition of entry, never a term in
fitness) is the same separation the first half exists to hold.
"""

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ui_servo.domain.contract import DirectionContract, ModularScale

type VariantId = str
type PartName = str
type BuilderFamily = str
type FragmentHtml = str
type Selector = str
type CellCoord = tuple[int, int, int]
"""A MAP-Elites grid cell. Defined here in U10 so :class:`Variant` can carry a hint
at the cell a brief was aimed at; the archive that gives coordinates their meaning
arrives with the seam below."""

type StyleSampleMapping = Mapping[str, Any]
"""The raw ``page.evaluate`` payload, before :meth:`StyleSample.parse` validates it."""

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# The StyleSample contract with the sensor adapter                             #
# --------------------------------------------------------------------------- #
#
# The adapter renders the fragment, walks the DOM, and returns ONE JSON object of
# the shape below. Everything expensive or impure -- getComputedStyle, the
# sRGB -> OKLCH conversion, the WCAG contrast maths, decoding the screenshot into a
# histogram -- happens there. The domain receives numbers and only ever bins them.
#
#   {
#     "span_id": "hero",
#     "viewport": {"width": 1280, "height": 800},
#     "elements": [
#       {
#         "selector":        "section > h1",   # for a human reading a report
#         "area_px":         41_600.0,         # getBoundingClientRect w*h, 0 if hidden
#         "color":           {"l": 0.94, "c": 0.014, "h": 84.0},   # OKLCH, l in [0,1],
#         "background":      {"l": 0.16, "c": 0.022, "h": 52.0},   # c >= 0, h in [0,360)
#         "font_size_px":    42.5,
#         "border_radius_px": 0.0,             # max of the four corners, px
#         "spacing_px":      [24.0, 24.0, 12.0],  # resolved padding/margin/gap, px, > 0 only
#         "contrast_ratio":  11.8              # WCAG 2.x ratio of color on background
#       },
#       ...
#     ],
#     "screenshot": {"oklch_bins": [ ... 24 counts ... ]}   # optional
#   }
#
# ``background`` is the nearest ancestor's opaque background, because that is what
# the pixels actually show and what the contrast ratio was computed against.
# ``screenshot.oklch_bins`` is 24 pixel counts in exactly the order of
# :data:`COLOR_DIMENSIONS` -- hue-major, lightness-minor -- so the domain never has
# to know a PNG exists. It is optional: computed styles alone give a usable vector,
# the screenshot only corrects for how much of the viewport each colour really won.


HUE_BINS: Final[int] = 8
LIGHTNESS_BINS: Final[int] = 3
COLOR_BINS: Final[int] = HUE_BINS * LIGHTNESS_BINS

LIGHTNESS_EDGES: Final[tuple[float, float]] = (0.35, 0.70)
"""Dark / mid / light cuts on the OKLCH L axis."""

RADIUS_EDGES: Final[tuple[float, ...]] = (0.0, 2.0, 4.0, 8.0, 24.0)
"""Corner-radius bucket edges in px; the sixth bucket is everything above 24 (pills).

Sharp (0) gets a bucket of its own because it is a *decision*, and the 4-8px band
gets one because that is where every default component library lives.
"""
RADIUS_BINS: Final[int] = len(RADIUS_EDGES) + 1

CONTRAST_EDGES: Final[tuple[float, ...]] = (3.0, 4.5, 7.0, 12.0)
"""WCAG bands: fail, large-text AA, AA, AAA, and beyond."""
CONTRAST_BINS: Final[int] = len(CONTRAST_EDGES) + 1

SCALE_MIN_EXPONENT: Final[int] = -2
SCALE_MAX_EXPONENT: Final[int] = 5
SCALE_HIT_BINS: Final[int] = SCALE_MAX_EXPONENT - SCALE_MIN_EXPONENT + 1
SCALE_BINS: Final[int] = SCALE_HIT_BINS + 2
"""Hit buckets per scale step, plus a below-base miss and an at-or-above-base miss."""

SCALE_TOLERANCE: Final[float] = 0.04
"""Relative slack when deciding whether a measured px value *is* a scale step.

Sub-pixel layout and rem rounding move real values by a fraction of a percent, so a
strict equality test would score a conforming fragment as entirely off-scale. Four
percent is wide enough to absorb that and narrow enough that a 16px value cannot pass
as a 18px step.
"""

SCREENSHOT_COLOR_SHARE: Final[float] = 0.5
"""How much of the colour block the pixel histogram wins when both sources exist.

The two disagree usefully: computed styles know the *intent* of every element,
including ones barely visible; pixels know what the eye actually got. Half each
keeps a full-bleed background from erasing an accent and keeps a declared accent
from claiming a presence it never had.
"""


def _color_labels() -> tuple[str, ...]:
    lightness = ("dark", "mid", "light")
    return tuple(
        f"color.h{hue}.{name}" for hue in range(HUE_BINS) for name in lightness
    )


def _scale_labels(prefix: str) -> tuple[str, ...]:
    steps = tuple(
        f"{prefix}.step{exponent:+d}"
        for exponent in range(SCALE_MIN_EXPONENT, SCALE_MAX_EXPONENT + 1)
    )
    return (*steps, f"{prefix}.off-scale-small", f"{prefix}.off-scale-large")


COLOR_DIMENSIONS: Final[tuple[str, ...]] = _color_labels()
SPACING_DIMENSIONS: Final[tuple[str, ...]] = _scale_labels("spacing")
RADIUS_DIMENSIONS: Final[tuple[str, ...]] = (
    "radius.sharp",
    "radius.hairline",
    "radius.small",
    "radius.default",
    "radius.large",
    "radius.pill",
)
TYPE_DIMENSIONS: Final[tuple[str, ...]] = _scale_labels("type")
CONTRAST_DIMENSIONS: Final[tuple[str, ...]] = (
    "contrast.below-3",
    "contrast.3-4.5",
    "contrast.4.5-7",
    "contrast.7-12",
    "contrast.above-12",
)

BLOCKS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("color", COLOR_DIMENSIONS),
    ("spacing", SPACING_DIMENSIONS),
    ("radius", RADIUS_DIMENSIONS),
    ("type", TYPE_DIMENSIONS),
    ("contrast", CONTRAST_DIMENSIONS),
)
"""The five faces of style, in vector order. Each is a distribution in its own right."""

DIMENSIONS: Final[tuple[str, ...]] = tuple(
    label for _, labels in BLOCKS for label in labels
)
BLOCK_NAMES: Final[tuple[str, ...]] = tuple(name for name, _ in BLOCKS)
BLOCK_WEIGHT: Final[float] = 1.0 / math.sqrt(len(BLOCKS))
"""Every block is L2-normalised and then scaled by this, so the whole vector is a
unit vector and cosine similarity between two vectors is the *plain mean* of the five
per-block similarities. No face of style can out-shout the others by having more bins.
"""

_ROUNDING: Final[int] = 10
"""Values are rounded before they are stored so that "the same sample" is the same
vector bit-for-bit, whatever order a caller assembled it in."""


class OklchColor(BaseModel):
    """One colour in OKLCH, as the adapter converted it.

    OKLCH rather than sRGB because the histogram is meant to be perceptual: two
    colours that land in the same bin should look related, which is false in RGB and
    roughly true here.
    """

    model_config = _MODEL_CONFIG

    l: float = Field(ge=0.0, le=1.0)  # noqa: E741 -- the CSS channel is named L
    c: float = Field(ge=0.0)
    h: float = Field(ge=0.0, lt=360.0)

    @property
    def hue_bin(self) -> int:
        return min(int(self.h * HUE_BINS / 360.0), HUE_BINS - 1)

    @property
    def lightness_bin(self) -> int:
        low, high = LIGHTNESS_EDGES
        if self.l < low:
            return 0
        return 1 if self.l < high else 2

    @property
    def color_bin(self) -> int:
        return self.hue_bin * LIGHTNESS_BINS + self.lightness_bin


class ElementStyle(BaseModel):
    """What one rendered element contributed to the look of the fragment."""

    model_config = _MODEL_CONFIG

    selector: Selector = ""
    area_px: float = Field(default=0.0, ge=0.0)
    color: OklchColor | None = None
    background: OklchColor | None = None
    font_size_px: float | None = Field(default=None, gt=0.0)
    border_radius_px: float | None = Field(default=None, ge=0.0)
    spacing_px: tuple[Annotated[float, Field(ge=0.0)], ...] = ()
    contrast_ratio: float | None = Field(default=None, gt=0.0)


class ScreenshotStats(BaseModel):
    """Pixel counts per colour bin, produced by the adapter that owns the image."""

    model_config = _MODEL_CONFIG

    oklch_bins: tuple[Annotated[float, Field(ge=0.0)], ...]

    @field_validator("oklch_bins")
    @classmethod
    def _right_shape(cls, bins: tuple[float, ...]) -> tuple[float, ...]:
        if len(bins) != COLOR_BINS:
            raise ValueError(
                f"screenshot histogram needs {COLOR_BINS} bins, got {len(bins)}"
            )
        return bins


class Viewport(BaseModel):
    model_config = _MODEL_CONFIG

    width: float = Field(gt=0.0)
    height: float = Field(gt=0.0)


class StyleSample(BaseModel):
    """One rendered fragment, reduced to the numbers the embedding needs.

    The shape is fixed here rather than in the adapter so that a second sensor
    family -- a different browser, a static renderer, a fixture file -- can feed the
    same vector without the domain learning anything new.
    """

    model_config = _MODEL_CONFIG

    span_id: str = ""
    viewport: Viewport | None = None
    elements: tuple[ElementStyle, ...] = ()
    screenshot: ScreenshotStats | None = None

    @classmethod
    def parse(cls, raw: StyleSampleMapping) -> Self:
        """Validate a ``page.evaluate`` payload (or a fixture) into a sample."""
        return cls.model_validate(dict(raw))


def _bucket(value: float, edges: Sequence[float]) -> int:
    for index, edge in enumerate(edges):
        if value <= edge:
            return index
    return len(edges)


def _scale_bin(px: float, scale: ModularScale) -> int:
    """Where *px* falls on *scale*: a step bucket, or one of the two miss buckets.

    Quantising against the contract's own base and ratio is what makes "off-scale" a
    measurement instead of a matter of taste. A page built on a 4px grid and a page
    built on this contract's 8x1.5 scale differ here even when they look similar in
    a screenshot, which is exactly the drift the fast loop is meant to notice.
    """
    if px <= 0.0:
        return SCALE_HIT_BINS
    exponent = round(math.log(px / scale.base_px) / math.log(scale.ratio))
    expected = scale.base_px * scale.ratio**exponent
    if abs(px - expected) <= SCALE_TOLERANCE * expected:
        clamped = min(max(exponent, SCALE_MIN_EXPONENT), SCALE_MAX_EXPONENT)
        return clamped - SCALE_MIN_EXPONENT
    return SCALE_HIT_BINS if px < scale.base_px else SCALE_HIT_BINS + 1


def _normalised(counts: Sequence[float]) -> tuple[float, ...]:
    """L2-normalise a block and weight it, treating "no data" as "no opinion".

    An empty block becomes uniform rather than zero. A zero block would read as
    maximally dissimilar to everything, so a fragment that simply has no rounded
    corners would score as distinctive on the strength of an absence.
    """
    total = math.fsum(counts)
    values = list(counts) if total > 0.0 else [1.0] * len(counts)
    norm = math.sqrt(math.fsum(value * value for value in values))
    return tuple(round(value / norm * BLOCK_WEIGHT, _ROUNDING) for value in values)


def _share(counts: Sequence[float]) -> tuple[float, ...]:
    total = math.fsum(counts)
    if total <= 0.0:
        return tuple(0.0 for _ in counts)
    return tuple(value / total for value in counts)


class StyleVector(BaseModel):
    """A fixed-length, unit-norm point in style space.

    Deterministic by construction: the same :class:`StyleSample` and the same
    contract always yield the same numbers, so "this candidate is the one we saw last
    turn" is an equality test rather than a re-render.
    """

    model_config = _MODEL_CONFIG

    values: tuple[float, ...]

    @field_validator("values")
    @classmethod
    def _right_shape(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if len(values) != len(DIMENSIONS):
            raise ValueError(
                f"a style vector has {len(DIMENSIONS)} dimensions, got {len(values)}"
            )
        return values

    @classmethod
    def from_sample(cls, sample: StyleSample, *, contract: DirectionContract) -> Self:
        """Embed one rendered fragment, quantised against *contract*'s own scales.

        Colour and type size are weighted by the area they occupy, because that is
        what a viewer's eye is weighted by. Spacing and corner radius are counted per
        occurrence, because rhythm and corner treatment are properties of the parts
        rather than of their size -- weighting them by area would let one full-bleed
        section decide the whole distribution.
        """
        color = [0.0] * COLOR_BINS
        spacing = [0.0] * SCALE_BINS
        radius = [0.0] * RADIUS_BINS
        type_size = [0.0] * SCALE_BINS
        contrast = [0.0] * CONTRAST_BINS

        for element in sample.elements:
            weight = element.area_px
            for swatch in (element.background, element.color):
                if swatch is not None and weight > 0.0:
                    color[swatch.color_bin] += weight
            if element.font_size_px is not None and weight > 0.0:
                type_size[_scale_bin(element.font_size_px, contract.type_scale)] += weight
            if element.border_radius_px is not None:
                radius[_bucket(element.border_radius_px, RADIUS_EDGES)] += 1.0
            for value in element.spacing_px:
                if value > 0.0:
                    spacing[_scale_bin(value, contract.spacing)] += 1.0
            if element.contrast_ratio is not None and weight > 0.0:
                contrast[_bucket(element.contrast_ratio, CONTRAST_EDGES)] += weight

        merged = _merge_color(color, sample.screenshot)
        blocks = (merged, spacing, radius, type_size, contrast)
        return cls(
            values=tuple(
                value for block in blocks for value in _normalised(block)
            )
        )

    def block(self, name: str) -> tuple[float, ...]:
        """One face of style on its own, still weighted as it sits in the vector."""
        start = 0
        for block_name, labels in BLOCKS:
            if block_name == name:
                return self.values[start : start + len(labels)]
            start += len(labels)
        raise KeyError(f"unknown block {name!r}; known: {list(BLOCK_NAMES)}")

    def labelled(self) -> dict[str, float]:
        """Named dimensions, for a report a human is going to read."""
        return dict(zip(DIMENSIONS, self.values, strict=True))

    def dominant(self, count: int = 5) -> tuple[tuple[str, float], ...]:
        ranked = sorted(
            self.labelled().items(), key=lambda item: (-item[1], item[0])
        )
        return tuple(ranked[:count])


def _merge_color(
    computed: Sequence[float], screenshot: ScreenshotStats | None
) -> tuple[float, ...]:
    from_styles = _share(computed)
    if screenshot is None:
        return tuple(from_styles)
    from_pixels = _share(screenshot.oklch_bins)
    if not any(from_pixels):
        return tuple(from_styles)
    if not any(from_styles):
        return from_pixels
    pixel_share = SCREENSHOT_COLOR_SHARE
    return tuple(
        (1.0 - pixel_share) * style + pixel_share * pixel
        for style, pixel in zip(from_styles, from_pixels, strict=True)
    )


def distance(a: StyleVector, b: StyleVector) -> float:
    """Cosine distance in [0, 1]: 0 is indistinguishable, 1 shares nothing.

    Cosine rather than Euclidean because the vector is a bundle of *distributions*
    and what matters is their shape, not how many elements happened to be sampled.
    Both operands are unit vectors and non-negative, so the dot product is already
    the cosine and the result cannot leave [0, 1] except by float noise, which is
    clamped away.
    """
    similarity = math.fsum(x * y for x, y in zip(a.values, b.values, strict=True))
    return min(max(1.0 - similarity, 0.0), 1.0)


def blandness(vector: StyleVector, anti_corpus: Sequence[StyleVector]) -> float:
    """Distance to the nearest stock template. **LOW means bland.**

    The direction contract names its anti-references -- default shadcn, Bootstrap,
    the stock SaaS landing -- because the failure mode of a capable model is not
    ugliness, it is the corpus median. Measuring distance to the *nearest* one rather
    than to their centroid is the point: landing exactly on Bootstrap is bland even
    if the average template is far away, and the centroid of three templates is a
    place no real page occupies.

    Raises on an empty corpus rather than returning a sentinel: "we have no bar" and
    "this is as far from the bar as possible" must never be the same number.
    """
    if not anti_corpus:
        raise ValueError("blandness needs a non-empty anti-corpus to measure against")
    return min(distance(vector, member) for member in anti_corpus)


def nearest_template(
    vector: StyleVector, anti_corpus: Mapping[str, StyleVector]
) -> tuple[str, float]:
    """Which template a candidate collapsed onto, and how close it got.

    The name is what a builder can act on; a bare float tells it nothing it can fix.
    """
    if not anti_corpus:
        raise ValueError("nearest_template needs a non-empty anti-corpus")
    scored = sorted(
        ((name, distance(vector, member)) for name, member in anti_corpus.items()),
        key=lambda item: (item[1], item[0]),
    )
    return scored[0]


def build_anti_corpus(
    samples: Iterable[tuple[str, StyleSample]], *, contract: DirectionContract
) -> dict[str, StyleVector]:
    """Embed the named anti-references once, so every candidate is measured the same."""
    return {
        name: StyleVector.from_sample(sample, contract=contract)
        for name, sample in samples
    }


class Variant(BaseModel):
    """One candidate fragment: an actuator output, before anything has judged it.

    ``builder_family`` is provenance and nothing else. It is recorded because the
    loop wants to know which family produced which kind of failure over time, and it
    is kept strictly out of anything a critic sees -- a blind panel that could infer
    the author is no longer blind.
    """

    model_config = _MODEL_CONFIG

    variant_id: VariantId = Field(min_length=1)
    part: PartName = Field(min_length=1)
    html: FragmentHtml
    builder_family: BuilderFamily = Field(min_length=1)
    cell_hint: CellCoord | None = None
    """The behaviour-space cell this variant was *asked* for.

    A hint, never a fact: where a variant actually lands is measured from what it
    rendered, not from what the brief hoped for. Recording the ask separately is what
    lets the explorer notice that a whole region of the space is unreachable by
    prompting.
    """

    @property
    def span_id(self) -> str:
        """Default attribution key. The fragment's own ``data-span-id`` wins if it
        differs; this is the fallback the regulator uses when no caller says otherwise."""
        return self.variant_id


# --------------------------------------------------------------------------- #
# Seam: the MAP-Elites archive (U11) lands below this line                     #
# --------------------------------------------------------------------------- #
#
# U11 adds `StyleAxes` (density, expressiveness, motion_intensity), `Elite` and
# `EliteArchive` to THIS module, appended here, without touching anything above.
# The contract between the halves is narrow and already fixed:
#
#   * `CellCoord` is the grid coordinate type; `Variant.cell_hint` already carries
#     one, so the archive does not need to widen `Variant`.
#   * `StyleAxes` derives from `StyleVector` (plus motion evidence) via the public
#     surface only -- `block()`, `labelled()`, `values` -- so the embedding can be
#     retuned without the archive noticing. Do not reach into the bin layout.
#   * `distance()` is the distinctiveness primitive the fitness bonus should use;
#     `blandness()` is its anti-corpus specialisation. Neither belongs in fitness
#     twice.
#   * The archive REFUSES any variant whose `RegulatorReport.passed` is False. Gates
#     are a precondition of entry, never a term in fitness -- the moment a gate can
#     be traded against distinctiveness, the taste separation this module and the
#     regulator both maintain is gone.
#
# Nothing above this line may import from `ui_servo.control`; the archive is domain
# too, and takes the regulator's verdict as a plain boolean argument.


# --------------------------------------------------------------------------- #
# The behaviour space                                                          #
# --------------------------------------------------------------------------- #
#
# MAP-Elites needs two different things from a candidate and must never confuse
# them. *Behaviour* says where a candidate belongs -- which cell of the grid it
# occupies -- and is deliberately low-dimensional, because a human has to be able
# to look at the grid and say "we never tried a dense, quiet, restrained hero".
# *Fitness* says how good it is once it is there, and only ever competes within a
# cell. Keeping them apart is what makes the archive an instrument of variety
# rather than a leaderboard with extra steps: a weird cell's best entry survives
# forever even though a mainstream cell's entry beats it on every score.
#
# Three axes, no more, because 3x3x3 is 27 briefs and that is already more than a
# human will look at in one sitting.

AXIS_NAMES: Final[tuple[str, str, str]] = ("density", "expressiveness", "motion_intensity")
"""The behaviour space, in :data:`CellCoord` order."""

DEFAULT_RESOLUTION: Final[int] = 3
"""Bins per axis. Three -- low / middle / high -- because the axes are derived
measurements with real noise in them, and a finer grid would spend briefs
distinguishing cells the derivation cannot actually tell apart."""

MOTION_CEILING_MS: Final[float] = 400.0
"""Fallback normaliser for :attr:`StyleAxes.motion_intensity`.

Used only when no contract is supplied. Callers that have one should pass it:
measuring pace against ``contract.motion_table().max_duration_ms`` makes the axis
mean "how far this pushes the envelope *the bar allows*" rather than "how far this
pushes some number the domain made up".
"""

_MAX_SPREAD: Final[float] = 0.5
"""Largest standard deviation a distribution over positions in [0, 1] can have --
half the mass at each end. The normaliser for :func:`_spread`."""

_OFF_SCALE_SMALL: Final[str] = "off-scale-small"
_OFF_SCALE_LARGE: Final[str] = "off-scale-large"
_OFF_SCALE: Final[tuple[str, str]] = (_OFF_SCALE_SMALL, _OFF_SCALE_LARGE)

DEFAULT_RADIUS_LABEL: Final[str] = "radius.default"
"""The corner bucket :data:`RADIUS_EDGES` documents as "where every default
component library lives". :attr:`StyleAxes.expressiveness` measures distance from it."""


def _block_labels(block_name: str) -> tuple[str, ...]:
    """The labels of one block, in block order.

    Read off :data:`BLOCKS` rather than sliced out of :data:`DIMENSIONS` by index:
    the seam's rule is that the archive knows names, never offsets, so retuning the
    embedding (more hue bins, another radius bucket) moves these derivations with
    it instead of silently mis-slicing.
    """
    for name, labels in BLOCKS:
        if name == block_name:
            return labels
    raise KeyError(f"unknown block {block_name!r}; known: {list(BLOCK_NAMES)}")


def _positions(block_name: str) -> dict[str, float]:
    """Label -> position in [0, 1] along an *ordered* block, smallest first.

    Only meaningful for blocks whose labels are a scale: spacing, type, radius and
    contrast are ordered small-to-large by construction, colour is not (its bins are
    hue-major, and a centroid over hues would measure nothing).

    The two off-scale labels of a modular-scale block are pinned to the ends they
    mean -- ``off-scale-small`` is below the base step, ``off-scale-large`` is at or
    above it -- rather than left where they sit in the tuple, which is at the far
    end for both. Left alone they would make a fragment with sub-base spacing read
    as the airiest thing in the corpus.
    """
    labels = _block_labels(block_name)
    ordered = [label for label in labels if not label.endswith(_OFF_SCALE)]
    span = max(len(ordered) - 1, 1)
    position = {label: index / span for index, label in enumerate(ordered)}
    for label in labels:
        if label.endswith(_OFF_SCALE_SMALL):
            position[label] = 0.0
        elif label.endswith(_OFF_SCALE_LARGE):
            position[label] = 1.0
    return position


def _shares(vector: StyleVector, block_name: str) -> dict[str, float]:
    """One block as a probability distribution over its own labels.

    Renormalised per block so the axes do not inherit :data:`BLOCK_WEIGHT` or the
    L2 norm: a behaviour coordinate is a shape, and the shape must not move when
    the embedding's weighting does.
    """
    labels = _block_labels(block_name)
    weights = vector.block(block_name)
    total = math.fsum(weights)
    if total <= 0.0:
        return {label: 1.0 / len(labels) for label in labels}
    return {
        label: weight / total for label, weight in zip(labels, weights, strict=True)
    }


def _clamped(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _mean_position(vector: StyleVector, block_name: str) -> float:
    """Where a block's mass sits on its own scale, in [0, 1]."""
    position = _positions(block_name)
    shares = _shares(vector, block_name)
    return _clamped(math.fsum(share * position[label] for label, share in shares.items()))


def _spread(vector: StyleVector, block_name: str) -> float:
    """How far apart a block's extremes are, normalised to [0, 1].

    The dynamic range of one face of style: a page where every heading is 17px has
    a spread near zero, one that sets a 68px display against 15px captions has a
    large one. Standard deviation rather than max-minus-min because one stray
    element should not define the range.
    """
    position = _positions(block_name)
    shares = _shares(vector, block_name)
    mean = math.fsum(share * position[label] for label, share in shares.items())
    variance = math.fsum(
        share * (position[label] - mean) ** 2 for label, share in shares.items()
    )
    return _clamped(math.sqrt(max(variance, 0.0)) / _MAX_SPREAD)


def _departure(vector: StyleVector, block_name: str, anchor: str) -> float:
    """Mean distance of a block's mass from one named label, normalised to [0, 1].

    "How far from the default" as arithmetic. Distance in both directions counts:
    sharp corners and pill corners are both decisions, and only the 4-8px band in
    the middle is the absence of one.
    """
    position = _positions(block_name)
    shares = _shares(vector, block_name)
    if anchor not in position:
        raise KeyError(f"{anchor!r} is not a label of block {block_name!r}")
    origin = position[anchor]
    reach = max(origin, 1.0 - origin) or 1.0
    return _clamped(
        math.fsum(share * abs(position[label] - origin) for label, share in shares.items())
        / reach
    )


class MotionEvidence(BaseModel):
    """What the fragment was observed to *do*, as opposed to how it looks.

    The third behaviour axis cannot come from :class:`StyleVector`: the vector is a
    still frame, and a page that slides its whole hero in over 300ms embeds
    identically to one that does not move at all. So motion is fed from the sensors
    instead, as two counts and a duration, and the domain does the arithmetic.

    Deliberately tiny. It is not a record of every animation -- that lives in the
    evidence stock as ``animation`` signals -- it is the summary a *coordinate*
    needs, and widening it would tempt the archive into scoring motion rather than
    locating it.

    An empty :class:`MotionEvidence` means "nothing moved". A caller that did not
    look at motion at all is claiming stillness, which is the safe direction: it
    files the variant in a still cell where a later, better-measured sibling can
    displace it, rather than scattering unmeasured candidates across the axis.
    """

    model_config = _MODEL_CONFIG

    animated_elements: int = Field(default=0, ge=0)
    """How many rendered elements were seen to animate."""

    total_elements: int = Field(default=0, ge=0)
    """How many were looked at. Zero means the fragment had nothing to move."""

    longest_duration_ms: float = Field(default=0.0, ge=0.0)
    """The longest single animation observed, in milliseconds."""

    @property
    def reach(self) -> float:
        """Share of the fragment that moves, in [0, 1].

        Clamped rather than validated across fields: a sensor that counts more
        animated elements than elements is telling us its own bookkeeping slipped,
        and saturating an advisory coordinate is a better answer than refusing to
        place an otherwise-good variant.
        """
        if self.total_elements <= 0:
            return 0.0
        return _clamped(self.animated_elements / self.total_elements)

    def pace(self, ceiling_ms: float = MOTION_CEILING_MS) -> float:
        """How far the longest animation reaches toward *ceiling_ms*, in [0, 1]."""
        if ceiling_ms <= 0.0:
            raise ValueError(f"a motion ceiling must be positive, got {ceiling_ms!r}")
        return _clamped(self.longest_duration_ms / ceiling_ms)

    def intensity(self, ceiling_ms: float = MOTION_CEILING_MS) -> float:
        """The motion axis: the mean of reach and pace.

        Both halves are needed and neither is sufficient. One element easing for
        320ms is a deliberate accent; every element easing for 90ms is a page that
        shimmers. The mean puts the first low-middle and the second high, which is
        the distinction a human scanning the grid actually wants.
        """
        return _clamped((self.reach + self.pace(ceiling_ms)) / 2.0)


STILL: Final[MotionEvidence] = MotionEvidence()
"""The default motion evidence: a fragment that was seen not to move."""


class StyleAxes(BaseModel):
    """Where one rendered fragment sits in the behaviour space.

    Three deterministic derivations from the *public* surface of
    :class:`StyleVector` (:meth:`~StyleVector.block`, and the label tuples in
    :data:`BLOCKS`) plus :class:`MotionEvidence`. Nothing here indexes into the
    vector: the archive knows the names of the blocks and of their labels and
    nothing about how many bins each has, so the embedding can be retuned without
    silently relabelling the whole grid.

    **The derivations, exactly.**

    ``density`` -- ``1 - mean position of the spacing block``. Spacing is quantised
    against the contract's own modular scale, so "where the mass sits" is a
    statement about the rhythm the builder chose relative to the bar, not about
    absolute pixels. Mass on the small steps is a tight page (density near 1); mass
    on the large steps is an airy one (near 0). Spacing is counted per occurrence
    rather than by area upstream, which is what makes this measure the page's
    rhythm instead of its biggest section.

    ``expressiveness`` -- the mean of two things a default-looking fragment gives
    away: the *spread* of the type block (typographic dynamic range: how far apart
    display and body setting are on the contract's scale) and the *departure* of the
    radius block from :data:`DEFAULT_RADIUS_LABEL` (corner treatment: the 4-8px band
    is what you get when nobody decided, and both sharp and pill are decisions).
    Colour is deliberately excluded: its bins are hue-major and unordered, so any
    centroid or spread over them would be an artefact of bin order rather than a
    property of the design. Contrast is excluded too, because it is what the
    accessibility gate is about, and a behaviour axis that moved with a gate would
    put the two back on one wire.

    ``motion_intensity`` -- :meth:`MotionEvidence.intensity`, normalised against the
    contract's longest permitted duration when a contract is supplied.

    All three land in [0, 1]. A vector with no data in a block reads as uniform
    (see :func:`_normalised`), which puts these derivations near the middle rather
    than at an extreme -- "we could not tell" belongs in a middle cell, not in the
    most distinctive one.
    """

    model_config = _MODEL_CONFIG

    density: float = Field(ge=0.0, le=1.0)
    expressiveness: float = Field(ge=0.0, le=1.0)
    motion_intensity: float = Field(ge=0.0, le=1.0)

    @classmethod
    def of(
        cls,
        vector: StyleVector,
        *,
        motion: MotionEvidence = STILL,
        contract: DirectionContract | None = None,
    ) -> Self:
        """Locate *vector* in the behaviour space.

        ``contract`` is used for one thing only -- the motion ceiling -- and is
        optional because a caller comparing two candidates to each other does not
        need the bar to do it. The style half needs no contract here: the vector was
        already quantised against one when it was embedded.
        """
        ceiling = (
            MOTION_CEILING_MS
            if contract is None
            else float(contract.motion_table().max_duration_ms)
        )
        return cls(
            density=_clamped(1.0 - _mean_position(vector, "spacing")),
            expressiveness=_clamped(
                (
                    _spread(vector, "type")
                    + _departure(vector, "radius", DEFAULT_RADIUS_LABEL)
                )
                / 2.0
            ),
            motion_intensity=motion.intensity(ceiling),
        )

    @property
    def values(self) -> tuple[float, float, float]:
        """The three coordinates in :data:`AXIS_NAMES` order."""
        return (self.density, self.expressiveness, self.motion_intensity)

    def cell(self, resolution: int = DEFAULT_RESOLUTION) -> CellCoord:
        """Which grid cell these axes fall in, at *resolution* bins per axis.

        Equal-width bins with the top edge closed, so 1.0 lands in the last bin
        rather than in a phantom one past the end.
        """
        if resolution < 1:
            raise ValueError(f"a grid needs at least one bin per axis, got {resolution}")
        binned = tuple(
            min(int(value * resolution), resolution - 1) for value in self.values
        )
        return (binned[0], binned[1], binned[2])

    def as_mapping(self) -> dict[str, float]:
        """Named coordinates, for a report a human is going to read."""
        return dict(zip(AXIS_NAMES, self.values, strict=True))

    def summary(self) -> str:
        return ", ".join(f"{name} {value:.2f}" for name, value in self.as_mapping().items())


# --------------------------------------------------------------------------- #
# Fitness                                                                      #
# --------------------------------------------------------------------------- #

DISTINCTIVENESS_WEIGHT: Final[float] = 0.25
"""Ceiling on the distinctiveness bonus, as a share of one rank step.

Capped at one step (see :func:`fitness_of`) so the bonus can lift a candidate at
most into a *tie* with the one immediately above it -- and a tie is resolved in
the incumbent's favour. Distinctiveness therefore breaks ties and orders equals; it
can never overturn what the panel decided. That is the whole reason it is bounded:
an unbounded novelty term is precisely how a quality-diversity loop Goodharts into
weird-but-worse.
"""


def rank_score(panel_rank: int, panel_size: int) -> float:
    """A 1-based panel rank as a score in [0, 1]; rank 1 scores 1.0.

    Ranks come out of the slow loop as an ordering, and an ordering cannot be
    compared across cells -- rank 2 of 6 is a better artefact than rank 1 of 2.
    Normalising by the size of the field it won is what makes fitness mean the same
    thing in every cell of the archive.
    """
    if panel_size < 1:
        raise ValueError(f"a panel has at least one candidate, got {panel_size}")
    if not 1 <= panel_rank <= panel_size:
        raise ValueError(f"rank {panel_rank} is outside a panel of {panel_size}")
    if panel_size == 1:
        return 1.0
    return (panel_size - panel_rank) / (panel_size - 1)


def fitness_of(
    *, panel_rank: int, panel_size: int, distinctiveness: float = 0.0
) -> float:
    """Panel rank plus a bounded distinctiveness bonus.

    ``distinctiveness`` is a cosine distance in [0, 1] from :func:`distance` --
    normally to the nearest artefact already in the archive
    (:meth:`EliteArchive.distinctiveness`). It is measured *once*: blandness
    (distance to the anti-corpus) rides on the report as taste for a human to read
    and is deliberately not added here, because the same idea entering fitness twice
    would weight it twice for reasons nobody chose.

    Gates are absent by construction. They are a precondition of entry, and there is
    no argument here that could carry them.
    """
    if not 0.0 <= distinctiveness <= 1.0:
        raise ValueError(
            f"distinctiveness {distinctiveness!r} is not a cosine distance in [0, 1]"
        )
    base = rank_score(panel_rank, panel_size)
    step = 1.0 if panel_size == 1 else 1.0 / (panel_size - 1)
    return base + min(DISTINCTIVENESS_WEIGHT, step) * distinctiveness


# --------------------------------------------------------------------------- #
# The archive                                                                  #
# --------------------------------------------------------------------------- #


class GateFailure(ValueError):
    """A variant whose gates did not pass was offered to the archive.

    Raised rather than returned, and raised at the boundary rather than logged,
    because "this never entered the taste loop" has to be impossible to miss. A
    quiet ``None`` would make a gate failure indistinguishable from a cell whose
    incumbent simply held, and the archive would fill with broken UI that nobody
    could see arriving.
    """


class Elite(BaseModel):
    """The current occupant of one cell: a variant, where it sits, and its score.

    Carries the evidence a human needs to make the pick -- what it looked like, what
    the gates said, what the panel found, how bland it was -- as plain values, so the
    frontier report is a rendering of the archive rather than a second gathering
    pass over stores the domain must not know about.

    ``screenshot`` is a locator as text, never an open file. This module has no
    filesystem and no image library by design; whoever wrote the image is the one
    who can read it back.
    """

    model_config = _MODEL_CONFIG

    variant: Variant
    axes: StyleAxes
    cell: CellCoord
    fitness: float
    style_vector: StyleVector | None = None
    blandness_score: float | None = None
    panel_rank: int | None = None
    gate_summary: str = ""
    findings: tuple[str, ...] = ()
    screenshot: str = ""

    @field_validator("fitness")
    @classmethod
    def _finite(cls, fitness: float) -> float:
        if not math.isfinite(fitness):
            raise ValueError(f"fitness must be a finite number, got {fitness!r}")
        return fitness

    @property
    def variant_id(self) -> VariantId:
        return self.variant.variant_id

    @property
    def aimed_at(self) -> CellCoord | None:
        """The cell the brief asked for, when it asked for one.

        Kept next to :attr:`cell` because the interesting reading is the
        *disagreement*: a whole region that briefs keep aiming at and variants keep
        missing is a region prompting cannot reach, which is a fact about the
        actuator, not about the archive.
        """
        return self.variant.cell_hint

    @property
    def on_target(self) -> bool | None:
        hint = self.aimed_at
        return None if hint is None else hint == self.cell

    def summary(self) -> str:
        rank = "unranked" if self.panel_rank is None else f"rank {self.panel_rank}"
        bland = (
            "blandness n/a"
            if self.blandness_score is None
            else f"blandness {self.blandness_score:.3f}"
        )
        return (
            f"{self.cell} {self.variant_id}: fitness {self.fitness:.3f} "
            f"({rank}, {bland}) [{self.axes.summary()}]"
        )


class Coverage(BaseModel):
    """How much of the behaviour space has been reached, and how well.

    ``occupied`` is the quality-diversity headline number: an archive that fills two
    cells with brilliant work has explored less than one that fills nine with good
    work, and no mean fitness will tell you that. ``empty_cells`` is the actionable
    half -- it is literally the list of briefs still worth writing.
    """

    model_config = _MODEL_CONFIG

    resolution: int
    occupied: int
    total: int
    qd_score: float
    best_fitness: float | None = None
    mean_fitness: float | None = None
    empty_cells: tuple[CellCoord, ...] = ()

    @property
    def ratio(self) -> float:
        return self.occupied / self.total if self.total else 0.0

    def summary(self) -> str:
        best = "n/a" if self.best_fitness is None else f"{self.best_fitness:.3f}"
        return (
            f"{self.occupied}/{self.total} cells occupied ({self.ratio:.0%}) at "
            f"resolution {self.resolution}; QD score {self.qd_score:.3f}, best {best}"
        )


class EliteArchive:
    """A MAP-Elites grid over :class:`StyleAxes`, keeping the fittest per cell.

    A stock, and the only mutable thing in this module. Everything else here is a
    value; the archive accumulates across rounds, which is exactly what makes it
    able to say "nothing has ever landed in the dense-quiet-restrained corner".

    Two rules, and they are the whole design:

    * **Gates are a precondition, never a term.** :meth:`place` takes the
      regulator's verdict as a plain boolean and raises :class:`GateFailure` when it
      is False. There is no weighting under which a distinctive fragment with a
      contrast violation gets in.
    * **Competition is local.** A candidate only ever competes with the current
      occupant of *its own* cell. A globally mediocre variant that is the only thing
      ever to reach its corner of the space keeps that corner, which is the entire
      point of quality-diversity over a leaderboard.

    Takes a boolean rather than a ``RegulatorReport`` because the report lives in
    ``ui_servo.control`` and the dependency rule runs the other way. The narrowness
    is a feature: the archive cannot accidentally start reading a gate detail.
    """

    __slots__ = ("_cells", "_offers", "_placements", "_resolution")

    def __init__(self, *, resolution: int = DEFAULT_RESOLUTION) -> None:
        if resolution < 1:
            raise ValueError(f"a grid needs at least one bin per axis, got {resolution}")
        self._resolution = resolution
        self._cells: dict[CellCoord, Elite] = {}
        self._offers = 0
        self._placements = 0

    # -- shape ------------------------------------------------------------- #

    @property
    def resolution(self) -> int:
        return self._resolution

    @property
    def total_cells(self) -> int:
        return self._resolution ** len(AXIS_NAMES)

    @property
    def offers(self) -> int:
        """How many variants were offered. With :attr:`placements`, the hit rate."""
        return self._offers

    @property
    def placements(self) -> int:
        """How many offers actually took a cell."""
        return self._placements

    def __len__(self) -> int:
        return len(self._cells)

    def __contains__(self, cell: object) -> bool:
        return cell in self._cells

    def __iter__(self) -> Iterator[Elite]:
        return iter(self.frontier())

    def all_cells(self) -> tuple[CellCoord, ...]:
        """Every coordinate in the grid, in odometer order."""
        size = self._resolution
        return tuple(
            (first, second, third)
            for first in range(size)
            for second in range(size)
            for third in range(size)
        )

    def cell_for(self, axes: StyleAxes) -> CellCoord:
        return axes.cell(self._resolution)

    def get(self, cell: CellCoord) -> Elite | None:
        return self._cells.get(cell)

    def occupied_cells(self) -> tuple[CellCoord, ...]:
        return tuple(sorted(self._cells))

    def empty_cells(self) -> tuple[CellCoord, ...]:
        return tuple(cell for cell in self.all_cells() if cell not in self._cells)

    # -- distinctiveness --------------------------------------------------- #

    def vectors(self) -> tuple[StyleVector, ...]:
        return tuple(
            elite.style_vector
            for elite in self._cells.values()
            if elite.style_vector is not None
        )

    def distinctiveness(self, vector: StyleVector) -> float:
        """Cosine distance from *vector* to the nearest artefact already held.

        Novelty measured against what the archive has actually seen, which is the
        question the exploration loop is asking -- "is this a new kind of thing for
        us?" -- and a different question from :func:`blandness`, which asks whether
        it is a new kind of thing at all. Both are :func:`distance`; only this one
        enters fitness.

        An empty archive returns 1.0: the first artefact is maximally novel by
        definition, and there is nothing to be near.
        """
        held = self.vectors()
        if not held:
            return 1.0
        return min(distance(vector, member) for member in held)

    # -- placement --------------------------------------------------------- #

    def place(
        self,
        variant: Variant,
        axes: StyleAxes,
        fitness: float,
        *,
        passed: bool,
        style_vector: StyleVector | None = None,
        blandness_score: float | None = None,
        panel_rank: int | None = None,
        gate_summary: str = "",
        findings: Sequence[str] = (),
        screenshot: str = "",
    ) -> Elite | None:
        """Offer *variant* to its cell. Returns the new elite, or ``None`` if it lost.

        ``passed`` is the regulator's verdict and is keyword-only and required, so
        no caller can place a variant without having stated it. False raises
        :class:`GateFailure`.

        Ties go to the incumbent. Two consequences, both wanted: the archive does not
        churn when a rerun reproduces the same score, and the capped distinctiveness
        bonus (:func:`fitness_of`) can pull a candidate level with the rank above it
        but never past it.
        """
        if not passed:
            raise GateFailure(
                f"variant {variant.variant_id!r} did not pass its gates; the archive "
                "takes gates as a precondition of entry, never as a term in fitness"
            )
        candidate = Elite(
            variant=variant,
            axes=axes,
            cell=self.cell_for(axes),
            fitness=fitness,
            style_vector=style_vector,
            blandness_score=blandness_score,
            panel_rank=panel_rank,
            gate_summary=gate_summary,
            findings=tuple(findings),
            screenshot=screenshot,
        )
        self._offers += 1
        incumbent = self._cells.get(candidate.cell)
        if incumbent is not None and incumbent.fitness >= candidate.fitness:
            return None
        self._cells[candidate.cell] = candidate
        self._placements += 1
        return candidate

    # -- reading ----------------------------------------------------------- #

    def frontier(self) -> list[Elite]:
        """Every cell's current occupant, fittest first, cell order breaking ties.

        The whole archive, not a Pareto front: in MAP-Elites the elites *are* the
        frontier, because each one already won the only competition it was in.
        """
        return sorted(
            self._cells.values(), key=lambda elite: (-elite.fitness, elite.cell)
        )

    def coverage(self) -> Coverage:
        scores = [elite.fitness for elite in self._cells.values()]
        return Coverage(
            resolution=self._resolution,
            occupied=len(self._cells),
            total=self.total_cells,
            qd_score=math.fsum(scores),
            best_fitness=max(scores) if scores else None,
            mean_fitness=math.fsum(scores) / len(scores) if scores else None,
            empty_cells=self.empty_cells(),
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(resolution={self._resolution}, "
            f"occupied={len(self._cells)}/{self.total_cells})"
        )
