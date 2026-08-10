"""One whole round, with the world replaced by fakes.

The properties worth testing about a composition root are not the ones its parts
already guarantee. The regulator's gates are tested in ``test_regulator``, the
panel's governance in ``test_critique``; what is only true *here* is the ordering
and the accounting:

* a variant that failed a gate is in ``rejected`` with the gate named, and never
  reached a judge -- asserted against the judges' own call log, because "we did
  not ask" is the saving the whole tier ordering exists to produce;
* a clean variant is ranked, and the round wrote a report a human can open;
* the evidence stock holds a signal from every stage of the round -- the sanitiser,
  the gates, the blind staging, the panel and the round itself -- because a stage
  that left no flow in the stock is a stage nobody can audit afterwards;
* the staged artefacts a critic is pointed at carry no family token and no variant
  id, and a fragment that stamps its author into its own markup stops the round.

The CLI smoke test at the bottom drives ``--dry-judges`` over the fixture
candidates directory. It composes the real adapters -- the JSONL store, nh3, a
headless browser, the preview server -- and is marked ``playwright`` so a machine
without a browser can skip it without the rest of the file going untested.
"""

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ui_servo.adapters.jsonl_store import EXEMPLAR_DIRNAME
from ui_servo.cli.servo import build_stores
from ui_servo.control import servo
from ui_servo.control.regulator import (
    GATE_NO_UNKNOWN_CLASS,
    GATE_SANITIZED,
)
from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.domain.policy import (
    FAMILY_TOKENS,
    RUBRIC_AXES,
    BlindnessError,
    is_blind,
)
from ui_servo.domain.variant import Variant
from ui_servo.ports.judge import JudgeRequest, JudgeResponse
from ui_servo.ports.sanitizer import SanitizeResult, Violation, ViolationKind
from ui_servo.ports.sensor import (
    SensorReport,
    span_screenshot_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = REPO_ROOT / "tests" / "fixtures" / "candidates"
CONTRACT = REPO_ROOT / "direction" / "direction.toml"

CLEAN_HTML = '<section class="p-lg" data-span-id="clean">clean</section>'
BROKEN_HTML = '<section class="glass-card" data-span-id="broken">broken</section>'


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class RoutingSanitizer:
    """Accepts anything but the one fragment the fixture calls broken."""

    def check(self, fragment_html: str) -> SanitizeResult:
        if "glass-card" in fragment_html:
            return SanitizeResult.rejected(
                [
                    Violation(
                        kind=ViolationKind.UNKNOWN_CLASS,
                        locator="section",
                        detail="class 'glass-card' is not in the contract's allowlist",
                    )
                ]
            )
        return SanitizeResult.ok(fragment_html)


class ShotSensor:
    """A sensor that writes a one-byte PNG per span, so staging has something to copy."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.observed: list[str] = []

    def observe(
        self,
        url: str,
        *,
        span_id: SpanId,
        turn_id: TurnId,
        baseline: Path | None = None,
        interactions: Sequence[Any] | None = None,
    ) -> SensorReport:
        self.observed.append(span_id)
        directory = self.root / span_id
        directory.mkdir(parents=True, exist_ok=True)
        shot = directory / "full-page.png"
        shot.write_bytes(b"\x89PNG")
        return SensorReport(
            span_id=span_id,
            turn_id=turn_id,
            url=url,
            screenshots={span_screenshot_key(span_id): shot},
            aria_snapshot=f'- heading "{span_id}" [level=2]',
        )


def _marked(screenshot: Path) -> bool:
    """Whether the staged fragment beside *screenshot* carries the fixture's marker.

    The fake critic has to judge by *content* rather than by position, because the
    panel flips presentation order per family: a judge that always answered "B"
    would be answering a different question for each family, and the caller-frame
    winner would depend on a seed instead of on the fixture.
    """
    try:
        return "second" in screenshot.with_suffix(".html").read_text(encoding="utf-8")
    except OSError:
        return False


class RecordingJudge:
    """A judge that prefers the fragment marked ``second``, and logs every ask."""

    def __init__(self, family: str) -> None:
        self.family = family
        self.calls: list[JudgeRequest] = []

    def judge(self, request: JudgeRequest) -> JudgeResponse:
        self.calls.append(request)
        first, second = (request.image_paths + (Path("x"), Path("x")))[:2]
        winner = "A" if _marked(first) else "B" if _marked(second) else "tie"
        verdict = {
            "winner": winner,
            "per_axis": {axis: winner for axis in RUBRIC_AXES},
            "findings": [
                {
                    "axis": "hierarchy",
                    "selector": "section > h2",
                    "gap": "the heading does not lead",
                    "severity": "minor",
                }
            ],
            "confidence": 0.5,
        }
        return JudgeResponse(family=self.family, raw=json.dumps(verdict), parsed=verdict)


class MemoryStore:
    """The evidence port, in a list."""

    def __init__(self) -> None:
        self.signals: list[Signal] = []

    def append(self, signal: Signal) -> None:
        self.signals.append(signal)

    def append_many(self, signals: Any) -> int:
        added = list(signals)
        self.signals.extend(added)
        return len(added)

    def signals_for_turn(self, turn_id: TurnId) -> tuple[Signal, ...]:
        return tuple(signal for signal in self.signals if signal.turn_id == turn_id)

    def signals_for_span(
        self, span_id: SpanId, *, turn_id: TurnId | None = None
    ) -> tuple[Signal, ...]:
        return tuple(
            signal
            for signal in self.signals
            if signal.span_id == span_id and (turn_id is None or signal.turn_id == turn_id)
        )

    def kinds(self) -> set[str]:
        return {signal.kind for signal in self.signals}


@pytest.fixture(scope="session")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    return {
        "store": MemoryStore(),
        "sanitizer": RoutingSanitizer(),
        "sensor": ShotSensor(tmp_path / "shots"),
        "judges": [RecordingJudge(family) for family in ("wren", "finch", "heron")],
        "out": tmp_path / "round",
    }


def variants() -> list[Variant]:
    return [
        Variant(variant_id="demo.alpha.0", part="demo", html=CLEAN_HTML, builder_family="alpha"),
        Variant(variant_id="demo.beta.1", part="demo", html=CLEAN_HTML.replace("clean", "second"), builder_family="beta"),
        Variant(variant_id="demo.delta.2", part="demo", html=BROKEN_HTML, builder_family="delta"),
    ]


def run(world: dict[str, Any], contract: DirectionContract, **overrides: Any) -> servo.RoundResult:
    field = overrides.pop("variants", None) or variants()
    kwargs: dict[str, Any] = {
        "contract": contract,
        "stores": servo.Stores(evidence=world["store"]),
        "sanitizer": world["sanitizer"],
        "sensor": world["sensor"],
        "judges": world["judges"],
        "round_id": "1",
        "out_dir": world["out"],
        "url_for": lambda variant: f"http://localhost/candidate/{variant.variant_id}",
        "part": "demo",
    }
    kwargs |= overrides
    return servo.run_round(field, **kwargs)


# --------------------------------------------------------------------------- #
# Filename grammar                                                             #
# --------------------------------------------------------------------------- #


class TestCandidateNames:
    def test_three_token_grammar_carries_the_variant_index(self, tmp_path: Path) -> None:
        parsed = servo.parse_candidate_name(tmp_path / "hero.claude.2.html")
        assert (parsed.part, parsed.builder_family, parsed.index) == ("hero", "claude", 2)

    def test_two_token_legacy_name_is_variant_zero(self, tmp_path: Path) -> None:
        parsed = servo.parse_candidate_name(tmp_path / "hero.codex.html")
        assert (parsed.part, parsed.builder_family, parsed.index) == ("hero", "codex", 0)

    @pytest.mark.parametrize("name", ["hero.html", "hero.claude.two.html", "hero.claude.2.3.html"])
    def test_a_name_the_loop_would_have_to_guess_at_is_refused(
        self, tmp_path: Path, name: str
    ) -> None:
        with pytest.raises(servo.CandidateNameError):
            servo.parse_candidate_name(tmp_path / name)

    def test_the_fixture_directory_loads_one_part(self) -> None:
        loaded = servo.load_candidates(CANDIDATES, part="demo")
        assert [variant.variant_id for variant in loaded] == [
            "demo.alpha.0",
            "demo.beta.1",
            "demo.delta.2",
        ]
        assert [variant.builder_family for variant in loaded] == ["alpha", "beta", "delta"]

    def test_an_unknown_part_loads_nothing(self) -> None:
        assert servo.load_candidates(CANDIDATES, part="hero") == ()


# --------------------------------------------------------------------------- #
# The round                                                                    #
# --------------------------------------------------------------------------- #


class TestRound:
    def test_a_gate_failure_is_rejected_with_the_gate_named(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        assert [rejection.variant_id for rejection in result.rejected] == ["demo.delta.2"]
        rejected = result.rejected[0]
        assert GATE_SANITIZED in rejected.failed_gates
        assert GATE_NO_UNKNOWN_CLASS in rejected.failed_gates
        assert "glass-card" in rejected.detail
        assert rejected.offenders, "a rejection must carry the evidence that produced it"

    def test_a_rejected_variant_never_reaches_a_judge(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        asked = "\n".join(
            request.prompt for judge in world["judges"] for request in judge.calls
        )
        assert asked, "the survivors were supposed to be argued about"
        assert "demo.delta.2" not in asked
        staged = {entry["variant_id"] for entry in result.blind_map}
        assert staged == {"demo.alpha.0", "demo.beta.1"}

    def test_survivors_are_ranked_and_the_report_exists(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        assert [ranked.variant_id for ranked in result.ranked] == [
            "demo.beta.1",
            "demo.alpha.0",
        ], "the judges always prefer B, and B is the second-listed survivor"
        assert result.ranked[0].rank == 1
        assert result.ranked[0].score == servo.WIN_POINTS
        assert result.ranked[0].wins == 1 and result.ranked[1].losses == 1
        assert result.report_path is not None and result.report_path.is_file()
        assert result.report_path.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_findings_come_back_attached_to_the_survivors(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        selectors = {
            finding["selector"] for ranked in result.ranked for finding in ranked.findings
        }
        assert selectors == {"section > h2"}

    def test_the_result_is_serialised_next_to_the_artefacts(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        assert result.result_path is not None
        payload = json.loads(result.result_path.read_text(encoding="utf-8"))
        assert payload["round_id"] == "1"
        assert [entry["variant_id"] for entry in payload["rejected"]] == ["demo.delta.2"]
        assert payload["blind_map"], "the unblinding key belongs in the round record"

    def test_the_stock_holds_a_signal_from_every_stage(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        run(world, contract)
        kinds = world["store"].kinds()
        assert {"sanitize", "gate", servo.STAGING_KIND, servo.ROUND_KIND} <= kinds
        assert {"panel-verdict", "panel-outcome"} <= kinds
        sources = {signal.source for signal in world["store"].signals}
        assert {"gate", "judge"} <= sources

    def test_probe_signals_already_in_the_stock_are_not_written_back(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        store: MemoryStore = world["store"]
        store.append(
            Signal(
                span_id="demo.alpha.0",
                turn_id="turn-0",
                source="probe",
                kind="overflow",
                ts="2026-08-05T00:00:00+00:00",
                payload={"selector": "section"},
            )
        )
        result = run(world, contract)
        overflow = [signal for signal in store.signals if signal.kind == "overflow"]
        assert len(overflow) == 1, "a re-read observation must not become a second flow"
        assert "demo.alpha.0" in {
            rejection.variant_id for rejection in result.rejected
        }, "a probe overflow is a gate failure, and the gate must see it"

    def test_a_field_of_one_is_ranked_without_a_comparison(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        only = [
            Variant(
                variant_id="demo.alpha.0", part="demo", html=CLEAN_HTML, builder_family="alpha"
            )
        ]
        result = run(world, contract, variants=only)
        assert result.comparisons == ()
        assert [ranked.variant_id for ranked in result.ranked] == ["demo.alpha.0"]
        assert result.report_path is not None and result.report_path.is_file()

    def test_a_larger_field_is_capped_rather_than_run_at_quadratic_cost(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        many = [
            Variant(
                variant_id=f"demo.fam{index}.0",
                part="demo",
                html=CLEAN_HTML.replace("clean", f"clean-{index}"),
                builder_family=f"fam{index}",
            )
            for index in range(servo.MAX_TOURNAMENT_FIELD + 2)
        ]
        result = run(world, contract, variants=many)
        assert len(result.not_argued) == 2
        expected = servo.MAX_TOURNAMENT_FIELD * (servo.MAX_TOURNAMENT_FIELD - 1) // 2
        assert len(result.comparisons) == expected


# --------------------------------------------------------------------------- #
# Blindness                                                                    #
# --------------------------------------------------------------------------- #


class TestBlindStaging:
    def test_staged_paths_carry_no_provenance(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        result = run(world, contract)
        for comparison in result.comparisons:
            for staged in (comparison.a, comparison.b):
                name = staged.screenshot.name
                assert staged.variant_id not in str(staged.screenshot)
                assert staged.builder_family not in name
                assert name.startswith(f"demo.{staged.label}.")
                assert staged.screenshot.parent.name == comparison.comparison_id
                assert staged.screenshot.is_file()
                assert staged.html.is_file()

    def test_every_prompt_that_was_sent_is_blind(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        run(world, contract)
        asked = [request for judge in world["judges"] for request in judge.calls]
        assert asked
        for request in asked:
            assert is_blind(request.prompt, extra_tokens=("alpha", "beta", "delta"))

    def test_the_same_variant_is_a_different_filename_in_every_comparison(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        three = [
            Variant(
                variant_id=f"demo.fam{index}.0",
                part="demo",
                html=CLEAN_HTML.replace("clean", f"clean-{index}"),
                builder_family=f"fam{index}",
            )
            for index in range(3)
        ]
        result = run(world, contract, variants=three)
        names: dict[str, set[str]] = {}
        for comparison in result.comparisons:
            for staged in (comparison.a, comparison.b):
                names.setdefault(staged.variant_id, set()).add(staged.screenshot.name)
        assert all(len(seen) == 2 for seen in names.values()), (
            "a filename repeated across comparisons is a way to correlate prompts"
        )

    def test_a_fragment_that_names_its_builder_stops_the_round(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        leaky = [
            Variant(
                variant_id="demo.alpha.0",
                part="demo",
                html='<section data-span-id="alpha-hero">hi</section>',
                builder_family="alpha",
            ),
            Variant(
                variant_id="demo.beta.1", part="demo", html=CLEAN_HTML, builder_family="beta"
            ),
        ]
        with pytest.raises(BlindnessError):
            run(world, contract, variants=leaky)

    def test_notes_that_would_unblind_are_dropped_rather_than_sent(
        self, world: dict[str, Any], contract: DirectionContract
    ) -> None:
        leaky = servo._Observation(
            variant=variants()[0],
            report=None,  # type: ignore[arg-type] -- unused by the notes filter
            notes="- heading built by alpha [level=2]",
        )
        # The accessibility tree names the builder, so it is dropped. The
        # markup, which is clean, still goes -- a text-only critic reads the
        # markup, and dropping it too would silence a whole family over a leak
        # that was not in it.
        blinded = servo._blind_notes(leaky, ("alpha",))
        assert "alpha" not in blinded
        assert "accessibility tree" not in blinded
        assert "markup:" in blinded

        # With no leak to find, both halves are presented.
        both = servo._blind_notes(leaky, ())
        assert "markup:" in both and "accessibility tree" in both


# --------------------------------------------------------------------------- #
# The stub panel                                                               #
# --------------------------------------------------------------------------- #


class TestDryPanel:
    def test_the_stub_ranks_by_the_measured_score_beside_the_screenshot(
        self, tmp_path: Path
    ) -> None:
        bland, distinct = tmp_path / "a.png", tmp_path / "b.png"
        (tmp_path / "a.json").write_text(json.dumps({"blandness": 0.1}), encoding="utf-8")
        (tmp_path / "b.json").write_text(json.dumps({"blandness": 0.9}), encoding="utf-8")
        response = servo.DryJudge().judge(
            JudgeRequest.of("compare", images=[bland, distinct])
        )
        assert response.parsed is not None
        assert response.parsed["winner"] == "B"
        assert set(response.parsed["per_axis"]) == set(RUBRIC_AXES)

    def test_the_stub_panel_has_enough_families_to_reach_a_majority(self) -> None:
        assert len({judge.family for judge in servo.dry_panel()}) >= 2

    def test_stub_families_are_not_model_families(self) -> None:
        assert not (set(servo.DRY_FAMILIES) & FAMILY_TOKENS)


# --------------------------------------------------------------------------- #
# The CLI                                                                      #
# --------------------------------------------------------------------------- #


def test_cli_wires_the_exemplar_store_to_out_dir_not_out_dir_slash_exemplars(
    tmp_path: Path,
) -> None:
    """Regression: ``cli.servo.build_stores`` used to construct
    ``JsonlExemplarStore(out_dir / "exemplars")``, but the store already appends
    ``EXEMPLAR_DIRNAME`` to whatever root it is given (see ``TestExemplarStore`` in
    ``test_evidence.py``). That stranded every exemplar at
    ``<out>/exemplars/exemplars/<name>/...`` instead of ``<out>/exemplars/<name>``.
    This calls the real construction site ``main()`` uses, so a revert of the fix
    fails this test.
    """
    out_dir = tmp_path / "servo-round0"
    out_dir.mkdir()
    stores = build_stores(out_dir)
    stores.exemplar.save_exemplar("hero-hero.claude.0", {"fragment.html": b"<p>hi</p>"}, {})
    saved = stores.exemplar.path_for("hero-hero.claude.0")
    assert saved == out_dir / EXEMPLAR_DIRNAME / "hero-hero.claude.0"
    assert saved.is_dir()


@pytest.mark.playwright
def test_cli_smoke_with_dry_judges(tmp_path: Path) -> None:
    """The documented acceptance command, end to end, with no model in the room."""
    out = tmp_path / "servo-round0"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ui_servo.cli.servo",
            "--candidates",
            str(CANDIDATES),
            "--part",
            "demo",
            "--round",
            "0",
            "--dry-judges",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr
    assert "round 0 / part demo" in completed.stdout
    assert "REJECTED demo.delta.2" in completed.stdout
    assert (out / servo.REPORT_FILENAME).is_file()
    payload = json.loads((out / servo.RESULT_FILENAME).read_text(encoding="utf-8"))
    assert [entry["variant_id"] for entry in payload["rejected"]] == ["demo.delta.2"]
    assert len(payload["ranked"]) == 2
    assert (out / servo.BLIND_DIRNAME).is_dir()
