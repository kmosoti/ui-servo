"""The slow loop's exploration half, and the two separations it has to hold.

The archive is a quality-*diversity* instrument, which means two things can go
wrong that a leaderboard cannot have wrong, and both are asserted here as
behaviour rather than as structure.

The first is the gate boundary. A variant that failed a hard gate has no business
in a discussion about taste, and the archive refuses it -- loudly, by raising,
because a silent refusal is indistinguishable from an ordinary loss and broken UI
would accumulate where nobody looks. Refusal is proven at both layers: on the
domain archive that takes a bare boolean, and on ``admit`` that takes a whole
regulator report.

The second is that fitness never overturns the panel. Distinctiveness is a bonus
capped at one rank step, so a novel candidate can pull level with the one above it
and never past it; the tie then goes to the incumbent. That is checked
arithmetically and then again through a placement, because the property that
matters is what ends up in the grid.

Everything else here is about being able to *see* the archive: axes that land in
[0, 1] and separate a styled fragment from a stock template, briefs that spread
across the space instead of clustering, a report that is one self-contained file
with a card per cell, and the single write path that puts a human's pick into the
exemplar store.

Runs against the real fixture samples from U10 and a real on-disk exemplar store.
No browser, no network, no model.
"""

import json
import re
from collections.abc import Sequence
from itertools import combinations, product
from pathlib import Path
from typing import Any

import pytest

from ui_servo.adapters.jsonl_store import JsonlExemplarStore
from ui_servo.control.explore import (
    FRAGMENT_FILENAME,
    admit,
    exemplar_name,
    frontier_report,
    gate_summary,
    picks_from,
    record_pick,
    seed_cells,
)
from ui_servo.control.explore import _spread_profile
from ui_servo.control.regulator import GateResult, REQUIRED_GATES, RegulatorReport
from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import SpanEvidence
from ui_servo.domain.variant import (
    AXIS_NAMES,
    DEFAULT_RESOLUTION,
    DISTINCTIVENESS_WEIGHT,
    Coverage,
    Elite,
    EliteArchive,
    GateFailure,
    MotionEvidence,
    StyleAxes,
    StyleSample,
    StyleVector,
    Variant,
    distance,
    fitness_of,
    rank_score,
)
from ui_servo.ports.store import META_FILENAME

FIXTURES = Path(__file__).parent / "fixtures" / "blandness"
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fixtures and small builders                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(
        (REPO_ROOT / "direction" / "direction.toml").read_text(encoding="utf-8")
    )


def sample(name: str) -> StyleSample:
    return StyleSample.parse(json.loads((FIXTURES / f"{name}.json").read_text()))


@pytest.fixture(scope="module")
def styled(contract: DirectionContract) -> StyleVector:
    return StyleVector.from_sample(sample("candidate_styled"), contract=contract)


@pytest.fixture(scope="module")
def bootstrap(contract: DirectionContract) -> StyleVector:
    return StyleVector.from_sample(sample("generic_bootstrap"), contract=contract)


@pytest.fixture(scope="module")
def shadcn(contract: DirectionContract) -> StyleVector:
    return StyleVector.from_sample(sample("generic_shadcn"), contract=contract)


def variant(variant_id: str = "v1", **overrides: Any) -> Variant:
    fields: dict[str, Any] = {
        "variant_id": variant_id,
        "part": "hero",
        "html": f'<section data-span-id="{variant_id}"><h1>Ship it</h1></section>',
        "builder_family": "family-a",
    }
    return Variant(**(fields | overrides))


def report(
    variant_id: str = "v1",
    *,
    passed: bool = True,
    vector: StyleVector | None = None,
    blandness_score: float | None = None,
    failing: str = REQUIRED_GATES[-1],
) -> RegulatorReport:
    """A whole report, every required gate present, passing unless told otherwise."""
    gates = tuple(
        GateResult.ok(name, "clean")
        if passed or name != failing
        else GateResult.failed(name, "an animation still runs under reduced motion")
        for name in REQUIRED_GATES
    )
    return RegulatorReport(
        variant_id=variant_id,
        gates=gates,
        evidence_span=SpanEvidence(span_id=variant_id, turn_id="turn-1", signals=()),
        style_vector=vector,
        blandness_score=blandness_score,
    )


def axes_at(density: float, expressiveness: float, motion: float) -> StyleAxes:
    return StyleAxes(
        density=density, expressiveness=expressiveness, motion_intensity=motion
    )


