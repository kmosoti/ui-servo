"""The panel, exercised without a model in the room.

Everything worth testing about a critic panel is a governance property, not a
model behaviour: that the family which wrote a candidate is not on the panel,
that a critic never sees the same pair twice, that an answer given in flipped
order comes back in the caller's frame, that a rubric-breaking verdict costs one
re-ask and then the vote, that three disagreeing families escalate instead of
being averaged, and that no prompt ever names a model family. All of those are
decidable against :class:`~ui_servo.ports.judge.ScriptedJudge` with canned JSON,
which is the point of the port.

The blindness test is the load-bearing one and it is written against the prompts
the judges actually received rather than against the prompt builder, because the
leak that matters is the one that survives composition -- a staged filename, a
note pasted in from a builder, a round id.

One live pairwise round sits at the bottom behind ``UI_SERVO_LIVE=1``: three real
families, two staged screenshots, the real contract as the bar. It is the only
test that would notice if a structured-output mode changed shape underneath the
schema.
"""

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from ui_servo.control.critique import (
    Bar,
    Candidate,
    CritiquePanel,
    build_prompt,
    reask_prompt,
    run_pairwise_round,
)
from ui_servo.domain import policy
from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.policy import (
    FAILURE_KINDS,
    RotationHistory,
    Vote,
    blindness_violations,
    derandomize,
    eligible_judges,
    enforce_blindness,
    flip_for,
    pair_key,
    panel_outcome,
    rotate,
    validate_verdict,
)
from ui_servo.domain.verdict import (
    CHOICES,
    RUBRIC_AXES,
    Finding,
    PairwiseVerdict,
    opposite,
    schema_axes,
    verdict_response_schema,
)
from ui_servo.ports.judge import RC_OK, RC_TIMEOUT, JudgeResponse, ScriptedJudge

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "direction" / "direction.toml"

PART_SPEC = "The masthead of a personal site: name, one line of positioning, three links."

BAR = Bar(
    summary="Deep warm dark, editorial serif display, molten amber accent. Reads like a page.",
    references=("linear.app (precision)", "rauno.me (restraint)"),
    anti_references=("stock-saas-landing", "default-shadcn"),
)


def payload(
    winner: str = "A",
    *,
    per_axis: Mapping[str, str] | None = None,
    findings: Iterable[Mapping[str, Any]] | None = None,
    confidence: float = 0.8,
    drop_selector: bool = False,
) -> dict[str, Any]:
    """A schema-shaped verdict, valid unless a test asks for it to be broken."""
    axes = dict(per_axis or {axis: winner for axis in RUBRIC_AXES})
    finding: dict[str, Any] = {
        "axis": "hierarchy",
        "selector": "header h1",
        "gap": "the name is set at body size, so nothing carries the page",
        "severity": "major",
    }
    if drop_selector:
        finding.pop("selector")
    return {
        "winner": winner,
        "per_axis": axes,
        "findings": list(findings) if findings is not None else [finding],
        "confidence": confidence,
    }


def answering(family: str, *payloads: Mapping[str, Any] | None) -> ScriptedJudge:
    """A judge that replies with each payload in turn; ``None`` means prose."""
    responses = tuple(
        JudgeResponse(
            family=family,
            raw="I have thoughts." if item is None else json.dumps(item),
            parsed=None if item is None else dict(item),
            rc=RC_OK,
        )
        for item in payloads
    )
    return ScriptedJudge(family=family, responses=responses)


def failing(family: str, rc: int = RC_TIMEOUT) -> ScriptedJudge:
    return ScriptedJudge(
        family=family,
        responses=(JudgeResponse.failure(family, rc=rc, raw="timed out"),),
    )


def candidates(tmp_path: Path | None = None) -> tuple[Candidate, Candidate]:
    """Two blind-staged candidates. The paths need not exist: nothing reads them."""
    root = tmp_path or Path("/nonexistent/staged")
    return (
        Candidate(ref="cand-7f3a", screenshot=root / "cand-7f3a.png", builder_family="alpha"),
        Candidate(ref="cand-91c4", screenshot=root / "cand-91c4.png", builder_family="beta"),
    )


