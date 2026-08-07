"""The contract is the setpoint; these tests are the calibration check on it."""

import copy
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ui_servo.domain.contract import (
    ANIMATABLE_PROPERTIES,
    AxisBounds,
    DirectionContract,
    FontFamily,
    LchColor,
    MotionTable,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "direction" / "direction.toml"


@pytest.fixture(scope="session")
def contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def raw(contract_text: str) -> dict[str, Any]:
    return tomllib.loads(contract_text)


@pytest.fixture(scope="session")
def contract(contract_text: str) -> DirectionContract:
    return DirectionContract.from_toml(contract_text)


@pytest.fixture
def mutable_raw(raw: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(raw)


def test_shipped_contract_is_v1(contract: DirectionContract) -> None:
    assert contract.meta.version == 1
    assert contract.palette.space == "oklch"
    assert contract.references and contract.anti_references


def test_round_trip_through_mapping(contract: DirectionContract) -> None:
    assert DirectionContract.from_mapping(contract.to_mapping()) == contract


def test_round_trip_is_not_vacuous(contract: DirectionContract) -> None:
    mapping = contract.to_mapping()
    assert mapping["meta"]["name"] == contract.meta.name
    assert mapping["palette"]["accent-2"]["l"] == contract.palette.accent_2.lightness
    assert "type" in mapping and "type_scale" not in mapping


def test_contract_is_frozen(contract: DirectionContract) -> None:
    with pytest.raises(ValidationError):
        contract.meta = contract.meta  # type: ignore[misc]


class TestDeepImmutability:
    """A setpoint that can be edited after validation is not a setpoint."""

    @pytest.mark.parametrize(
        ("accessor", "key", "value"),
        [
            (lambda c: c.motion.durations_ms, "base", 5000),
            (lambda c: c.motion.easings, "standard", "cubic-bezier(9, 9, 9, 9)"),
            (lambda c: c.type_scale.steps, "base", 99),
            (lambda c: c.spacing.steps, "base", 99),
            (lambda c: c.density, "measure", None),
        ],
    )
    def test_nested_mappings_reject_writes(
        self, contract: DirectionContract, accessor: Any, key: str, value: Any
    ) -> None:
        mapping = accessor(contract)
        with pytest.raises(TypeError):
            mapping[key] = value
        with pytest.raises(TypeError):
            del mapping[key]

    def test_validator_cannot_be_bypassed_by_mutation(self, contract: DirectionContract) -> None:
        with pytest.raises(TypeError):
            contract.motion.durations_ms["base"] = 5000
        assert contract.motion_table().max_duration_ms <= 600
        assert "--motion-duration-base: 5000ms;" not in contract.to_css_custom_properties()

    def test_construction_copies_the_source_mapping(self, mutable_raw: dict[str, Any]) -> None:
        built = DirectionContract.from_mapping(mutable_raw)
        mutable_raw["motion"]["durations_ms"]["base"] = 5000
        assert built.motion.durations_ms["base"] != 5000

    def test_dump_is_a_detached_copy(self, contract: DirectionContract) -> None:
        dumped = contract.to_mapping()
        dumped["motion"]["durations_ms"]["base"] = 5000
        dumped["density"]["measure"]["max"] = 9999.0
        assert contract.motion.durations_ms["base"] != 5000
        assert contract.density["measure"].max != 9999.0

    def test_mappings_still_behave_as_mappings(self, contract: DirectionContract) -> None:
        assert dict(contract.motion.durations_ms) == {
            name: value for name, value in contract.motion.durations_ms.items()
        }
        assert "base" in contract.type_scale.steps
        assert len(contract.density) >= 1


class TestPalette:
    def test_oklch_rendering(self) -> None:
        color = LchColor.model_validate({"l": 0.8, "c": 0.185, "h": 78.0})
        assert color.to_css() == "oklch(0.8 0.185 78)"

    def test_alpha_rendering(self) -> None:
        color = LchColor.model_validate({"l": 0.5, "c": 0.1, "h": 10.0, "alpha": 0.25})
        assert color.to_css() == "oklch(0.5 0.1 10 / 0.25)"

    def test_out_of_gamut_lightness_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LchColor.model_validate({"l": 1.4, "c": 0.1, "h": 10.0})

    def test_hue_wraparound_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LchColor.model_validate({"l": 0.5, "c": 0.1, "h": 360.0})

    def test_illegible_text_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["palette"]["text"] = dict(mutable_raw["palette"]["background"])
        mutable_raw["palette"]["text"]["l"] = 0.30
        with pytest.raises(ValidationError, match="legibility is correctness"):
            DirectionContract.from_mapping(mutable_raw)

    def test_dark_scheme_derived_from_background(self, contract: DirectionContract) -> None:
        assert contract.palette.color_scheme() == "dark"


class TestScales:
    def test_geometric_sizes(self, contract: DirectionContract) -> None:
        scale = contract.type_scale
        assert scale.size_px("base") == pytest.approx(scale.base_px)
        assert scale.size_px("md") == pytest.approx(scale.base_px * scale.ratio)
        assert scale.size_px("xs") == pytest.approx(scale.base_px / scale.ratio**2)

    def test_unknown_step_names_itself(self, contract: DirectionContract) -> None:
        with pytest.raises(KeyError, match="unknown step"):
            contract.spacing.size_px("nope")

    def test_scale_must_be_anchored(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["spacing"]["steps"]["base"] = 1
        with pytest.raises(ValidationError, match="exponent 0"):
            DirectionContract.from_mapping(mutable_raw)

    def test_duplicate_exponents_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["spacing"]["steps"]["md"] = mutable_raw["spacing"]["steps"]["lg"]
        with pytest.raises(ValidationError, match="distinct exponents"):
            DirectionContract.from_mapping(mutable_raw)

    def test_step_names_must_be_css_safe(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["type"]["steps"]["Big Step; }"] = 9
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_font_stack_quotes_only_what_needs_it(self) -> None:
        family = FontFamily.model_validate(
            {"name": "Instrument Serif", "fallbacks": ["ui-serif", "-apple-system", "Segoe UI"]}
        )
        assert family.to_css_stack() == '"Instrument Serif", ui-serif, -apple-system, "Segoe UI"'


class TestMotionTable:
    def test_matches_the_toml(self, contract: DirectionContract, raw: dict[str, Any]) -> None:
        table = contract.motion_table()
        assert table.durations_ms == frozenset(raw["motion"]["durations_ms"].values())
        assert table.easings == frozenset(raw["motion"]["easings"].values())
        assert table.animatable_properties == frozenset(raw["motion"]["animatable_properties"])
        assert table.reduced_motion_required is raw["motion"]["reduced_motion_required"]
        assert table.max_duration_ms == max(raw["motion"]["durations_ms"].values())

    def test_table_is_immutable(self, contract: DirectionContract) -> None:
        table = contract.motion_table()
        with pytest.raises(AttributeError):
            table.reduced_motion_required = False  # type: ignore[misc]

    def test_permits_on_contract_animation(self, contract: DirectionContract) -> None:
        table = contract.motion_table()
        assert table.permits(
            duration_ms=contract.motion.durations_ms["base"],
            easing=contract.motion.easings["standard"],
            property_name="opacity",
        )

    def test_reports_every_violation_at_once(self) -> None:
        table = MotionTable(
            durations_ms=frozenset({140}),
            easings=frozenset({"linear"}),
            animatable_properties=frozenset({"opacity"}),
            reduced_motion_required=True,
        )
        violations = table.violations(duration_ms=133, easing="ease-in-out", property_name="left")
        assert len(violations) == 3
        assert not table.permits(duration_ms=133, easing="ease-in-out", property_name="left")

    def test_non_compositor_property_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["motion"]["animatable_properties"] = ["transform", "height"]
        with pytest.raises(ValidationError, match="compositor-only"):
            DirectionContract.from_mapping(mutable_raw)
        assert "height" not in ANIMATABLE_PROPERTIES

    @pytest.mark.parametrize(
        "easing",
        ["ease-in-out", "cubic-bezier(0.2, 0, 0)", "cubic-bezier(1.4, 0, 0, 1)", "spring(1 2 3)"],
    )
    def test_off_contract_easings_rejected(
        self, mutable_raw: dict[str, Any], easing: str
    ) -> None:
        mutable_raw["motion"]["easings"]["standard"] = easing
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_linear_easing_allowed(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["motion"]["easings"]["standard"] = "linear"
        assert "linear" in DirectionContract.from_mapping(mutable_raw).motion_table().easings

    def test_absurd_duration_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["motion"]["durations_ms"]["slow"] = 5000
        with pytest.raises(ValidationError, match="outside 0"):
            DirectionContract.from_mapping(mutable_raw)

    def test_reduced_motion_cannot_be_waived(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["motion"]["reduced_motion_required"] = False
        with pytest.raises(ValidationError, match="reduced-motion"):
            DirectionContract.from_mapping(mutable_raw)


class TestDensityAxes:
    def test_bounds_clamp_and_contain(self, contract: DirectionContract) -> None:
        measure = contract.density["measure"]
        assert measure.clamp(1000.0) == measure.max
        assert measure.clamp(0.0) == measure.min
        assert measure.contains(measure.default)
        assert not measure.contains(measure.max + 1)

    def test_default_outside_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError, match="outside"):
            AxisBounds.model_validate({"min": 1.0, "max": 2.0, "default": 3.0})

    def test_inverted_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError, match="min < max"):
            AxisBounds.model_validate({"min": 2.0, "max": 1.0, "default": 1.5})

    def test_units_reach_css(self, contract: DirectionContract) -> None:
        assert contract.density["measure"].to_css_value().endswith("ch")


class TestReferences:
    def test_bar_is_concrete(self, contract: DirectionContract) -> None:
        assert len(contract.references) >= 2
        assert all(reference.note for reference in contract.references)
        assert any("shadcn" in reference.name for reference in contract.anti_references)

    def test_reference_cannot_also_be_anti_reference(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["anti_references"].append(dict(mutable_raw["references"][0]))
        with pytest.raises(ValidationError, match="both reference and anti-reference"):
            DirectionContract.from_mapping(mutable_raw)

    def test_empty_reference_set_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["references"] = []
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)


class TestCssEmission:
    def test_every_colour_role_emitted(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        for name, color in contract.palette.entries():
            assert f"  --color-{name}: {color.to_css()};" in css

    def test_families_emitted(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        assert "--type-family-display:" in css
        assert "--type-family-text:" in css
        assert "--color-scheme" not in css
        assert "color-scheme: dark;" in css

    def test_scale_values_are_computed_not_copied(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        expected = contract.type_scale.size_px("md") / contract.type_scale.rem_base_px
        assert f"--type-md: {expected:.4f}".rstrip("0").rstrip(".") + "rem;" in css
        assert "--space-base: 0.5rem;" in css

    def test_motion_tokens_emitted(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        for name, value in contract.motion.durations_ms.items():
            assert f"--motion-duration-{name}: {value}ms;" in css
        for name, value in contract.motion.easings.items():
            assert f"--motion-ease-{name}: {value};" in css

    def test_reduced_motion_branch_zeroes_durations(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        head, _, reduced = css.partition("@media (prefers-reduced-motion: reduce)")
        assert reduced, "contract requires reduced motion, so the branch must be emitted"
        for name in contract.motion.durations_ms:
            assert f"--motion-duration-{name}: 0ms;" in reduced
            assert f"--motion-duration-{name}: 0ms;" not in head

    def test_density_defaults_emitted(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        for axis, bounds in contract.density.items():
            assert f"--density-{axis}: {bounds.to_css_value()};" in css

    def test_emission_is_deterministic(self, contract: DirectionContract) -> None:
        assert contract.to_css_custom_properties() == contract.to_css_custom_properties()

    def test_braces_balance(self, contract: DirectionContract) -> None:
        css = contract.to_css_custom_properties()
        assert css.count("{") == css.count("}")
        assert css.endswith("\n")


class TestStrictNumerics:
    """Coercion turns 'the contract I wrote' into 'the contract that parsed'."""

    @pytest.mark.parametrize("version", ["1", True, 1.0, "one", None])
    def test_version_is_not_coerced(self, mutable_raw: dict[str, Any], version: Any) -> None:
        mutable_raw["meta"]["version"] = version
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize("value", ["true", 1, 0, "yes", None])
    def test_reduced_motion_flag_is_not_coerced(
        self, mutable_raw: dict[str, Any], value: Any
    ) -> None:
        mutable_raw["motion"]["reduced_motion_required"] = value
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize("value", ["140", True, 140.5, None])
    def test_durations_are_not_coerced(self, mutable_raw: dict[str, Any], value: Any) -> None:
        mutable_raw["motion"]["durations_ms"]["base"] = value
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize("value", ["0", True, 1.5, None])
    def test_step_exponents_are_not_coerced(
        self, mutable_raw: dict[str, Any], value: Any
    ) -> None:
        mutable_raw["type"]["steps"]["base"] = value
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_step_exponents_are_bounded(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["type"]["steps"]["xl"] = 10_000
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize("value", ["1.25", True, "inf", None, [1.25]])
    def test_ratio_is_not_coerced(self, mutable_raw: dict[str, Any], value: Any) -> None:
        mutable_raw["type"]["ratio"] = value
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize(
        "value", [float("inf"), float("-inf"), float("nan"), 1e400, -1e400]
    )
    def test_non_finite_density_bounds_rejected(
        self, mutable_raw: dict[str, Any], value: float
    ) -> None:
        mutable_raw["density"]["measure"]["max"] = value
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize("field", ["min", "max", "default"])
    def test_density_bounds_are_range_limited(
        self, mutable_raw: dict[str, Any], field: str
    ) -> None:
        mutable_raw["density"]["measure"][field] = 1e9
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_non_finite_colour_channel_rejected(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["palette"]["accent"]["c"] = float("nan")
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_infinite_axis_default_cannot_reach_css(
        self, mutable_raw: dict[str, Any], contract: DirectionContract
    ) -> None:
        mutable_raw["density"]["measure"]["default"] = float("inf")
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)
        css = contract.to_css_custom_properties()
        assert "inf" not in css and "nan" not in css

    def test_integers_are_still_accepted_where_a_float_is_expected(
        self, mutable_raw: dict[str, Any]
    ) -> None:
        mutable_raw["spacing"]["base_px"] = 8
        mutable_raw["type"]["rem_base_px"] = 16
        built = DirectionContract.from_mapping(mutable_raw)
        assert built.spacing.base_px == 8.0
        assert built.type_scale.rem_base_px == 16.0

    def test_rem_base_is_bounded(self, mutable_raw: dict[str, Any]) -> None:
        mutable_raw["type"]["rem_base_px"] = 0.0
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)


def _smuggle[T: BaseModel](model: T, **overrides: Any) -> T:
    """Rebuild a frozen model with unvalidated field values.

    Stands in for every route that reaches emission without passing the
    validators -- ``model_construct``, a future in-memory mutation, a bug. The
    emitter is supposed to survive it.
    """
    return type(model).model_construct(**{**model.__dict__, **overrides})


def _outside_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _skeleton(css: str) -> str:
    """The sheet with comments and string literals removed: pure structure."""
    return re.sub(r'"(?:\\.|[^"\\])*"', "STRING", _outside_comments(css))


_DECLARATION = re.compile(r" *(?:--[A-Za-z0-9_-]+|color-scheme): [^;{}]*;")
_STRUCTURE = re.compile(
    r"\s*|:root \{|\}|  \}|  :root \{|@media \(prefers-reduced-motion: reduce\) \{"
)


def assert_well_formed_sheet(css: str) -> None:
    """Every line is a declaration with a sanitised name, or a block delimiter."""
    for line in _skeleton(css).splitlines():
        assert _DECLARATION.fullmatch(line) or _STRUCTURE.fullmatch(line), line
    skeleton = _skeleton(css)
    assert skeleton.count("{") == skeleton.count("}") == 3


def _unescaped_quote_count(text: str) -> int:
    return len(re.findall(r'(?<!\\)"', text))


class TestCssInjection:
    """Contract text reaches a stylesheet, so contract text is untrusted input."""

    ESCAPE_PAYLOAD = "*/ body { color: red } /*"

    def test_comment_escaping_payload_rejected_at_parse_time(
        self, mutable_raw: dict[str, Any]
    ) -> None:
        mutable_raw["meta"]["revision"] = self.ESCAPE_PAYLOAD
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize(
        "revision",
        ["2026-08-05\n*/ body{}", "a { } b", "x; y", 'v"1', "*/", "/*", "", " leading-space"],
    )
    def test_revision_rejects_css_metacharacters(
        self, mutable_raw: dict[str, Any], revision: str
    ) -> None:
        mutable_raw["meta"]["revision"] = revision
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    def test_comment_payload_cannot_escape_even_if_validation_is_bypassed(
        self, contract: DirectionContract
    ) -> None:
        smuggled = contract.model_copy(
            update={"meta": _smuggle(contract.meta, revision=self.ESCAPE_PAYLOAD)}
        )
        css = smuggled.to_css_custom_properties()
        assert "*/ body" not in css
        outside = _outside_comments(css)
        assert "color: red" not in outside
        assert "body" not in outside

    def test_font_family_rejects_quote_escape_at_parse_time(
        self, mutable_raw: dict[str, Any]
    ) -> None:
        mutable_raw["type"]["display_family"]["name"] = 'Evil"; } body { display: none } .x {'
        with pytest.raises(ValidationError):
            DirectionContract.from_mapping(mutable_raw)

    @pytest.mark.parametrize(
        "name", ['a"b', "a;b", "a{b", "a}b", "a\\b", "a/*b", "a\nb", "", "9 lives; }"]
    )
    def test_font_family_rejects_metacharacters(self, name: str) -> None:
        with pytest.raises(ValidationError):
            FontFamily.model_validate({"name": name, "fallbacks": ["serif"]})

    def test_font_family_escapes_even_if_validation_is_bypassed(self) -> None:
        family = FontFamily.model_construct(
            name='Evil"; } body { display: none } .x {', fallbacks=("serif",)
        )
        stack = family.to_css_stack()
        assert _unescaped_quote_count(stack) == 2
        assert '\\"' in stack
        outside_strings = re.sub(r'"(?:\\.|[^"\\])*"', "STRING", stack)
        assert not set(outside_strings) & set("{};")

    def test_generated_sheet_has_no_stray_declarations(self, contract: DirectionContract) -> None:
        outside = _outside_comments(contract.to_css_custom_properties())
        assert outside.count("{") == outside.count("}") == 3
        assert "@import" not in outside
        assert "url(" not in outside

    def test_easing_value_cannot_open_a_block_if_validation_is_bypassed(
        self, contract: DirectionContract
    ) -> None:
        smuggled = contract.model_copy(
            update={
                "motion": _smuggle(
                    contract.motion, easings={"standard": "red; } body { display: none } .x {"}
                )
            }
        )
        outside = _outside_comments(smuggled.to_css_custom_properties())
        assert "display: none" not in outside
        assert outside.count("{") == outside.count("}") == 3

    def test_easing_sanitiser_is_a_no_op_on_valid_values(
        self, contract: DirectionContract
    ) -> None:
        css = contract.to_css_custom_properties()
        for name, value in contract.motion.easings.items():
            assert f"--motion-ease-{name}: {value};" in css

    NAME_PAYLOAD = "x: 0; } body { color: red } .a {"

    def test_clean_sheet_is_well_formed(self, contract: DirectionContract) -> None:
        assert_well_formed_sheet(contract.to_css_custom_properties())

    def test_density_axis_name_cannot_inject(self, contract: DirectionContract) -> None:
        smuggled = contract.model_copy(
            update={"density": {self.NAME_PAYLOAD: contract.density["measure"]}}
        )
        css = smuggled.to_css_custom_properties()
        assert_well_formed_sheet(css)
        assert "; }" not in _skeleton(css)
        assert "--density-x0bodycolorreda:" in css

    @pytest.mark.parametrize("axis", ["type_scale", "spacing"])
    def test_scale_step_names_cannot_inject(self, contract: DirectionContract, axis: str) -> None:
        scale = getattr(contract, axis)
        smuggled = contract.model_copy(
            update={
                axis: _smuggle(scale, steps={"base": 0, self.NAME_PAYLOAD: 1})
            }
        )
        assert_well_formed_sheet(smuggled.to_css_custom_properties())

    def test_motion_names_cannot_inject(self, contract: DirectionContract) -> None:
        smuggled = contract.model_copy(
            update={
                "motion": _smuggle(
                    contract.motion,
                    durations_ms={self.NAME_PAYLOAD: 140},
                    easings={self.NAME_PAYLOAD: "linear"},
                )
            }
        )
        css = smuggled.to_css_custom_properties()
        assert_well_formed_sheet(css)
        assert "--motion-duration-x0bodycolorreda: 140ms;" in css
        assert "--motion-ease-x0bodycolorreda: linear;" in css

    def test_empty_name_degrades_to_a_placeholder(self, contract: DirectionContract) -> None:
        smuggled = contract.model_copy(update={"density": {"@@@": contract.density["measure"]}})
        css = smuggled.to_css_custom_properties()
        assert_well_formed_sheet(css)
        assert "--density-invalid:" in css

    def test_smuggled_names_cannot_widen_the_class_allowlist(
        self, contract: DirectionContract
    ) -> None:
        smuggled = contract.model_copy(
            update={
                "motion": _smuggle(contract.motion, durations_ms={self.NAME_PAYLOAD: 140})
            }
        )
        pattern = re.compile(r"^[A-Za-z0-9_-]+$")
        assert all(pattern.fullmatch(name) for name in smuggled.class_allowlist_seed())

    def test_control_characters_stripped_from_strings(self) -> None:
        family = FontFamily.model_construct(name="A\x00B\x1fC", fallbacks=("serif",))
        assert "\x00" not in family.to_css_stack()
        assert "\x1f" not in family.to_css_stack()


class TestClassAllowlistSeed:
    def test_derived_from_every_token_family(self, contract: DirectionContract) -> None:
        seed = contract.class_allowlist_seed()
        assert {"bg-background", "text-accent-2", "border-border"} <= seed
        assert {"type-base", "type-display"} <= seed
        assert {"p-md", "gap-lg", "my-xs"} <= seed
        assert {"duration-quick", "ease-standard"} <= seed

    def test_off_contract_classes_absent(self, contract: DirectionContract) -> None:
        seed = contract.class_allowlist_seed()
        assert "bg-blue-500" not in seed
        assert "duration-137" not in seed
        assert "p-7" not in seed

    def test_is_immutable(self, contract: DirectionContract) -> None:
        assert isinstance(contract.class_allowlist_seed(), frozenset)

    def test_every_class_is_a_safe_css_identifier(self, contract: DirectionContract) -> None:
        pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        assert all(pattern.fullmatch(name) for name in contract.class_allowlist_seed())


class TestCli:
    def _run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ui_servo.domain.contract", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
        )

    def test_check_succeeds_and_summarises(self) -> None:
        result = self._run("--check")
        assert result.returncode == 0, result.stderr
        assert "ok " in result.stdout
        assert "reduced-motion required True" in result.stdout
        assert not result.stderr

    def test_emit_css_writes_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "nested" / "tokens.css"
        result = self._run("--emit-css", str(destination))
        assert result.returncode == 0, result.stderr
        assert "--color" in destination.read_text(encoding="utf-8")

    def test_emit_css_to_stdout(self) -> None:
        result = self._run("--emit-css", "-")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("/*")
        assert "--color-background:" in result.stdout

    def test_invalid_contract_exits_nonzero(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.toml"
        broken.write_text('[meta]\nversion = 2\nname = "x"\n', encoding="utf-8")
        result = self._run("--check", "--contract", str(broken))
        assert result.returncode == 1
        assert "invalid contract" in result.stderr

    def test_missing_contract_exits_two(self, tmp_path: Path) -> None:
        result = self._run("--check", "--contract", str(tmp_path / "absent.toml"))
        assert result.returncode == 2
        assert "cannot read contract" in result.stderr

    def test_default_resolves_to_the_checkout_contract(self) -> None:
        result = self._run("--check", env={"UI_SERVO_CONTRACT": ""})
        assert result.returncode == 0, result.stderr
        assert str(CONTRACT_PATH) in result.stdout

    def test_environment_override_wins(self, tmp_path: Path, contract_text: str) -> None:
        override = tmp_path / "elsewhere.toml"
        override.write_text(contract_text, encoding="utf-8")
        result = self._run("--check", env={"UI_SERVO_CONTRACT": str(override)})
        assert result.returncode == 0, result.stderr
        assert str(override) in result.stdout

    def test_missing_override_exits_two_and_never_falls_through(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        result = self._run("--check", env={"UI_SERVO_CONTRACT": str(missing)})
        assert result.returncode == 2
        assert str(missing) in result.stderr
        assert "does not name a readable file" in result.stderr
        assert str(CONTRACT_PATH) not in result.stdout
        assert "ok " not in result.stdout

    def test_override_naming_a_directory_exits_two(self, tmp_path: Path) -> None:
        result = self._run("--check", env={"UI_SERVO_CONTRACT": str(tmp_path)})
        assert result.returncode == 2
        assert str(tmp_path) in result.stderr

    def test_unreadable_override_exits_two(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked.toml"
        locked.write_text("[meta]\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            result = self._run("--check", env={"UI_SERVO_CONTRACT": str(locked)})
        finally:
            locked.chmod(0o600)
        assert result.returncode == 2
        assert "cannot read contract" in result.stderr

    def test_non_utf8_contract_exits_two(self, tmp_path: Path) -> None:
        binary = tmp_path / "binary.toml"
        binary.write_bytes(b"\xff\xfe[meta]\x00\x80version = 1\n")
        result = self._run("--check", "--contract", str(binary))
        assert result.returncode == 2
        assert "cannot read contract" in result.stderr

    def test_explicit_path_beats_the_environment(self, tmp_path: Path, contract_text: str) -> None:
        override = tmp_path / "env.toml"
        override.write_text(contract_text, encoding="utf-8")
        result = self._run(
            "--check", "--contract", str(CONTRACT_PATH), env={"UI_SERVO_CONTRACT": str(override)}
        )
        assert result.returncode == 0, result.stderr
        assert str(CONTRACT_PATH) in result.stdout