def png(path: Path, *, blob: bytes = b"\x89PNG\r\n\x1a\nfake") -> Path:
    path.write_bytes(blob)
    return path


# --------------------------------------------------------------------------- #
# The behaviour space                                                          #
# --------------------------------------------------------------------------- #


class TestStyleAxes:
    """Three coordinates, derived from the vector's public surface and nothing else."""

    def test_every_axis_is_a_fraction(
        self, styled: StyleVector, bootstrap: StyleVector, shadcn: StyleVector
    ) -> None:
        for vector in (styled, bootstrap, shadcn):
            axes = StyleAxes.of(vector)
            assert axes.values == tuple(axes.as_mapping()[name] for name in AXIS_NAMES)
            assert all(0.0 <= value <= 1.0 for value in axes.values), axes

    def test_derivation_is_deterministic(self, styled: StyleVector) -> None:
        assert StyleAxes.of(styled) == StyleAxes.of(styled)

    def test_the_styled_fragment_and_a_stock_template_land_apart(
        self, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        """The axes have to separate something, or the grid is one cell wearing 27 hats."""
        assert StyleAxes.of(styled).cell() != StyleAxes.of(bootstrap).cell()

    def test_a_vector_with_no_evidence_lands_mid_grid(
        self, contract: DirectionContract
    ) -> None:
        """Absence is "no opinion", so it must not read as the most distinctive corner."""
        empty = StyleVector.from_sample(StyleSample(), contract=contract)
        axes = StyleAxes.of(empty)
        assert axes.density == pytest.approx(0.5)
        assert axes.cell()[:2] == (1, 1)

    def test_tighter_spacing_reads_as_denser(self, contract: DirectionContract) -> None:
        def spaced(*values: float) -> StyleAxes:
            payload = {
                "elements": [
                    {"selector": "div", "area_px": 100.0, "spacing_px": list(values)}
                ]
            }
            return StyleAxes.of(
                StyleVector.from_sample(StyleSample.parse(payload), contract=contract)
            )

        base = contract.spacing.base_px
        tight = spaced(base / 1.5**2, base / 1.5)
        airy = spaced(base * 1.5**4, base * 1.5**5)
        assert tight.density > airy.density
        assert airy.density < 0.5 < tight.density

    def test_default_corners_and_flat_type_read_as_unexpressive(
        self, contract: DirectionContract
    ) -> None:
        flat = StyleSample.parse(
            {
                "elements": [
                    {
                        "selector": f"div:nth-child({index})",
                        "area_px": 100.0,
                        "font_size_px": contract.type_scale.base_px,
                        "border_radius_px": 6.0,
                    }
                    for index in range(4)
                ]
            }
        )
        loud = StyleSample.parse(
            {
                "elements": [
                    {
                        "selector": "h1",
                        "area_px": 100.0,
                        "font_size_px": contract.type_scale.base_px * 1.25**5,
                        "border_radius_px": 0.0,
                    },
                    {
                        "selector": "p",
                        "area_px": 100.0,
                        "font_size_px": contract.type_scale.base_px / 1.25**2,
                        "border_radius_px": 48.0,
                    },
                ]
            }
        )
        embed = lambda s: StyleAxes.of(StyleVector.from_sample(s, contract=contract))  # noqa: E731
        assert embed(flat).expressiveness == pytest.approx(0.0, abs=1e-9)
        assert embed(loud).expressiveness > 0.75

    def test_motion_is_evidence_not_embedding(self, styled: StyleVector) -> None:
        """The vector is a still frame; the third axis must come from the sensors."""
        still = StyleAxes.of(styled)
        moving = StyleAxes.of(
            styled,
            motion=MotionEvidence(
                animated_elements=8, total_elements=8, longest_duration_ms=400.0
            ),
        )
        assert still.motion_intensity == 0.0
        assert moving.motion_intensity == 1.0
        assert (still.density, still.expressiveness) == (
            moving.density,
            moving.expressiveness,
        )

    def test_motion_is_measured_against_the_contract_when_one_is_given(
        self, styled: StyleVector, contract: DirectionContract
    ) -> None:
        evidence = MotionEvidence(
            animated_elements=1, total_elements=1, longest_duration_ms=320.0
        )
        ceiling = contract.motion_table().max_duration_ms
        assert ceiling == 320
        assert StyleAxes.of(styled, motion=evidence, contract=contract).motion_intensity == 1.0
        assert StyleAxes.of(styled, motion=evidence).motion_intensity < 1.0

    def test_a_sensor_that_miscounts_saturates_rather_than_raising(self) -> None:
        assert MotionEvidence(animated_elements=9, total_elements=4).reach == 1.0

    def test_cell_binning_closes_the_top_edge(self) -> None:
        assert axes_at(0.0, 0.0, 0.0).cell() == (0, 0, 0)
        assert axes_at(1.0, 1.0, 1.0).cell() == (2, 2, 2)
        assert axes_at(0.5, 0.5, 0.5).cell() == (1, 1, 1)
        assert axes_at(1.0, 1.0, 1.0).cell(resolution=5) == (4, 4, 4)

    def test_the_derivation_never_indexes_the_vector(self) -> None:
        """The seam's rule, checked lexically: names of blocks, never bin offsets.

        A slice or an index into ``values`` in the archive half would survive every
        behavioural test above and then quietly mis-slice the first time the
        embedding gained a bin.
        """
        source = (REPO_ROOT / "ui_servo" / "domain" / "variant.py").read_text()
        _, _, archive_half = source.partition("# The behaviour space")
        assert archive_half, "the archive half of the module moved"
        assert not re.search(r"\.values\s*\[", archive_half)
        assert "COLOR_BINS" not in archive_half
        assert "SCALE_HIT_BINS" not in archive_half


# --------------------------------------------------------------------------- #
# Fitness                                                                      #
# --------------------------------------------------------------------------- #


class TestFitness:
    def test_rank_is_normalised_by_the_field_it_won(self) -> None:
        assert rank_score(1, 4) == 1.0
        assert rank_score(4, 4) == 0.0
        assert rank_score(1, 1) == 1.0
        assert rank_score(2, 3) == pytest.approx(0.5)

    def test_a_rank_outside_the_panel_is_refused(self) -> None:
        with pytest.raises(ValueError, match="outside a panel"):
            rank_score(0, 3)
        with pytest.raises(ValueError, match="outside a panel"):
            rank_score(4, 3)

    @pytest.mark.parametrize("panel_size", range(2, 9))
    def test_distinctiveness_never_overturns_the_panel(self, panel_size: int) -> None:
        """The whole Goodhart guard: a novelty bonus that cannot promote past a rank."""
        for rank in range(2, panel_size + 1):
            novel = fitness_of(panel_rank=rank, panel_size=panel_size, distinctiveness=1.0)
            above = fitness_of(panel_rank=rank - 1, panel_size=panel_size, distinctiveness=0.0)
            assert novel <= above + 1e-12

    def test_distinctiveness_still_orders_equals(self) -> None:
        dull = fitness_of(panel_rank=2, panel_size=4, distinctiveness=0.0)
        novel = fitness_of(panel_rank=2, panel_size=4, distinctiveness=0.8)
        assert novel > dull

    def test_the_bonus_is_capped_by_the_weight(self) -> None:
        assert fitness_of(panel_rank=1, panel_size=2, distinctiveness=1.0) == pytest.approx(
            1.0 + DISTINCTIVENESS_WEIGHT
        )

    def test_gates_are_not_an_argument(self) -> None:
        """Structural: there is no parameter through which a gate could be traded."""
        import inspect

        assert "passed" not in inspect.signature(fitness_of).parameters
        assert "gate" not in str(inspect.signature(fitness_of))

    def test_a_bogus_distinctiveness_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cosine distance"):
            fitness_of(panel_rank=1, panel_size=2, distinctiveness=1.5)


# --------------------------------------------------------------------------- #
# The archive                                                                  #
# --------------------------------------------------------------------------- #


class TestEliteArchive:
    def test_grid_shape(self) -> None:
        archive = EliteArchive()
        assert archive.resolution == DEFAULT_RESOLUTION
        assert archive.total_cells == DEFAULT_RESOLUTION ** len(AXIS_NAMES) == 27
        assert len(archive.all_cells()) == 27
        assert len(archive) == 0
        assert archive.empty_cells() == archive.all_cells()

    def test_keeps_the_fittest_per_cell(self) -> None:
        archive = EliteArchive()
        cell = axes_at(0.1, 0.1, 0.1)
        weak = archive.place(variant("weak"), cell, 0.2, passed=True)
        strong = archive.place(variant("strong"), cell, 0.9, passed=True)
        assert weak is not None and strong is not None
        assert len(archive) == 1
        assert archive.get((0, 0, 0)) is strong
        assert archive.get((0, 0, 0)).variant_id == "strong"

    def test_a_weaker_challenger_loses_and_says_so(self) -> None:
        archive = EliteArchive()
        cell = axes_at(0.1, 0.1, 0.1)
        archive.place(variant("strong"), cell, 0.9, passed=True)
        assert archive.place(variant("weak"), cell, 0.2, passed=True) is None
        assert archive.get((0, 0, 0)).variant_id == "strong"
        assert archive.offers == 2
        assert archive.placements == 1

    def test_the_incumbent_holds_a_tie(self) -> None:
        """No churn on equal fitness, which is what makes the capped bonus safe."""
        archive = EliteArchive()
        cell = axes_at(0.1, 0.1, 0.1)
        archive.place(variant("first"), cell, 0.5, passed=True)
        assert archive.place(variant("second"), cell, 0.5, passed=True) is None
        assert archive.get((0, 0, 0)).variant_id == "first"

    def test_competition_is_local_to_a_cell(self) -> None:
        """A globally mediocre variant keeps a corner nothing else has reached."""
        archive = EliteArchive()
        archive.place(variant("mainstream"), axes_at(0.5, 0.5, 0.5), 0.95, passed=True)
        lonely = archive.place(variant("odd"), axes_at(0.95, 0.95, 0.95), 0.05, passed=True)
        assert lonely is not None
        assert len(archive) == 2
        assert {elite.variant_id for elite in archive.frontier()} == {"mainstream", "odd"}

    def test_refuses_gate_failed_variants(self) -> None:
        archive = EliteArchive()
        with pytest.raises(GateFailure, match="precondition of entry"):
            archive.place(variant("broken"), axes_at(0.9, 0.9, 0.9), 9.0, passed=False)
        assert len(archive) == 0
        assert archive.offers == 0

    def test_a_gate_failure_cannot_be_bought_with_fitness(self) -> None:
        """The refusal is not a threshold; no score is high enough."""
        archive = EliteArchive()
        for fitness in (0.0, 1.0, 1e9):
            with pytest.raises(GateFailure):
                archive.place(variant("broken"), axes_at(0.5, 0.5, 0.5), fitness, passed=False)
        assert archive.coverage().occupied == 0

    def test_an_infinite_fitness_is_refused(self) -> None:
        archive = EliteArchive()
        with pytest.raises(ValueError, match="finite"):
            archive.place(variant("v"), axes_at(0.5, 0.5, 0.5), float("inf"), passed=True)

    def test_frontier_is_fittest_first_and_stable(self) -> None:
        archive = EliteArchive()
        archive.place(variant("mid"), axes_at(0.5, 0.5, 0.5), 0.5, passed=True)
        archive.place(variant("top"), axes_at(0.9, 0.1, 0.1), 0.9, passed=True)
        archive.place(variant("low"), axes_at(0.1, 0.9, 0.1), 0.1, passed=True)
        assert [elite.variant_id for elite in archive.frontier()] == ["top", "mid", "low"]
        assert list(archive) == archive.frontier()

    def test_distinctiveness_is_distance_to_the_nearest_thing_held(
        self, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = EliteArchive()
        assert archive.distinctiveness(styled) == 1.0, "an empty archive has no neighbours"
        archive.place(
            variant("first"), axes_at(0.1, 0.1, 0.1), 0.5, passed=True, style_vector=styled
        )
        assert archive.distinctiveness(styled) == pytest.approx(0.0, abs=1e-9)
        assert archive.distinctiveness(bootstrap) == pytest.approx(
            distance(bootstrap, styled)
        )

    def test_coverage_counts_cells_not_candidates(self) -> None:
        archive = EliteArchive()
        archive.place(variant("a"), axes_at(0.1, 0.1, 0.1), 0.4, passed=True)
        archive.place(variant("b"), axes_at(0.15, 0.15, 0.15), 0.8, passed=True)
        archive.place(variant("c"), axes_at(0.9, 0.9, 0.9), 0.6, passed=True)
        coverage = archive.coverage()
        assert isinstance(coverage, Coverage)
        assert coverage.occupied == 2
        assert coverage.total == 27
        assert coverage.ratio == pytest.approx(2 / 27)
        assert coverage.qd_score == pytest.approx(1.4)
        assert coverage.best_fitness == pytest.approx(0.8)
        assert coverage.mean_fitness == pytest.approx(0.7)
        assert len(coverage.empty_cells) == 25
        assert (0, 0, 0) not in coverage.empty_cells
        assert "2/27 cells occupied" in coverage.summary()

    def test_an_empty_archive_reports_no_best_rather_than_zero(self) -> None:
        coverage = EliteArchive().coverage()
        assert coverage.best_fitness is None and coverage.mean_fitness is None
        assert coverage.qd_score == 0.0

    def test_the_cell_a_brief_aimed_at_is_kept_beside_where_it_landed(self) -> None:
        """The unreachable-region signal: what was asked for, versus what arrived."""
        archive = EliteArchive()
        aimed = archive.place(
            variant("aimed", cell_hint=(2, 2, 2)), axes_at(0.1, 0.1, 0.1), 0.5, passed=True
        )
        assert aimed.aimed_at == (2, 2, 2)
        assert aimed.cell == (0, 0, 0)
        assert aimed.on_target is False
        unhinted = archive.place(variant("free"), axes_at(0.9, 0.9, 0.9), 0.5, passed=True)
        assert unhinted.on_target is None


# --------------------------------------------------------------------------- #
# Seeding the briefs                                                           #
# --------------------------------------------------------------------------- #


class TestSeedCells:
    def test_four_distinct_cells(self) -> None:
        cells = seed_cells(4)
        assert len(cells) == 4
        assert len(set(cells)) == 4
        assert all(all(0 <= axis < DEFAULT_RESOLUTION for axis in cell) for cell in cells)

    def test_four_cells_are_maximally_spread(self) -> None:
        """Checked against brute force, not against a remembered answer."""
        grid = list(product(range(DEFAULT_RESOLUTION), repeat=len(AXIS_NAMES)))
        best = max(_spread_profile(subset) for subset in combinations(grid, 4))
        assert _spread_profile(seed_cells(4)) == best

    @pytest.mark.parametrize("k", [2, 3, 4, 5, 6])
    def test_spread_is_optimal_for_the_sizes_a_round_actually_uses(self, k: int) -> None:
        grid = list(product(range(DEFAULT_RESOLUTION), repeat=len(AXIS_NAMES)))
        best = max(_spread_profile(subset) for subset in combinations(grid, k))
        assert _spread_profile(seed_cells(k)) == best

    def test_every_axis_is_exercised(self) -> None:
        """Four briefs that all shared a density would be four samples of one idea."""
        for axis in range(len(AXIS_NAMES)):
            assert len({cell[axis] for cell in seed_cells(4)}) > 1

    def test_deterministic(self) -> None:
        assert seed_cells(5) == seed_cells(5)

    def test_avoids_cells_the_archive_already_holds(self) -> None:
        taken = [(0, 0, 0), (2, 2, 2), (0, 2, 2)]
        cells = seed_cells(4, avoid=taken)
        assert not set(cells) & set(taken)

    def test_falls_back_to_the_whole_grid_rather_than_returning_fewer(self) -> None:
        grid = list(product(range(DEFAULT_RESOLUTION), repeat=len(AXIS_NAMES)))
        cells = seed_cells(4, avoid=grid[:25])
        assert len(cells) == 4 and len(set(cells)) == 4

    def test_a_finer_grid_is_supported(self) -> None:
        cells = seed_cells(6, resolution=4)
        assert len(set(cells)) == 6
        assert all(all(axis < 4 for axis in cell) for cell in cells)

    def test_refuses_impossible_requests(self) -> None:
        with pytest.raises(ValueError, match="at least one brief"):
            seed_cells(0)
        with pytest.raises(ValueError, match="only 27"):
            seed_cells(28)


# --------------------------------------------------------------------------- #
# admit                                                                        #
# --------------------------------------------------------------------------- #


class TestAdmit:
    def test_places_a_ranked_survivor(self, styled: StyleVector) -> None:
        archive = EliteArchive()
        elite = admit(
            report("v1", vector=styled, blandness_score=0.42),
            1,
            archive=archive,
            variant=variant("v1"),
            panel_size=3,
            findings=["nav links sit off the type scale"],
        )
        assert elite is not None
        assert archive.get(elite.cell) is elite
        assert elite.panel_rank == 1
        assert elite.blandness_score == 0.42
        assert elite.gate_summary == f"{len(REQUIRED_GATES)}/{len(REQUIRED_GATES)} gates passed"
        assert elite.findings == ("nav links sit off the type scale",)
        assert elite.fitness >= 1.0, "rank 1 plus a first-artefact novelty bonus"

    def test_refuses_a_gate_failed_report(self, styled: StyleVector) -> None:
        archive = EliteArchive()
        with pytest.raises(GateFailure) as caught:
            admit(
                report("broken", passed=False, vector=styled),
                1,
                archive=archive,
                variant=variant("broken"),
                panel_size=3,
            )
        assert REQUIRED_GATES[-1] in str(caught.value)
        assert len(archive) == 0

    def test_refuses_a_report_with_no_style_vector(self) -> None:
        with pytest.raises(ValueError, match="no style vector"):
            admit(
                report("v1"),
                1,
                archive=EliteArchive(),
                variant=variant("v1"),
                panel_size=2,
            )

    def test_blandness_is_carried_but_not_summed_into_fitness(
        self, styled: StyleVector
    ) -> None:
        """Taste rides along for the human; only distinctiveness scores."""
        bland = admit(
            report("bland", vector=styled, blandness_score=0.01),
            1,
            archive=EliteArchive(),
            variant=variant("bland"),
            panel_size=2,
        )
        distinct = admit(
            report("distinct", vector=styled, blandness_score=0.99),
            1,
            archive=EliteArchive(),
            variant=variant("distinct"),
            panel_size=2,
        )
        assert bland.blandness_score != distinct.blandness_score
        assert bland.fitness == distinct.fitness

    def test_the_second_arrival_in_a_cell_is_judged_against_the_first(
        self, styled: StyleVector
    ) -> None:
        archive = EliteArchive()
        winner = admit(
            report("winner", vector=styled),
            1,
            archive=archive,
            variant=variant("winner"),
            panel_size=2,
        )
        runner_up = admit(
            report("runner-up", vector=styled),
            2,
            archive=archive,
            variant=variant("runner-up"),
            panel_size=2,
        )
        assert runner_up is None, "same cell, worse rank, identical vector"
        assert archive.get(winner.cell).variant_id == "winner"

    def test_motion_evidence_moves_the_cell(self, styled: StyleVector) -> None:
        still = admit(
            report("still", vector=styled),
            1,
            archive=EliteArchive(),
            variant=variant("still"),
            panel_size=2,
        )
        moving = admit(
            report("moving", vector=styled),
            1,
            archive=EliteArchive(),
            variant=variant("moving"),
            panel_size=2,
            motion=MotionEvidence(
                animated_elements=6, total_elements=6, longest_duration_ms=400.0
            ),
        )
        assert still.cell[2] == 0
        assert moving.cell[2] == 2

    def test_gate_summary_names_what_failed(self, styled: StyleVector) -> None:
        failed = report("broken", passed=False, vector=styled)
        summary = gate_summary(failed)
        assert summary.startswith(f"{len(REQUIRED_GATES) - 1}/{len(REQUIRED_GATES)}")
        assert REQUIRED_GATES[-1] in summary


# --------------------------------------------------------------------------- #
# The frontier report                                                          #
# --------------------------------------------------------------------------- #


def populated(tmp_path: Path, styled: StyleVector, bootstrap: StyleVector) -> EliteArchive:
    archive = EliteArchive()
    admit(
        report("hero-a", vector=styled, blandness_score=0.31),
        1,
        archive=archive,
        variant=variant("hero-a", cell_hint=(0, 0, 0)),
        panel_size=3,
        findings=["the nav <a> sits off the type scale"],
        screenshot=png(tmp_path / "hero-a.png"),
    )
    admit(
        report("hero-b", vector=bootstrap, blandness_score=0.04),
        2,
        archive=archive,
        variant=variant("hero-b", builder_family="family-b"),
        panel_size=3,
        findings=["reads as the stock landing page"],
        screenshot=tmp_path / "missing.png",
    )
    admit(
        report("hero-c", vector=styled),
        3,
        archive=archive,
        variant=variant("hero-c", builder_family="family-c"),
        panel_size=3,
        motion=MotionEvidence(
            animated_elements=4, total_elements=4, longest_duration_ms=400.0
        ),
    )
    return archive


class TestFrontierReport:
    def test_renders_one_card_per_occupied_cell(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = populated(tmp_path, styled, bootstrap)
        out = frontier_report(archive, tmp_path / "reports" / "frontier.html")
        html = out.read_text(encoding="utf-8")
        assert out.exists()
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")
        assert html.count('<article class="card">') == len(archive)
        assert html.count("</article>") == len(archive)
        for elite in archive.frontier():
            assert elite.variant.variant_id in html
            assert str(elite.cell) in html

    def test_a_card_carries_everything_the_pick_needs(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = populated(tmp_path, styled, bootstrap)
        html = frontier_report(archive, tmp_path / "frontier.html").read_text()
        assert "data:image/png;base64," in html, "screenshots are inlined"
        for axis in AXIS_NAMES:
            assert axis in html
        assert "gates passed" in html
        assert "blandness" in html
        assert "the nav" in html and "type scale" in html
        assert "panel rank" in html

    def test_it_is_self_contained(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        """No fetch at view time: the report must not change after it was read."""
        html = frontier_report(
            populated(tmp_path, styled, bootstrap), tmp_path / "frontier.html"
        ).read_text()
        assert "<link" not in html
        assert "<script" not in html
        assert not re.search(r'src="(?!data:)', html)
        assert "http://" not in html and "https://" not in html

    def test_coverage_and_the_unreached_cells_are_shown(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = populated(tmp_path, styled, bootstrap)
        html = frontier_report(archive, tmp_path / "frontier.html").read_text()
        assert archive.coverage().summary() in html
        assert "Unreached cells" in html
        for cell in archive.empty_cells():
            assert str(cell) in html

    def test_a_missing_screenshot_becomes_a_placeholder(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        html = frontier_report(
            populated(tmp_path, styled, bootstrap), tmp_path / "frontier.html"
        ).read_text()
        assert "screenshot unavailable: missing.png" in html

    def test_model_written_findings_are_escaped(self, tmp_path: Path, styled: StyleVector) -> None:
        """Findings are untrusted text; a critic must not be able to script the report."""
        archive = EliteArchive()
        admit(
            report("v1", vector=styled),
            1,
            archive=archive,
            variant=variant("v1"),
            panel_size=2,
            findings=['<script>alert("xss")</script> & <img src=x onerror=1>'],
        )
        html = frontier_report(archive, tmp_path / "frontier.html").read_text()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "onerror" not in html.replace("onerror=1", "")

    def test_an_empty_archive_still_renders(self, tmp_path: Path) -> None:
        html = frontier_report(EliteArchive(), tmp_path / "frontier.html").read_text()
        assert "the archive is empty" in html
        assert "0/27 cells occupied" in html

    def test_a_fixed_clock_makes_the_report_reproducible(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = populated(tmp_path, styled, bootstrap)
        first = frontier_report(
            archive, tmp_path / "a.html", now=lambda: "2026-01-01T00:00:00+00:00"
        ).read_text()
        second = frontier_report(
            archive, tmp_path / "b.html", now=lambda: "2026-01-01T00:00:00+00:00"
        ).read_text()
        assert first == second
        assert "2026-01-01T00:00:00+00:00" in first


# --------------------------------------------------------------------------- #
# The human's pick                                                             #
# --------------------------------------------------------------------------- #


class TestRecordPick:
    def test_writes_the_fragment_the_screenshot_and_the_provenance(
        self, tmp_path: Path, styled: StyleVector
    ) -> None:
        store = JsonlExemplarStore(tmp_path)
        shot = png(tmp_path / "hero-a.png")
        archive = EliteArchive()
        elite = admit(
            report("hero-a", vector=styled, blandness_score=0.31),
            1,
            archive=archive,
            variant=variant("hero-a", cell_hint=(0, 0, 0)),
            panel_size=3,
            findings=["the nav sits off the type scale"],
            screenshot=shot,
        )

        exemplar = record_pick(
            elite,
            store,
            note="the only one that reads like a page",
            picked_by="kmosoti",
            now=lambda: "2026-08-05T12:00:00+00:00",
        )

        assert exemplar.name == "hero-hero-a"
        assert set(exemplar.files) == {FRAGMENT_FILENAME, "screenshot.png"}
        saved = store.path_for(exemplar.name)
        assert (saved / FRAGMENT_FILENAME).read_text() == elite.variant.html
        assert (saved / "screenshot.png").read_bytes() == shot.read_bytes()

        meta = json.loads((saved / META_FILENAME).read_text())
        assert meta["variant_id"] == "hero-a"
        assert meta["part"] == "hero"
        assert meta["cell"] == list(elite.cell)
        assert meta["cell_hint"] == [0, 0, 0]
        assert meta["panel_rank"] == 1
        assert meta["blandness_score"] == 0.31
        assert meta["axes"] == elite.axes.as_mapping()
        assert meta["findings"] == ["the nav sits off the type scale"]
        assert meta["picked_by"] == "kmosoti"
        assert meta["note"] == "the only one that reads like a page"
        assert meta["picked_at"] == "2026-08-05T12:00:00+00:00"
        assert meta["gate_summary"].endswith("gates passed")

        assert [held.name for held in store.list_exemplars()] == ["hero-hero-a"]

    def test_a_pick_without_a_screenshot_still_records_the_markup(
        self, tmp_path: Path
    ) -> None:
        store = JsonlExemplarStore(tmp_path)
        elite = Elite(
            variant=variant("v1"), axes=axes_at(0.5, 0.5, 0.5), cell=(1, 1, 1), fitness=1.0
        )
        exemplar = record_pick(elite, store)
        assert set(exemplar.files) == {FRAGMENT_FILENAME}

    def test_an_unreadable_screenshot_fails_loudly(self, tmp_path: Path) -> None:
        """Half an exemplar is worse than a clear failure while the file is recoverable."""
        store = JsonlExemplarStore(tmp_path)
        elite = Elite(
            variant=variant("v1"),
            axes=axes_at(0.5, 0.5, 0.5),
            cell=(1, 1, 1),
            fitness=1.0,
            screenshot=str(tmp_path / "gone.png"),
        )
        with pytest.raises(ValueError, match="cannot be read"):
            record_pick(elite, store)
        assert store.list_exemplars() == ()

    def test_an_explicit_name_wins(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        elite = Elite(
            variant=variant("v1"), axes=axes_at(0.5, 0.5, 0.5), cell=(1, 1, 1), fitness=1.0
        )
        assert record_pick(elite, store, name="ember-hero-v3").name == "ember-hero-v3"

    def test_derived_names_are_safe_path_components(self) -> None:
        elite = Elite(
            variant=variant("v 1/../etc", part="hero card"),
            axes=axes_at(0.5, 0.5, 0.5),
            cell=(1, 1, 1),
            fitness=1.0,
        )
        assert exemplar_name(elite) == "hero-card-v-1-..-etc"
        assert "/" not in exemplar_name(elite)

    def test_it_is_the_only_writer_of_taste(self) -> None:
        """Conant-Ashby, enforced lexically: the model may not extend its own bar.

        Every other module measures, ranks and reports. If a second call site for
        ``save_exemplar`` ever appears outside the adapters and this test, the loop
        has gained a path by which it learns from its own preferences with no human
        in it.
        """
        callers = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "ui_servo").rglob("*.py")
            if "__pycache__" not in path.parts
            and "save_exemplar(" in path.read_text(encoding="utf-8")
            and not path.as_posix().endswith(("ports/store.py",))
            and "adapters/" not in path.relative_to(REPO_ROOT).as_posix()
        )
        assert callers == ["ui_servo/control/explore.py"]

    def test_picks_from_resolves_cells_and_refuses_typos(
        self, tmp_path: Path, styled: StyleVector, bootstrap: StyleVector
    ) -> None:
        archive = populated(tmp_path, styled, bootstrap)
        occupied = archive.occupied_cells()
        assert [elite.cell for elite in picks_from(archive, occupied)] == list(occupied)
        with pytest.raises(KeyError, match="no elite occupies"):
            picks_from(archive, [(2, 2, 1)])


# --------------------------------------------------------------------------- #
# The round, end to end                                                        #
# --------------------------------------------------------------------------- #


def test_one_exploration_cycle(
    tmp_path: Path, styled: StyleVector, bootstrap: StyleVector, shadcn: StyleVector
) -> None:
    """Seed briefs, admit survivors, refuse the broken one, report, pick."""
    archive = EliteArchive()
    briefs = seed_cells(3)
    assert len(set(briefs)) == 3

    vectors: Sequence[tuple[str, StyleVector]] = (
        ("hero-a", styled),
        ("hero-b", bootstrap),
        ("hero-c", shadcn),
    )
    for rank, ((name, vector), brief) in enumerate(zip(vectors, briefs, strict=True), start=1):
        admit(
            report(name, vector=vector, blandness_score=0.1 * rank),
            rank,
            archive=archive,
            variant=variant(name, cell_hint=brief),
            panel_size=len(vectors) + 1,
            screenshot=png(tmp_path / f"{name}.png"),
        )

    with pytest.raises(GateFailure):
        admit(
            report("hero-d", passed=False, vector=styled),
            4,
            archive=archive,
            variant=variant("hero-d"),
            panel_size=4,
        )

    assert 0 < len(archive) <= 3
    assert "hero-d" not in {elite.variant_id for elite in archive.frontier()}

    out = frontier_report(archive, tmp_path / "out" / "frontier.html")
    assert out.exists() and out.stat().st_size > 0

    store = JsonlExemplarStore(tmp_path)
    picked = record_pick(archive.frontier()[0], store, picked_by="kmosoti")
    assert len(store.list_exemplars()) == 1
    assert FRAGMENT_FILENAME in picked.files
