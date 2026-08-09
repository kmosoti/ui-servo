"""Calibration for the stock: the join must be honest and the store must not lose.

Two properties carry most of the weight here. The join is only useful if order
and grouping survive it, because sequence is evidence. The store is only a stock
if what goes in comes out -- every record, in order, untruncated -- including
after a crash mid-write and under concurrent sensors.
"""

import ast
import math
import random
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ui_servo.adapters.jsonl_store import (
    EVIDENCE_DIRNAME,
    EXEMPLAR_DIRNAME,
    SEQUENCE_FIELD,
    JsonlEvidenceStore,
    JsonlExemplarStore,
)
from ui_servo.domain.evidence import (
    Signal,
    SpanEvidence,
    TurnEvidence,
    by_kind,
    by_source,
    chronological,
    failures_only,
    group_by_kind,
    join_spans,
    kind_counts,
    spans_in_turn,
    spans_of,
)
from ui_servo.ports.store import (
    META_FILENAME,
    EvidenceStorePort,
    Exemplar,
    ExemplarStorePort,
    StoreError,
    StoreWriteError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

DEPARTURE_KINDS: frozenset[str] = frozenset(
    {"swap-error", "js-error", "unknown-class", "overflow", "motion-violation", "axe-violation"}
)
"""A caller's failure vocabulary, supplied by the caller.

Parked in the test suite deliberately. The domain must not ship a default set --
which kinds count as failures is policy, and policy belongs to the gates and the
rubric (U9/U10), not to the record of what a sensor saw.
"""


def signal(
    kind: str = "swap-ok",
    *,
    span: str = "s1",
    turn: str = "t1",
    source: str = "probe",
    at: int = 0,
    **payload: Any,
) -> Signal:
    return Signal(
        span_id=span,
        turn_id=turn,
        source=source,  # type: ignore[arg-type]
        kind=kind,
        ts=(EPOCH + timedelta(milliseconds=at)).isoformat(),
        payload=payload,
    )


class TestSignal:
    def test_is_frozen(self) -> None:
        one = signal()
        with pytest.raises(ValidationError):
            one.kind = "other"  # type: ignore[misc]

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            signal(source="vibes")

    def test_unknown_kind_accepted(self) -> None:
        assert signal("sensor-invented-this").kind == "sensor-invented-this"

    def test_non_iso_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Signal(span_id="s", turn_id="t", source="probe", kind="k", ts="yesterday")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Signal.model_validate(
                {
                    "span_id": "s",
                    "turn_id": "t",
                    "source": "probe",
                    "kind": "k",
                    "ts": EPOCH.isoformat(),
                    "severity": "major",
                }
            )

    def test_empty_identifiers_rejected(self) -> None:
        with pytest.raises(ValidationError):
            signal(span="")

    def test_observed_at_parses(self) -> None:
        assert signal(at=250).observed_at == EPOCH + timedelta(milliseconds=250)

    def test_payload_defaults_to_empty(self) -> None:
        assert signal().payload == {}


class TestJoin:
    def test_groups_by_span(self) -> None:
        signals = [
            signal("swap-ok", span="a"),
            signal("overflow", span="b"),
            signal("animation", span="a"),
        ]
        joined = join_spans(signals)
        assert set(joined) == {("t1", "a"), ("t1", "b")}
        assert [one.kind for one in joined["t1", "a"]] == ["swap-ok", "animation"]
        assert joined["t1", "b"].turn_id == "t1"

    def test_one_span_id_in_two_turns_stays_two_renderings(self) -> None:
        signals = [
            signal("overflow", span="hero", turn="t1"),
            signal("swap-ok", span="hero", turn="t2"),
            signal("animation", span="hero", turn="t1"),
        ]
        joined = join_spans(signals)
        assert set(joined) == {("t1", "hero"), ("t2", "hero")}
        assert joined["t1", "hero"].kinds == {"overflow", "animation"}
        assert joined["t2", "hero"].kinds == {"swap-ok"}
        assert joined["t2", "hero"].turn_id == "t2"

    def test_a_fixed_regression_does_not_contaminate_the_later_turn(self) -> None:
        joined = join_spans(
            [signal("overflow", span="hero", turn="t1"), signal("swap-ok", span="hero", turn="t2")]
        )
        assert joined["t2", "hero"].has_failure(DEPARTURE_KINDS) is False
        assert joined["t1", "hero"].has_failure(DEPARTURE_KINDS) is True

    def test_spans_in_turn_keys_by_span_id(self) -> None:
        signals = [signal(span="a", turn="t1"), signal(span="a", turn="t2")]
        assert set(spans_in_turn(signals, "t2")) == {"a"}
        assert spans_in_turn(signals, "t2")["a"].turn_id == "t2"

    def test_preserves_first_seen_span_order(self) -> None:
        joined = join_spans([signal(span="z"), signal(span="a"), signal(span="z")])
        assert list(joined) == [("t1", "z"), ("t1", "a")]

    def test_preserves_signal_order_within_a_span(self) -> None:
        signals = [signal("k", at=index, i=index) for index in range(20)]
        joined = join_spans(reversed(signals))
        assert [one.payload["i"] for one in joined["t1", "s1"]] == list(reversed(range(20)))

    def test_empty_input_joins_to_nothing(self) -> None:
        assert join_spans([]) == {}

    def test_span_evidence_reports_kinds_and_sources(self) -> None:
        joined = join_spans([signal("overflow"), signal("axe-violation", source="harness")])
        span = joined["t1", "s1"]
        assert span.kinds == {"overflow", "axe-violation"}
        assert span.sources == {"probe", "harness"}
        assert span.key == ("t1", "s1")
        assert len(span) == 2

    def test_span_evidence_is_frozen(self) -> None:
        span = SpanEvidence(span_id="s", turn_id="t", signals=())
        with pytest.raises(AttributeError):
            span.span_id = "other"  # type: ignore[misc]


class TestFilters:
    def test_by_kind_accepts_one_or_many(self) -> None:
        signals = [signal("overflow"), signal("animation"), signal("swap-ok")]
        assert [one.kind for one in by_kind(signals, "overflow")] == ["overflow"]
        assert len(by_kind(signals, {"overflow", "swap-ok"})) == 2

    def test_by_source_separates_sensor_families(self) -> None:
        signals = [signal(source="probe"), signal(source="harness"), signal(source="judge")]
        assert len(by_source(signals, "harness")) == 1
        assert len(by_source(signals, ("probe", "judge"))) == 2

    def test_failures_only_selects_the_callers_kinds(self) -> None:
        signals = [signal("swap-ok"), signal("overflow"), signal("animation"), signal("js-error")]
        chosen = failures_only(signals, failure_kinds=DEPARTURE_KINDS)
        assert [one.kind for one in chosen] == ["overflow", "js-error"]

    def test_the_domain_ships_no_failure_classification(self) -> None:
        from ui_servo.domain import evidence

        constants = {
            name: value
            for name, value in vars(evidence).items()
            if isinstance(value, (set, frozenset))
        }
        assert not [
            name for name, value in constants.items() if value & DEPARTURE_KINDS
        ], "the domain must not ship an opinion about which kinds are failures"
        assert not hasattr(Signal, "is_failure")

    def test_failure_kinds_must_be_supplied(self) -> None:
        with pytest.raises(TypeError):
            failures_only([signal("overflow")])  # type: ignore[call-arg]

    def test_the_same_signals_partition_differently_under_different_policy(self) -> None:
        signals = [signal("verdict"), signal("overflow")]
        strict = failures_only(signals, failure_kinds={"verdict"})
        lenient = failures_only(signals, failure_kinds={"overflow"})
        assert [one.kind for one in strict] == ["verdict"]
        assert [one.kind for one in lenient] == ["overflow"]

    def test_unlisted_kinds_are_evidence_not_failures(self) -> None:
        assert failures_only([signal("some-future-kind")], failure_kinds=DEPARTURE_KINDS) == ()

    def test_group_by_kind_and_counts(self) -> None:
        signals = [signal("overflow"), signal("overflow"), signal("swap-ok")]
        assert kind_counts(signals) == {"overflow": 2, "swap-ok": 1}
        assert len(group_by_kind(signals)["overflow"]) == 2

    def test_chronological_is_a_stable_sort(self) -> None:
        late, early, tie = signal("a", at=90), signal("b", at=10), signal("c", at=90)
        assert [one.kind for one in chronological([late, early, tie])] == ["b", "a", "c"]

    def test_spans_of_is_first_seen_order(self) -> None:
        assert spans_of([signal(span="b"), signal(span="a"), signal(span="b")]) == (
            ("t1", "b"),
            ("t1", "a"),
        )


class TestTurnEvidence:
    def test_from_signals_joins_spans(self) -> None:
        turn = TurnEvidence.from_signals(
            [signal(span="a"), signal("overflow", span="b"), signal(span="a")]
        )
        assert turn.turn_id == "t1"
        assert turn.span_ids == ("a", "b")
        assert len(turn["a"]) == 2
        assert len(turn.signals) == 3

    def test_mixed_turns_are_an_error(self) -> None:
        with pytest.raises(ValueError, match="several turns"):
            TurnEvidence.from_signals([signal(turn="t1"), signal(turn="t2")])

    def test_declared_turn_must_match(self) -> None:
        with pytest.raises(ValueError, match="not 't9'"):
            TurnEvidence.from_signals([signal(turn="t1")], turn_id="t9")

    def test_empty_needs_a_declared_turn(self) -> None:
        with pytest.raises(ValueError, match="cannot infer"):
            TurnEvidence.from_signals([])
        assert TurnEvidence.from_signals([], turn_id="t7").spans == ()

    def test_unknown_span_raises_keyerror(self) -> None:
        turn = TurnEvidence.from_signals([signal(span="a")])
        with pytest.raises(KeyError, match="unknown span"):
            turn["nope"]

    def test_failing_spans_are_the_ones_with_the_callers_departures(self) -> None:
        turn = TurnEvidence.from_signals(
            [signal(span="ok"), signal("overflow", span="bad"), signal("animation", span="bad")]
        )
        assert [span.span_id for span in turn.failing_spans(DEPARTURE_KINDS)] == ["bad"]
        assert turn["bad"].failures(DEPARTURE_KINDS)[0].kind == "overflow"
        assert not turn["ok"].has_failure(DEPARTURE_KINDS)
        assert turn.failing_spans({"animation"})[0].span_id == "bad"


class TestEvidenceStoreRoundTrip:
    def test_satisfies_the_port(self, tmp_path: Path) -> None:
        assert isinstance(JsonlEvidenceStore(tmp_path), EvidenceStorePort)

    def test_one_file_per_turn(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append_many([signal(turn="t1"), signal(turn="t2"), signal(turn="t1")])
        directory = tmp_path / EVIDENCE_DIRNAME
        assert sorted(path.name for path in directory.glob("*.jsonl")) == ["t1.jsonl", "t2.jsonl"]
        assert (directory / "t1.jsonl").read_bytes().count(b"\n") == 2
        assert store.turn_ids() == ("t1", "t2")

    def test_round_trip_preserves_every_field(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        original = signal("axe-violation", source="harness", at=17, nodes=[{"target": [".a"]}])
        store.append(original)
        assert store.signals_for_turn("t1") == (original,)

    def test_payload_is_not_truncated(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        payload = {
            "html": "<section>" + "x" * 50_000 + "</section>",
            "nested": {"deep": [{"selector": "#a > .b:hover", "unicode": "café — ✓"}]},
            "numbers": [1, 2.5, -3],
            "null": None,
        }
        store.append(signal("swap-error", **payload))
        assert store.signals_for_turn("t1")[0].payload == payload

    def test_signals_for_span(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append_many(
            [signal(span="a"), signal(span="b", turn="t2"), signal(span="a", turn="t2")]
        )
        assert len(store.signals_for_span("a")) == 2
        assert len(store.signals_for_span("a", turn_id="t1")) == 1
        assert store.signals_for_span("a", turn_id="t2")[0].turn_id == "t2"

    def test_unknown_turn_reads_empty(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        assert store.signals_for_turn("never-written") == ()
        assert store.signals_for_span("never-seen") == ()

    def test_append_many_reports_how_many_landed(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        assert store.append_many([signal(), signal(turn="t2")]) == 2
        assert store.append_many([]) == 0

    def test_reopening_the_store_appends_rather_than_truncates(self, tmp_path: Path) -> None:
        JsonlEvidenceStore(tmp_path).append(signal("first"))
        JsonlEvidenceStore(tmp_path).append(signal("second"))
        assert [one.kind for one in JsonlEvidenceStore(tmp_path).signals_for_turn("t1")] == [
            "first",
            "second",
        ]

    def test_path_traversal_in_a_turn_id_is_refused(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError, match="safe path component"):
            store.append(signal(turn="../../etc/passwd"))
        assert not (tmp_path.parent / "etc").exists()

    def test_paths_and_sets_in_a_payload_survive_as_json(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("screenshot", path=tmp_path / "shot.png", tags={"wcag2aa"}))
        stored = store.signals_for_turn("t1")[0].payload
        assert stored["path"] == str(tmp_path / "shot.png")
        assert stored["tags"] == ["wcag2aa"]

    def test_unserialisable_payload_fails_loudly(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError, match="cannot serialise"):
            store.append(signal("weird", thing=object()))

    def test_a_failed_batch_writes_nothing(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError):
            store.append_many([signal("fine"), signal("weird", thing=object())])
        assert store.signals_for_turn("t1") == ()

    def test_a_poisoned_later_turn_leaves_the_earlier_turn_untouched(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError, match="cannot serialise"):
            store.append_many(
                [
                    signal("fine", turn="t1"),
                    signal("fine", turn="t2"),
                    signal("weird", turn="t2", thing=object()),
                ]
            )
        assert store.turn_ids() == ()
        assert store.signals_for_turn("t1") == ()
        assert store.signals_for_turn("t2") == ()

    def test_an_unwritable_turn_is_refused_before_any_turn_is_written(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError, match="safe path component"):
            store.append_many([signal(turn="t1"), signal(turn="../escape")])
        assert store.signals_for_turn("t1") == ()

    def test_partial_write_failure_names_the_turns_that_landed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = JsonlEvidenceStore(tmp_path)
        original = JsonlEvidenceStore._append_blob

        def fail_on_second(self: JsonlEvidenceStore, path: Path, blob: bytes) -> None:
            if path.stem == "t2":
                raise StoreError("disk went away")
            original(self, path, blob)

        monkeypatch.setattr(JsonlEvidenceStore, "_append_blob", fail_on_second)
        with pytest.raises(StoreWriteError) as caught:
            store.append_many([signal(turn="t1"), signal(turn="t2")])
        assert caught.value.turns_written == ("t1",)
        assert isinstance(caught.value, StoreError)


class TestLosslessPayloads:
    """A stock that quietly rewrites what a sensor said is an interpretation."""

    def test_invalid_utf8_bytes_survive_exactly(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        blob = b"\x89PNG\r\n\x1a\n\xff\xfe not utf-8 \x00"
        store.append(signal("screenshot", raw=blob, nested={"chunks": [blob, b""]}))
        stored = store.signals_for_turn("t1")[0].payload
        assert stored["raw"] == blob
        assert stored["nested"]["chunks"] == [blob, b""]

    def test_non_finite_floats_survive_exactly(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("metric", ratio=math.inf, drift=-math.inf, score=math.nan, ok=1.5))
        stored = store.signals_for_turn("t1")[0].payload
        assert stored["ratio"] == math.inf
        assert stored["drift"] == -math.inf
        assert math.isnan(stored["score"])
        assert stored["ok"] == 1.5

    def test_a_payload_that_looks_like_the_encoding_survives(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        payload = {"__ui_servo__": "bytes", "value": "not really base64"}
        store.append(signal("adversarial", spoof=payload))
        assert store.signals_for_turn("t1")[0].payload["spoof"] == payload

    def test_booleans_are_not_mistaken_for_numbers(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("flags", yes=True, no=False))
        stored = store.signals_for_turn("t1")[0].payload
        assert stored["yes"] is True and stored["no"] is False

    def test_non_string_mapping_keys_are_refused_not_renamed(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        with pytest.raises(StoreError, match="cannot serialise"):
            store.append(signal("histogram", bins={1: "a", 2: "b"}))
        assert store.signals_for_turn("t1") == ()


class TestAdmissionOrder:
    """Order across turns is a stored fact, not an accident of file names."""

    def test_records_carry_an_admission_number(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append_many([signal("a"), signal("b")])
        lines = (tmp_path / EVIDENCE_DIRNAME / "t1.jsonl").read_text(encoding="utf-8").splitlines()
        assert [line.startswith(f'{{"{SEQUENCE_FIELD}":') for line in lines] == [True, True]

    def test_span_history_is_write_order_not_turn_name_order(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("first", span="hero", turn="t9"))
        store.append(signal("second", span="hero", turn="t0"))
        store.append(signal("third", span="hero", turn="t9"))
        assert [one.kind for one in store.signals_for_span("hero")] == [
            "first",
            "second",
            "third",
        ]

    def test_ordering_survives_a_restart(self, tmp_path: Path) -> None:
        JsonlEvidenceStore(tmp_path).append(signal("first", span="hero", turn="t9"))
        JsonlEvidenceStore(tmp_path).append(signal("second", span="hero", turn="t0"))
        assert [one.kind for one in JsonlEvidenceStore(tmp_path).signals_for_span("hero")] == [
            "first",
            "second",
        ]

    def test_a_lost_counter_is_rebuilt_from_the_stock(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("first", span="hero", turn="t9"))
        (tmp_path / EVIDENCE_DIRNAME / ".seq").unlink()
        store.append(signal("second", span="hero", turn="t0"))
        assert [one.kind for one in store.signals_for_span("hero")] == ["first", "second"]

    def test_a_corrupt_counter_is_rebuilt_from_the_stock(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("first", span="hero", turn="t9"))
        (tmp_path / EVIDENCE_DIRNAME / ".seq").write_text("garbage", encoding="utf-8")
        store.append(signal("second", span="hero", turn="t0"))
        assert [one.kind for one in store.signals_for_span("hero")] == ["first", "second"]

    def test_lock_and_counter_files_are_not_turns(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal())
        assert store.turn_ids() == ("t1",)


class TestAppendOnlyProperty:
    """Appending N signals then reading yields N, in order, whatever the batching."""

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_n_in_n_out_in_order(self, tmp_path: Path, seed: int) -> None:
        rng = random.Random(seed)
        store = JsonlEvidenceStore(tmp_path / str(seed))
        written = [
            signal(
                rng.choice(["swap-ok", "overflow", "animation", "axe-violation"]),
                span=f"s{rng.randrange(4)}",
                source=rng.choice(["probe", "harness", "gate", "judge"]),
                at=index,
                i=index,
            )
            for index in range(250)
        ]
        cursor = 0
        while cursor < len(written):
            size = rng.randrange(1, 12)
            store.append_many(written[cursor : cursor + size])
            cursor += size
        read = store.signals_for_turn("t1")
        assert len(read) == len(written)
        assert read == tuple(written)
        assert [one.payload["i"] for one in read] == list(range(250))

    def test_join_of_what_was_read_matches_join_of_what_was_written(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        written = [signal("k", span=f"s{index % 3}", at=index, i=index) for index in range(30)]
        store.append_many(written)
        assert join_spans(store.signals_for_turn("t1")) == join_spans(written)

    def test_concurrent_sensors_do_not_interleave_records(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        start = threading.Barrier(8)

        def emit(worker: int) -> None:
            start.wait()
            for index in range(25):
                store.append(signal("swap-ok", span=f"w{worker}", at=index, worker=worker))

        threads = [threading.Thread(target=emit, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        read = store.signals_for_turn("t1")
        assert len(read) == 200
        assert all(len(span) == 25 for span in join_spans(read).values())


class TestCrossInstanceSafety:
    """The writers are separate processes, so the lock that counts is the OS one."""

    @staticmethod
    def _hammer(stores: list[JsonlEvidenceStore], *, per_worker: int) -> None:
        start = threading.Barrier(len(stores))

        def emit(index: int, store: JsonlEvidenceStore) -> None:
            start.wait()
            for step in range(per_worker):
                store.append_many(
                    [
                        signal("swap-ok", span=f"w{index}", at=step, worker=index, step=step),
                        signal("animation", span=f"w{index}", at=step, worker=index, step=step),
                    ]
                )

        threads = [
            threading.Thread(target=emit, args=(index, store))
            for index, store in enumerate(stores)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def test_separate_instances_over_one_root_lose_nothing(self, tmp_path: Path) -> None:
        stores = [JsonlEvidenceStore(tmp_path) for _ in range(6)]
        self._hammer(stores, per_worker=20)
        read = JsonlEvidenceStore(tmp_path).signals_for_turn("t1")
        assert len(read) == 6 * 20 * 2
        joined = join_spans(read)
        assert len(joined) == 6
        assert all(len(span) == 40 for span in joined.values())

    def test_admission_numbers_are_unique_across_instances(self, tmp_path: Path) -> None:
        stores = [JsonlEvidenceStore(tmp_path) for _ in range(4)]
        self._hammer(stores, per_worker=15)
        raw = (tmp_path / EVIDENCE_DIRNAME / "t1.jsonl").read_text(encoding="utf-8").splitlines()
        numbers = [int(line.split(",", 1)[0].split(":", 1)[1]) for line in raw]
        assert sorted(numbers) == list(range(len(numbers)))

    def test_concurrent_recovery_cannot_truncate_committed_records(self, tmp_path: Path) -> None:
        seed = JsonlEvidenceStore(tmp_path)
        seed.append_many([signal("committed", span="w0", i=index) for index in range(30)])
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        with path.open("ab") as handle:
            handle.write(b'{"seq": 99, "span_id": "s1", "turn_id": "t1", "kin')

        stores = [JsonlEvidenceStore(tmp_path) for _ in range(6)]
        self._hammer(stores, per_worker=10)

        read = JsonlEvidenceStore(tmp_path).signals_for_turn("t1")
        assert len(by_kind(read, "committed")) == 30
        assert len(read) == 30 + 6 * 10 * 2
        assert path.with_name("t1.jsonl.torn").read_bytes().count(b"\n") == 1

    def test_a_reader_never_sees_a_partial_record(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        seen: list[int] = []
        stop = threading.Event()

        def write() -> None:
            for index in range(60):
                store.append_many([signal("swap-ok", i=index, blob="y" * 4000)])
            stop.set()

        def read() -> None:
            reader = JsonlEvidenceStore(tmp_path)
            while not stop.is_set():
                seen.append(len(reader.signals_for_turn("t1")))

        threads = [threading.Thread(target=write), threading.Thread(target=read)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert seen == sorted(seen)
        assert len(store.signals_for_turn("t1")) == 60


class TestCrashSafety:
    def test_torn_final_record_is_dropped_not_fatal(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append_many([signal("swap-ok"), signal("overflow")])
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        with path.open("ab") as handle:
            handle.write(b'{"span_id": "s1", "turn_id": "t1", "sou')
        assert [one.kind for one in store.signals_for_turn("t1")] == ["swap-ok", "overflow"]

    def test_appending_after_a_crash_quarantines_the_uncommitted_fragment(
        self, tmp_path: Path
    ) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("swap-ok"))
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        fragment = b'{"span_id": "s1", "turn_id": "t1", "sou'
        with path.open("ab") as handle:
            handle.write(fragment)
        store.append(signal("overflow"))
        assert [one.kind for one in store.signals_for_turn("t1")] == ["swap-ok", "overflow"]
        assert path.with_name("t1.jsonl.torn").read_bytes() == fragment + b"\n"

    def test_a_wholly_torn_file_recovers_to_empty(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        path.write_bytes(b'{"span_id"')
        store.append(signal("overflow"))
        assert [one.kind for one in store.signals_for_turn("t1")] == ["overflow"]

    def test_quarantine_files_are_not_turns(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("swap-ok"))
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        with path.open("ab") as handle:
            handle.write(b"{oops")
        store.append(signal("overflow"))
        assert store.turn_ids() == ("t1",)

    def test_corruption_in_the_middle_is_raised(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("swap-ok"))
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        with path.open("ab") as handle:
            handle.write(b"not json at all\n")
        with pytest.raises(StoreError, match="not valid JSON"):
            store.signals_for_turn("t1")

    def test_a_record_that_is_not_a_signal_is_raised(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        path.write_bytes(b'{"span_id": "s1"}\n')
        with pytest.raises(StoreError, match="not a Signal"):
            store.signals_for_turn("t1")

    @pytest.mark.parametrize("line", [b"null\n", b"123\n", b'"a string"\n', b"[1, 2]\n"])
    def test_a_json_scalar_line_is_a_store_error_not_a_leak(
        self, tmp_path: Path, line: bytes
    ) -> None:
        store = JsonlEvidenceStore(tmp_path)
        (tmp_path / EVIDENCE_DIRNAME / "t1.jsonl").write_bytes(line)
        with pytest.raises(StoreError, match="t1.jsonl:1 is not a Signal"):
            store.signals_for_turn("t1")

    def test_a_record_with_a_nonsense_admission_number_is_a_store_error(
        self, tmp_path: Path
    ) -> None:
        store = JsonlEvidenceStore(tmp_path)
        (tmp_path / EVIDENCE_DIRNAME / "t1.jsonl").write_bytes(b'{"seq": "later"}\n')
        with pytest.raises(StoreError, match="not a Signal"):
            store.signals_for_turn("t1")

    def test_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path)
        store.append(signal("swap-ok"))
        path = tmp_path / EVIDENCE_DIRNAME / "t1.jsonl"
        with path.open("ab") as handle:
            handle.write(b"\n\n")
        assert len(store.signals_for_turn("t1")) == 1

    def test_fsync_mode_still_round_trips(self, tmp_path: Path) -> None:
        store = JsonlEvidenceStore(tmp_path, fsync=True)
        store.append(signal("swap-ok"))
        assert len(store.signals_for_turn("t1")) == 1


class TestExemplarStore:
    def test_satisfies_the_port(self, tmp_path: Path) -> None:
        assert isinstance(JsonlExemplarStore(tmp_path), ExemplarStorePort)

    def test_directory_is_root_slash_exemplars_not_root(self, tmp_path: Path) -> None:
        """Regression: the store appends ``EXEMPLAR_DIRNAME`` to whatever root it is
        given. A caller that already appends ``"exemplars"`` before constructing the
        store doubles the segment -- see the CLI wiring test in ``test_servo.py``.
        """
        store = JsonlExemplarStore(tmp_path)
        assert store.directory == tmp_path / EXEMPLAR_DIRNAME

    def test_save_and_list_round_trip(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        saved = store.save_exemplar(
            "hero-2026-08",
            {"fragment.html": b"<section data-span-id='x'>hi</section>", "shot.png": b"\x89PNG\r"},
            {"picked_by": "human", "axes": ["density", "motion"], "round": 3},
        )
        assert saved == Exemplar(
            name="hero-2026-08",
            files=("fragment.html", "shot.png"),
            meta={"picked_by": "human", "axes": ["density", "motion"], "round": 3},
        )
        listed = store.list_exemplars()
        assert listed == (saved,)
        assert listed[0].meta["round"] == 3

    def test_files_are_stored_verbatim(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        blob = bytes(range(256))
        store.save_exemplar("raw", {"bytes.bin": blob}, {})
        assert (tmp_path / EXEMPLAR_DIRNAME / "raw" / "bytes.bin").read_bytes() == blob

    def test_meta_is_written_as_json(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("m", {"a.txt": b"a"}, {"note": "restraint"})
        text = (tmp_path / EXEMPLAR_DIRNAME / "m" / META_FILENAME).read_text(encoding="utf-8")
        assert '"note": "restraint"' in text

    def test_listing_is_sorted_and_ignores_stray_directories(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("zeta", {"a.txt": b"z"}, {})
        store.save_exemplar("alpha", {"a.txt": b"a"}, {})
        (tmp_path / EXEMPLAR_DIRNAME / "not-an-exemplar").mkdir()
        assert [one.name for one in store.list_exemplars()] == ["alpha", "zeta"]

    def test_re_saving_replaces_the_named_exemplar(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        store.save_exemplar("hero", {"a.txt": b"two"}, {"v": 2})
        assert (tmp_path / EXEMPLAR_DIRNAME / "hero" / "a.txt").read_bytes() == b"two"
        assert store.list_exemplars()[0].meta == {"v": 2}

    def test_re_saving_removes_files_the_new_pick_omits(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"a", "stale.png": b"old"}, {"v": 1})
        store.save_exemplar("hero", {"a.txt": b"a2"}, {"v": 2})
        assert store.list_exemplars()[0].files == ("a.txt",)
        assert not (tmp_path / EXEMPLAR_DIRNAME / "hero" / "stale.png").exists()

    def test_a_poisoned_meta_leaves_the_previous_exemplar_intact(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        with pytest.raises(StoreError, match="cannot serialise"):
            store.save_exemplar("hero", {"a.txt": b"two"}, {"v": object()})
        assert store.list_exemplars() == (
            Exemplar(name="hero", files=("a.txt",), meta={"v": 1}),
        )
        assert (tmp_path / EXEMPLAR_DIRNAME / "hero" / "a.txt").read_bytes() == b"one"

    def test_a_failed_file_write_leaves_the_previous_exemplar_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        original = Path.write_bytes

        def fail(self: Path, data: bytes) -> int:
            if self.name == "b.txt":
                raise OSError("no space left on device")
            return original(self, data)

        monkeypatch.setattr(Path, "write_bytes", fail)
        with pytest.raises(StoreError, match="cannot write exemplar"):
            store.save_exemplar("hero", {"a.txt": b"two", "b.txt": b"two"}, {"v": 2})
        monkeypatch.undo()
        assert store.list_exemplars() == (
            Exemplar(name="hero", files=("a.txt",), meta={"v": 1}),
        )

    def test_a_crash_between_the_two_renames_is_recovered(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        directory = tmp_path / EXEMPLAR_DIRNAME
        (directory / "hero").rename(directory / ".trash-hero-123-456")
        assert JsonlExemplarStore(tmp_path).list_exemplars() == (
            Exemplar(name="hero", files=("a.txt",), meta={"v": 1}),
        )
        assert not list(directory.glob(".trash-*"))

    def test_a_crash_before_the_swap_leaves_the_old_pick_and_no_debris(
        self, tmp_path: Path
    ) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        directory = tmp_path / EXEMPLAR_DIRNAME
        staging = directory / ".staging-hero-123-456"
        staging.mkdir()
        (staging / "a.txt").write_bytes(b"never promised")
        (staging / META_FILENAME).write_bytes(b"{}")
        assert JsonlExemplarStore(tmp_path).list_exemplars() == (
            Exemplar(name="hero", files=("a.txt",), meta={"v": 1}),
        )
        assert not list(directory.glob(".staging-*"))

    def test_recovery_does_not_clobber_a_completed_save(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"new"}, {"v": 2})
        directory = tmp_path / EXEMPLAR_DIRNAME
        trash = directory / ".trash-hero-1-1"
        trash.mkdir()
        (trash / "a.txt").write_bytes(b"old")
        (trash / META_FILENAME).write_bytes(b'{"v": 1}')
        assert JsonlExemplarStore(tmp_path).list_exemplars()[0].meta == {"v": 2}
        assert not list(directory.glob(".trash-*"))

    def test_staging_and_trash_are_never_listed_as_exemplars(self, tmp_path: Path) -> None:
        store = JsonlExemplarStore(tmp_path)
        store.save_exemplar("hero", {"a.txt": b"one"}, {"v": 1})
        directory = tmp_path / EXEMPLAR_DIRNAME
        for name in (".staging-hero-9-9", ".trash-hero-9-9"):
            (directory / name).mkdir()
            (directory / name / META_FILENAME).write_bytes(b"{}")
        assert [one.name for one in store.list_exemplars()] == ["hero"]

    def test_empty_exemplar_refused(self, tmp_path: Path) -> None:
        with pytest.raises(StoreError, match="no files"):
            JsonlExemplarStore(tmp_path).save_exemplar("empty", {}, {})

    def test_meta_filename_may_not_be_overwritten_by_a_file(self, tmp_path: Path) -> None:
        with pytest.raises(StoreError, match=META_FILENAME):
            JsonlExemplarStore(tmp_path).save_exemplar("x", {META_FILENAME: b"{}"}, {})

    @pytest.mark.parametrize("name", ["../escape", "with/slash", "", ".hidden"])
    def test_unsafe_names_refused(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(StoreError, match="safe path component"):
            JsonlExemplarStore(tmp_path).save_exemplar(name, {"a.txt": b"a"}, {})

    def test_unsafe_file_names_refused(self, tmp_path: Path) -> None:
        with pytest.raises(StoreError, match="safe path component"):
            JsonlExemplarStore(tmp_path).save_exemplar("ok", {"../out.txt": b"a"}, {})


class TestDomainStaysPure:
    """The dependency rule for this unit, checked where it is easiest to break."""

    @staticmethod
    def _imports(path: Path) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            match node:
                case ast.Import(names=names):
                    found.update(alias.name for alias in names)
                case ast.ImportFrom(module=module) if module:
                    found.add(module)
        return found

    def test_evidence_imports_only_stdlib_and_pydantic(self) -> None:
        roots = {
            name.partition(".")[0]
            for name in self._imports(REPO_ROOT / "ui_servo" / "domain" / "evidence.py")
        }
        assert roots - (sys.stdlib_module_names | {"pydantic"}) == set()

    def test_evidence_knows_nothing_of_ports_adapters_or_orjson(self) -> None:
        imports = self._imports(REPO_ROOT / "ui_servo" / "domain" / "evidence.py")
        assert not any(
            name.startswith(("ui_servo.ports", "ui_servo.adapters", "ui_servo.control"))
            or name == "orjson"
            for name in imports
        )

    def test_the_port_imports_no_adapter_and_no_serialiser(self) -> None:
        imports = self._imports(REPO_ROOT / "ui_servo" / "ports" / "store.py")
        assert "orjson" not in imports
        assert not any(name.startswith("ui_servo.adapters") for name in imports)

    def test_orjson_lives_in_the_adapter(self) -> None:
        assert "orjson" in self._imports(
            REPO_ROOT / "ui_servo" / "adapters" / "jsonl_store.py"
        )