def verdict(winner: str = "A", **kwargs: Any) -> PairwiseVerdict:
    parsed = validate_verdict(payload(winner, **kwargs))
    assert isinstance(parsed, PairwiseVerdict), parsed
    return parsed


class TestTheVerdictShape:
    def test_the_schema_tracks_the_rubric(self) -> None:
        assert schema_axes(verdict_response_schema()) == RUBRIC_AXES

    def test_the_schema_is_flat_enough_for_a_cli_structured_mode(self) -> None:
        text = json.dumps(verdict_response_schema())
        assert "$ref" not in text and "$defs" not in text

    def test_each_caller_gets_its_own_schema(self) -> None:
        first = verdict_response_schema()
        first["properties"]["winner"]["enum"] = ["A"]
        assert verdict_response_schema()["properties"]["winner"]["enum"] == list(CHOICES)

    def test_swapping_twice_is_the_identity(self) -> None:
        original = verdict("A")
        assert original.swapped().swapped() == original
        assert original.swapped().winner == "B"
        assert all(choice == "B" for choice in original.swapped().per_axis.values())

    def test_a_finding_must_cite_a_place(self) -> None:
        with pytest.raises(ValueError):
            Finding(axis="color", selector="   ", gap="too grey", severity="minor")
        with pytest.raises(ValueError):
            Finding(axis="color", selector="n/a", gap="too grey", severity="minor")

    def test_every_axis_must_be_answered(self) -> None:
        partial = payload("A", per_axis={"hierarchy": "A"})
        violations = validate_verdict(partial)
        assert isinstance(violations, list)
        assert any("per_axis" in message for message in violations)

    def test_a_verdict_is_immutable(self) -> None:
        held = verdict("B")
        with pytest.raises(ValueError):
            held.winner = "A"  # type: ignore[misc]

    def test_axes_favouring_reads_the_split(self) -> None:
        mixed = verdict(
            "A", per_axis={axis: ("A" if index % 2 else "B") for index, axis in enumerate(RUBRIC_AXES)}
        )
        assert set(mixed.axes_favouring("A")) | set(mixed.axes_favouring("B")) == set(RUBRIC_AXES)
        assert sum(mixed.axis_tally().values()) == len(RUBRIC_AXES)


class TestSelfPreference:
    def test_the_builder_family_cannot_judge_its_own_work(self) -> None:
        judges = [answering("alpha"), answering("beta"), answering("gamma")]
        eligible = eligible_judges("alpha", judges)
        assert [judge.family for judge in eligible] == ["beta", "gamma"]

    def test_both_authors_of_a_pair_are_excluded(self) -> None:
        judges = [answering(name) for name in ("alpha", "beta", "gamma")]
        assert [j.family for j in eligible_judges(["alpha", "beta"], judges)] == ["gamma"]

    def test_exclusion_ignores_case_and_padding(self) -> None:
        judges = [answering("Alpha"), answering("beta")]
        assert [j.family for j in eligible_judges("  ALPHA ", judges)] == ["beta"]

    def test_bare_family_names_work_as_well_as_ports(self) -> None:
        assert eligible_judges("alpha", ["alpha", "beta"]) == ["beta"]

    def test_no_builder_means_no_exclusion(self) -> None:
        judges = [answering("alpha"), answering("beta")]
        assert eligible_judges(None, judges) == judges

    def test_the_round_never_asks_a_candidate_author(self) -> None:
        judges = (answering("alpha", payload("A")), answering("beta", payload("A")), answering("gamma", payload("A")))
        a, b = candidates()
        result = run_pairwise_round(
            judges,
            round_id="r1",
            turn_id="t1",
            part_spec=PART_SPEC,
            bar=BAR,
            a=a,
            b=b,
        )
        assert [ask.family for ask in result.asks] == ["gamma"]
        assert not any(judge.calls for judge in judges if judge.family in {"alpha", "beta"})


class TestRotation:
    def test_rotation_is_deterministic_for_a_round(self) -> None:
        judges = ["alpha", "beta", "gamma"]
        assert rotate(judges, "r1") == rotate(judges, "r1")

    def test_rotation_moves_the_lead_across_rounds(self) -> None:
        judges = ["alpha", "beta", "gamma"]
        leaders = {rotate(judges, f"round-{index}")[0] for index in range(12)}
        assert len(leaders) > 1

    def test_rotation_keeps_the_whole_roster(self) -> None:
        judges = ["alpha", "beta", "gamma"]
        assert sorted(rotate(judges, "r1")) == sorted(judges)

    def test_a_pair_key_does_not_depend_on_order(self) -> None:
        assert pair_key("cand-a", "cand-b") == pair_key("cand-b", "cand-a")

    def test_a_pair_needs_two_distinct_candidates(self) -> None:
        with pytest.raises(ValueError):
            pair_key("cand-a", "cand-a")

    def test_a_family_never_judges_the_same_pair_twice(self) -> None:
        judges = ["alpha", "beta", "gamma"]
        pair = pair_key("cand-a", "cand-b")
        history = RotationHistory()
        asked: list[str] = []
        for index in range(3):
            panel = rotate(judges, f"round-{index}", pair=pair, history=history, panel_size=1)
            asked.extend(panel)
            history = history.remembering(panel, pair)
        assert sorted(asked) == sorted(judges)
        assert rotate(judges, "round-3", pair=pair, history=history) == []

    def test_history_is_per_pair(self) -> None:
        history = RotationHistory().remembering("alpha", pair_key("cand-a", "cand-b"))
        assert history.has_seen("alpha", pair_key("cand-b", "cand-a"))
        assert not history.has_seen("alpha", pair_key("cand-a", "cand-c"))
        assert not history.has_seen("beta", pair_key("cand-a", "cand-b"))

    def test_a_second_round_on_the_same_pair_has_nobody_left(self) -> None:
        judges = (answering("gamma", payload("A")), answering("delta", payload("A")))
        a, b = candidates()
        first = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert len(first.history) == 2
        second = run_pairwise_round(
            judges,
            round_id="r2",
            turn_id="t1",
            part_spec=PART_SPEC,
            bar=BAR,
            a=a,
            b=b,
            history=first.history,
        )
        assert second.asks == ()
        assert second.outcome.escalated

    def test_the_round_reports_who_saw_the_pair(self) -> None:
        judges = (answering("gamma", payload("A")), answering("delta", payload("B")))
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert set(result.history.families_for(result.pair)) == {"gamma", "delta"}


class TestOrderRandomisation:
    def test_the_flip_is_stable_for_a_round(self) -> None:
        pair = pair_key("cand-a", "cand-b")
        assert flip_for("r1", pair, "alpha") == flip_for("r1", pair, "alpha")

    def test_the_flip_differs_across_judges_or_rounds(self) -> None:
        pair = pair_key("cand-a", "cand-b")
        seen = {
            flip_for(f"round-{index}", pair, family)
            for index in range(8)
            for family in ("alpha", "beta", "gamma")
        }
        assert seen == {True, False}

    def test_derandomising_a_flipped_answer_restores_the_callers_frame(self) -> None:
        answered = verdict("A")
        restored = derandomize(answered, True)
        assert restored.winner == "B"
        assert restored.per_axis == {axis: "B" for axis in RUBRIC_AXES}
        assert restored.findings == answered.findings

    def test_derandomising_an_unflipped_answer_changes_nothing(self) -> None:
        answered = verdict("A")
        assert derandomize(answered, False) is answered

    def test_the_round_returns_verdicts_in_the_callers_frame(self) -> None:
        judges = tuple(
            answering(name, payload("A")) for name in ("gamma", "delta", "epsilon", "zeta")
        )
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r7", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert result.asks, "expected an unowned panel to be asked"
        for vote, ask in zip(result.votes, result.asks, strict=True):
            expected = "B" if ask.was_flipped else "A"
            assert vote.verdict.winner == expected
            assert vote.verdict.per_axis == {axis: expected for axis in RUBRIC_AXES}

    def test_the_flip_is_recorded_beside_the_verdict(self) -> None:
        judges = (answering("gamma", payload("A")),)
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r7", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        recorded = [s for s in result.signals if s.kind == "panel-verdict"]
        assert recorded[0].payload["was_flipped"] == result.asks[0].was_flipped


class TestReAsk:
    def test_a_finding_without_a_selector_is_a_violation(self) -> None:
        violations = validate_verdict(payload("A", drop_selector=True))
        assert isinstance(violations, list)
        assert any("selector" in message for message in violations)

    def test_prose_instead_of_json_is_a_violation(self) -> None:
        violations = validate_verdict(None)
        assert isinstance(violations, list) and violations

    def test_one_re_ask_then_the_vote_is_lost(self) -> None:
        judge = answering("gamma", payload("A", drop_selector=True), payload("B", drop_selector=True))
        a, b = candidates()
        result = run_pairwise_round(
            (judge,), round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert len(judge.calls) == 2, "exactly one re-ask, never a loop"
        assert result.votes == ()
        assert [rejection.family for rejection in result.rejections] == ["gamma"]
        assert "rejected twice" in result.rejections[0].reason
        kinds = [signal.kind for signal in result.signals]
        assert kinds.count("panel-reask") == 1
        assert "judge-error" in kinds

    def test_a_re_ask_that_lands_is_counted(self) -> None:
        judge = answering("gamma", payload("A", drop_selector=True), payload("A"))
        a, b = candidates()
        result = run_pairwise_round(
            (judge,), round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert len(judge.calls) == 2
        assert len(result.votes) == 1
        assert [ask.attempt for ask in result.asks] == [1, 2]

    def test_the_re_ask_quotes_the_violated_rule(self) -> None:
        judge = answering("gamma", payload("A", drop_selector=True), payload("A"))
        a, b = candidates()
        run_pairwise_round(
            (judge,), round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert "selector" in judge.calls[1].prompt
        assert PART_SPEC in judge.calls[1].prompt

    def test_a_transport_failure_is_not_re_asked(self) -> None:
        judge = failing("gamma")
        a, b = candidates()
        result = run_pairwise_round(
            (judge,), round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert len(judge.calls) == 1, "a timeout is not a rubric problem; re-asking wastes the round"
        assert result.rejections[0].rc == RC_TIMEOUT
        assert result.votes == ()


class TestPanelOutcome:
    def test_two_of_three_wins(self) -> None:
        result = panel_outcome(
            [
                Vote("alpha", verdict("A")),
                Vote("beta", verdict("A")),
                Vote("gamma", verdict("B")),
            ]
        )
        assert result.status == "winner"
        assert result.winner == "A"
        assert result and result.tally == {"A": 2, "B": 1, "tie": 0}

    def test_three_way_disagreement_escalates(self) -> None:
        result = panel_outcome(
            [
                Vote("alpha", verdict("A")),
                Vote("beta", verdict("B")),
                Vote("gamma", verdict("tie")),
            ]
        )
        assert result.escalated and result.winner is None
        assert "no majority" in result.detail

    def test_a_majority_tie_escalates_rather_than_winning(self) -> None:
        result = panel_outcome(
            [Vote("alpha", verdict("tie")), Vote("beta", verdict("tie")), Vote("gamma", verdict("A"))]
        )
        assert result.escalated
        assert "tie" in result.detail

    def test_one_family_is_not_a_panel(self) -> None:
        assert panel_outcome([Vote("alpha", verdict("A"))]).escalated
        assert panel_outcome([]).escalated

    def test_two_calls_to_one_family_are_one_vote(self) -> None:
        result = panel_outcome(
            [
                Vote("alpha", verdict("A")),
                Vote("alpha", verdict("A")),
                Vote("beta", verdict("B")),
            ]
        )
        assert result.escalated, "a duplicated family must not outvote the rest of the panel"
        assert "repeat families" in result.detail

    def test_unanimity_of_two_wins(self) -> None:
        result = panel_outcome([Vote("alpha", verdict("B")), Vote("beta", verdict("B"))])
        assert result.winner == "B"

    def test_the_outcome_serialises_for_the_stock(self) -> None:
        result = panel_outcome([Vote("alpha", verdict("A")), Vote("beta", verdict("A"))])
        assert json.loads(json.dumps(result.as_mapping()))["winner"] == "A"

    def test_a_round_reports_the_majority_of_the_eligible_panel(self) -> None:
        judges = tuple(
            answering(name, payload("A"))
            for name in ("alpha", "beta", "gamma", "delta", "epsilon")
        )
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert len(result.votes) == 3, "the two candidate authors sit the round out"
        assert result.outcome.status == "winner"
        assert result.outcome.voting_families == result.families


class TestBlindness:
    def test_a_family_token_anywhere_is_a_violation(self) -> None:
        assert blindness_violations("built with Claude")
        assert blindness_violations("/tmp/stage/codex-a.png")
        assert blindness_violations("compare GPT-5 output")
        assert not blindness_violations("/tmp/stage/cand-7f3a.png")

    def test_ordinary_critique_prose_is_not_flagged(self) -> None:
        assert not blindness_violations(
            "The heading hierarchy is flat and the accent colour is used for three "
            "different meanings; density is cramped at the meta line."
        )

    def test_local_leaks_can_be_added(self) -> None:
        assert blindness_violations("built by builder-seven", extra_tokens=["builder-seven"])

    def test_enforcement_refuses_rather_than_warns(self) -> None:
        with pytest.raises(policy.BlindnessError):
            enforce_blindness("this fragment came from claude")

    def test_no_prompt_in_a_round_names_a_family(self) -> None:
        judges = tuple(
            answering(name, payload("A", drop_selector=True), payload("A"))
            for name in ("gamma", "delta", "epsilon")
        )
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        sent = [request.prompt for judge in judges for request in judge.calls]
        assert sent, "expected the panel to have asked somebody"
        assert sorted(sent) == sorted(result.prompts), "the round must report every prompt it sent"
        for prompt in sent:
            assert blindness_violations(prompt) == ()

    def test_a_leaky_staged_path_stops_the_round(self) -> None:
        leaky = Candidate(ref="cand-7f3a", screenshot=Path("/tmp/stage/claude-run/a.png"))
        _, b = candidates()
        with pytest.raises(policy.BlindnessError):
            run_pairwise_round(
                (answering("gamma", payload("A")),),
                round_id="r1",
                turn_id="t1",
                part_spec=PART_SPEC,
                bar=BAR,
                a=leaky,
                b=b,
            )

    def test_a_leaky_note_stops_the_round(self) -> None:
        a, b = candidates()
        leaky = Candidate(ref=a.ref, screenshot=a.screenshot, notes="generated by gemini, second pass")
        with pytest.raises(policy.BlindnessError):
            run_pairwise_round(
                (answering("gamma", payload("A")),),
                round_id="r1",
                turn_id="t1",
                part_spec=PART_SPEC,
                bar=BAR,
                a=leaky,
                b=b,
            )

    def test_a_candidate_has_nowhere_to_put_builder_reasoning(self) -> None:
        assert set(Candidate.__slots__) == {"ref", "screenshot", "notes", "builder_family"}

    def test_the_builder_family_never_reaches_the_prompt(self) -> None:
        a, b = candidates()
        prompt = build_prompt(PART_SPEC, BAR, a, b)
        assert "alpha" not in prompt and "beta" not in prompt

    def test_the_re_ask_stays_blind(self) -> None:
        assert blindness_violations(reask_prompt("original", ["cite a selector"])) == ()


class TestThePromptItself:
    def test_it_carries_the_spec_the_bar_and_both_screenshots(self) -> None:
        a, b = candidates()
        prompt = build_prompt(PART_SPEC, BAR, a, b)
        assert PART_SPEC in prompt
        assert BAR.summary in prompt
        assert "linear.app (precision)" in prompt
        assert "stock-saas-landing" in prompt
        assert str(a.screenshot) in prompt and str(b.screenshot) in prompt
        assert all(axis in prompt for axis in RUBRIC_AXES)

    def test_the_labels_follow_presentation_order(self) -> None:
        a, b = candidates()
        prompt = build_prompt(PART_SPEC, BAR, b, a)
        assert prompt.index("CANDIDATE A") < prompt.index("CANDIDATE B")
        assert prompt.index(str(b.screenshot)) < prompt.index(str(a.screenshot))

    def test_the_round_asks_for_the_verdict_schema(self) -> None:
        judge = answering("gamma", payload("A"))
        a, b = candidates()
        run_pairwise_round(
            (judge,), round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        request = judge.calls[0]
        assert schema_axes(request.response_schema or {}) == RUBRIC_AXES
        assert len(request.image_paths) == 2

    def test_nothing_on_disk_is_touched(self) -> None:
        a, b = candidates(Path("/nonexistent/staged"))
        result = run_pairwise_round(
            (answering("gamma", payload("A")), answering("delta", payload("A"))),
            round_id="r1",
            turn_id="t1",
            part_spec=PART_SPEC,
            bar=BAR,
            a=a,
            b=b,
        )
        assert result.votes, "the panel must not depend on reading the staged files itself"

    def test_a_bar_can_be_built_from_the_contract(self) -> None:
        contract = DirectionContract.from_toml(CONTRACT_PATH.read_text(encoding="utf-8"))
        bar = Bar.from_contract(contract)
        assert contract.meta.name in bar.summary
        assert any("linear.app" in reference for reference in bar.references)
        assert bar.anti_references
        assert blindness_violations(bar.presented()) == ()


class TestTheEvidenceItLeaves:
    def test_every_signal_is_a_judge_signal_for_the_turn(self, tmp_path: Path) -> None:
        judges = (answering("gamma", payload("A")), answering("delta", payload("A")))
        a, b = candidates(tmp_path)
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="turn-9", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert result.signals
        assert {signal.source for signal in result.signals} == {"judge"}
        assert {signal.turn_id for signal in result.signals} == {"turn-9"}
        assert all(signal.payload["round_id"] == "r1" for signal in result.signals)

    def test_the_outcome_is_recorded_last(self) -> None:
        judges = (answering("gamma", payload("A")), answering("delta", payload("A")))
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        assert result.signals[-1].kind == "panel-outcome"
        assert result.signals[-1].payload["candidates"] == {"A": a.ref, "B": b.ref}

    def test_signals_reach_a_store_as_they_happen(self, tmp_path: Path) -> None:
        pytest.importorskip("orjson")
        from ui_servo.adapters.jsonl_store import JsonlEvidenceStore

        store = JsonlEvidenceStore(root=tmp_path)
        panel = CritiquePanel(
            judges=(answering("gamma", payload("A")), answering("delta", payload("A"))),
            store=store,
        )
        a, b = candidates(tmp_path)
        result = panel.run_round(
            round_id="r1", turn_id="turn-9", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        stored = store.signals_for_turn("turn-9")
        assert len(stored) == len(result.signals)
        assert [signal.kind for signal in stored] == [signal.kind for signal in result.signals]

    def test_a_recorded_verdict_round_trips(self) -> None:
        judges = (answering("gamma", payload("A")),)
        a, b = candidates()
        result = run_pairwise_round(
            judges, round_id="r1", turn_id="t1", part_spec=PART_SPEC, bar=BAR, a=a, b=b
        )
        recorded = next(s for s in result.signals if s.kind == "panel-verdict")
        assert PairwiseVerdict.from_mapping(recorded.payload["verdict"]) == result.votes[0].verdict


class TestFailureKinds:
    def test_the_canonical_set_covers_the_deterministic_departures(self) -> None:
        assert {
            "swap-error",
            "js-error",
            "csp",
            "axe-violation",
            "motion-violation",
            "unknown-class",
            "overflow",
            "judge-error",
        } <= FAILURE_KINDS

    def test_measurements_are_not_failures(self) -> None:
        assert not FAILURE_KINDS & {
            "swap-ok",
            "animation",
            "longtask",
            "interaction",
            "element-timing",
            "screenshot",
            "aria-snapshot",
            "pixel-diff",
            "frame-timing",
            "reduced-motion",
            "judge-response",
        }

    def test_it_classifies_only_kinds_something_actually_emits(self) -> None:
        pytest.importorskip("fastapi")
        from ui_servo.adapters.beacon_ingest import KNOWN_PROBE_KINDS
        from ui_servo.ports.sanitizer import ViolationKind

        emitted = (
            set(KNOWN_PROBE_KINDS)
            | {kind.value for kind in ViolationKind}
            | {"axe-violation", "judge-error", "judge-response"}
        )
        assert FAILURE_KINDS <= emitted

    def test_the_policy_default_partitions_a_stock(self) -> None:
        from ui_servo.domain.evidence import Signal

        signals = [
            Signal(span_id="s", turn_id="t", source="probe", kind=kind, ts="2026-08-05T00:00:00Z")
            for kind in ("swap-ok", "overflow", "animation", "js-error")
        ]
        assert [signal.kind for signal in policy.failing_signals(signals)] == ["overflow", "js-error"]


class TestOpposite:
    @pytest.mark.parametrize("choice", CHOICES)
    def test_opposite_is_an_involution(self, choice: str) -> None:
        assert opposite(opposite(choice)) == choice


def _stage(directory: Path, ref: str, *, on_contract: bool) -> Path:
    """Render one candidate screenshot under an opaque name.

    Blind staging in miniature: the file says nothing about who made it, which is
    the caller's half of the blindness contract that
    :mod:`ui_servo.control.critique` cannot enforce for anybody.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1000, 640), "#17110c" if on_contract else "#f4f5f7")
    draw = ImageDraw.Draw(image)
    big = ImageFont.load_default(size=64 if on_contract else 22)
    small = ImageFont.load_default(size=20 if on_contract else 15)
    ink = "#f6ece0" if on_contract else "#333333"
    accent = "#ffb347" if on_contract else "#0d6efd"

    if on_contract:
        draw.text((80, 120), "Kennedy Mosoti", font=big, fill=ink)
        draw.line((80, 210, 320, 210), fill=accent, width=4)
        draw.text((80, 250), "Systems, control loops, and interfaces that stay out of the way.", font=small, fill=ink)
        for index, label in enumerate(("writing", "work", "contact")):
            draw.text((80 + index * 170, 340), label, font=small, fill=accent)
    else:
        draw.rectangle((0, 0, 1000, 56), fill="#ffffff", outline="#dee2e6")
        draw.text((24, 20), "Kennedy Mosoti", font=big, fill=ink)
        draw.rectangle((300, 140, 700, 320), fill="#ffffff", outline="#dee2e6")
        draw.text((330, 170), "Welcome to my website", font=big, fill=ink)
        draw.text((330, 210), "I build things with modern technologies.", font=small, fill="#6c757d")
        draw.rounded_rectangle((330, 250, 470, 290), radius=6, fill=accent)
        draw.text((360, 262), "Get started", font=small, fill="#ffffff")

    path = directory / f"{ref}.png"
    image.save(path)
    return path


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("UI_SERVO_LIVE") != "1", reason="live panel round; set UI_SERVO_LIVE=1"
)
def test_live_pairwise_round(tmp_path: Path) -> None:
    """One real round: three families, two staged screenshots, the real bar.

    Asserts the protocol, never the taste. Which candidate wins is the models'
    business; that at least two families returned a schema-valid, selector-citing
    verdict through the blind prompt is this project's.
    """
    from ui_servo.adapters.cli_judges import ClaudeJudge, CodexJudge, GeminiJudge

    a = Candidate(ref="cand-7f3a", screenshot=_stage(tmp_path, "cand-7f3a", on_contract=True))
    b = Candidate(ref="cand-91c4", screenshot=_stage(tmp_path, "cand-91c4", on_contract=False))
    contract = DirectionContract.from_toml(CONTRACT_PATH.read_text(encoding="utf-8"))
    panel = CritiquePanel(judges=(ClaudeJudge(), CodexJudge(), GeminiJudge()), timeout_s=300)

    result = panel.run_round(
        round_id="live-1",
        turn_id="live",
        part_spec=PART_SPEC,
        bar=Bar.from_contract(contract),
        a=a,
        b=b,
    )

    print(f"\nlive panel: {result.outcome.status} winner={result.outcome.winner}")
    print(f"detail: {result.outcome.detail}")
    for vote in result.votes:
        cited = ", ".join(vote.verdict.selectors[:3])
        print(f"  {vote.family}: {vote.verdict.winner} conf={vote.verdict.confidence} [{cited}]")
    for rejection in result.rejections:
        print(f"  {rejection.family}: REJECTED {rejection.reason[:160]}")

    assert all(blindness_violations(prompt) == () for prompt in result.prompts)
    assert result.votes, f"no family produced a usable verdict: {result.rejections}"
    for vote in result.votes:
        assert all(finding.selector for finding in vote.verdict.findings)
        assert set(vote.verdict.per_axis) == set(RUBRIC_AXES)
