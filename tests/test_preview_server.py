"""The driving adapter, tested at both ends of the wire.

Three kinds of test live here and they are load-bearing in different ways.

The :class:`TestClient` tests pin the *contract of the routes*: what a beacon
body may look like, what lands in the stock, and what the shell does and does not
inject. They are fast, hermetic and run everywhere.

The hostile-input tests pin the *bounds*. ``POST /beacon`` is reachable by
anything that can reach the preview server and it writes to the stock the loop
steers on, so every limit the route claims is tested by exceeding it: an oversized
body, an over-long batch, a payload built to blow up the encoder, a timestamp
built to drag its neighbours into the previous century.

The Playwright test pins the thing nothing else can: that a real browser, given
the real shell, runs the real probe and that its evidence arrives in the real
store. Every layer between the fixture and the JSONL file is exercised at once --
shell assembly, script ordering, ``sendBeacon`` transport, defensive decode,
turn-id filing. It is the only test in the repository that can catch "the sensor
works and the ingest works and they do not work together", which is the failure
mode a tiered loop is most exposed to, so it is worth its seconds.
"""

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from ui_servo.adapters.beacon_ingest import (
    MAX_BODY_BYTES,
    MAX_EVENTS,
    BeaconError,
    IngestHealth,
    create_beacon_router,
    decode_batch,
    validate_turn_id,
)
from ui_servo.adapters.granian_server import serve
from ui_servo.adapters.jsonl_store import JsonlEvidenceStore
from ui_servo.adapters.preview_server import (
    FIXTURE_HTML,
    PROBE_JS,
    AssetMissing,
    candidate_path,
    create_app,
    probe_source,
    split_document,
)
from ui_servo.domain.contract import DirectionContract

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_TOML = REPO_ROOT / "direction" / "direction.toml"
TURN = "test-turn-0001"

FRAGMENT = (
    '<section class="p-lg bg-surface" data-span-id="hero">\n'
    '  <h1 class="type-display text-text">A candidate</h1>\n'
    "</section>\n"
)


@pytest.fixture(scope="session")
def contract() -> DirectionContract:
    return DirectionContract.from_toml(CONTRACT_TOML.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path: Path) -> JsonlEvidenceStore:
    return JsonlEvidenceStore(tmp_path / "evidence-root")


@pytest.fixture
def candidates(tmp_path: Path) -> Path:
    directory = tmp_path / "candidates"
    directory.mkdir()
    (directory / "hero.html").write_text(FRAGMENT, encoding="utf-8")
    return directory


@pytest.fixture
def client(
    candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract
) -> Iterator[TestClient]:
    with TestClient(create_app(candidates, store, contract, TURN, dev=True)) as running:
        yield running


def beacon_event(**overrides: object) -> dict[str, object]:
    """One event shaped exactly as ``probe.js`` emits it today.

    Note ``node``: a top-level compact element descriptor, added when the probe
    stopped shipping whole element objects inside payloads.
    """
    return {
        "spanId": "hero",
        "turnId": TURN,
        "kind": "swap-ok",
        "ts": 341,
        "node": "section #hero p-lg bg-surface",
        "payload": {"children": 1},
    } | overrides


def post_beacon(client: TestClient, batch: object) -> object:
    """Exactly what ``navigator.sendBeacon(url, JSON.stringify(batch))`` produces."""
    return client.post(
        "/beacon",
        content=json.dumps(batch),
        headers={"content-type": "text/plain;charset=UTF-8"},
    )


class TestBeaconIngest:
    def test_batch_lands_in_the_store_with_span_and_turn_intact(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        batch = [
            beacon_event(kind="swap-ok", ts=10),
            beacon_event(kind="unknown-class", ts=20, spanId="pricing"),
            beacon_event(kind="js-error", ts=30, payload={"message": "boom"}),
        ]
        assert post_beacon(client, batch).status_code == 204

        signals = store.signals_for_turn(TURN)
        assert [signal.kind for signal in signals] == ["swap-ok", "unknown-class", "js-error"]
        assert [signal.span_id for signal in signals] == ["hero", "pricing", "hero"]
        assert {signal.source for signal in signals} == {"probe"}
        assert {signal.turn_id for signal in signals} == {TURN}
        assert signals[2].payload["message"] == "boom"
        assert store.signals_for_span("pricing", turn_id=TURN) == (signals[1],)

    def test_a_stringified_body_is_parsed_despite_its_text_plain_label(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """``sendBeacon`` with a string sends ``text/plain``; the payload is JSON anyway."""
        assert post_beacon(client, [beacon_event()]).status_code == 204
        assert len(store.signals_for_turn(TURN)) == 1

    def test_an_application_json_blob_is_parsed_too(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        response = client.post(
            "/beacon",
            content=json.dumps([beacon_event()]).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 204
        assert len(store.signals_for_turn(TURN)) == 1

    def test_the_node_descriptor_survives_into_the_payload(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """``node`` is top-level on the wire and has no field in a Signal."""
        post_beacon(client, [beacon_event()])
        (signal,) = store.signals_for_turn(TURN)
        assert signal.payload["node"] == "section #hero p-lg bg-surface"
        assert signal.payload["children"] == 1

    def test_a_payload_that_already_uses_node_keeps_its_own(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        post_beacon(
            client,
            [beacon_event(node="div #mount", payload={"node": "reported by the sensor"})],
        )
        (signal,) = store.signals_for_turn(TURN)
        assert signal.payload["node"] == "reported by the sensor"
        assert signal.payload["node_selector"] == "div #mount"

    def test_a_null_node_is_omitted_rather_than_stored(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """Page-level events carry ``node: null``; absent means unknown, not null."""
        post_beacon(client, [beacon_event(node=None, spanId="probe")])
        (signal,) = store.signals_for_turn(TURN)
        assert "node" not in signal.payload

    @pytest.mark.parametrize("kind", ["probe-drop", "probe-config-incomplete"])
    def test_the_probes_self_reports_are_ordinary_evidence(
        self, client: TestClient, store: JsonlEvidenceStore, kind: str
    ) -> None:
        post_beacon(client, [beacon_event(kind=kind, spanId="probe", payload={"dropped": 12})])
        assert [signal.kind for signal in store.signals_for_turn(TURN)] == [kind]

    def test_unknown_kinds_are_stored_verbatim(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """A sensor this build has never heard of is still a witness."""
        post_beacon(client, [beacon_event(kind="sensor-from-the-future")])
        assert [signal.kind for signal in store.signals_for_turn(TURN)] == [
            "sensor-from-the-future"
        ]

    def test_an_event_with_no_kind_is_skipped_but_its_neighbours_are_not(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        post_beacon(
            client, [beacon_event(kind=""), beacon_event(kind="overflow"), "not an object"]
        )
        assert [signal.kind for signal in store.signals_for_turn(TURN)] == ["overflow"]

    def test_a_hostile_turn_id_is_filed_under_the_server_turn(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """The store turns a turn id into a filename, so the route may not trust one."""
        post_beacon(client, [beacon_event(turnId="../../etc/passwd")])
        post_beacon(client, [beacon_event(turnId="t:0007")])
        assert store.turn_ids() == (TURN,)
        assert len(store.signals_for_turn(TURN)) == 2

    def test_a_missing_turn_id_falls_back_to_the_server_turn(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        post_beacon(client, [beacon_event(turnId=None)])
        assert [signal.turn_id for signal in store.signals_for_turn(TURN)] == [TURN]

    def test_a_server_turn_id_the_store_could_not_file_is_refused_at_startup(
        self, candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract
    ) -> None:
        """Fail fast: the fallback turn is what every unusable client id lands on."""
        for bad in ["turn:0001", "../escape", "", ".hidden"]:
            with pytest.raises(ValueError, match="safe path component"):
                create_app(candidates, store, contract, bad)
            with pytest.raises(ValueError, match="safe path component"):
                create_beacon_router(store, turn_id=bad)
        assert validate_turn_id("turn-0001") == "turn-0001"

    def test_an_empty_batch_is_accepted_and_writes_nothing(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        assert post_beacon(client, []).status_code == 204
        assert client.post("/beacon", content=b"").status_code == 204
        assert store.turn_ids() == ()

    def test_a_body_that_is_not_json_is_refused(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        response = client.post("/beacon", content=b"<html>not a beacon</html>")
        assert response.status_code == 400
        assert store.turn_ids() == ()

    def test_a_single_event_object_is_accepted_as_a_batch_of_one(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        assert post_beacon(client, beacon_event()).status_code == 204
        assert len(store.signals_for_turn(TURN)) == 1


class TestBeaconLimits:
    """The route's bounds, each tested by exceeding it."""

    def test_a_body_over_the_cap_is_refused_with_413(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        oversized = json.dumps([beacon_event(payload={"blob": "x" * MAX_BODY_BYTES})])
        assert len(oversized) > MAX_BODY_BYTES
        response = client.post("/beacon", content=oversized)
        assert response.status_code == 413
        assert store.turn_ids() == ()

    def test_a_body_that_lies_about_its_length_is_still_cut_off(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        """Chunked senders declare nothing, so the stream itself must be counted."""

        def chunks() -> Iterator[bytes]:
            yield b"["
            for _ in range((MAX_BODY_BYTES // 64) + 64):
                yield b'{"kind":"swap-ok","spanId":"a","ts":1,"payload":{"x":"' + b"y" * 40 + b'"}},'
            yield b"]"

        response = client.post("/beacon", content=chunks())
        assert response.status_code == 413
        assert store.turn_ids() == ()

    def test_a_batch_longer_than_the_cap_keeps_the_cap_and_counts_the_rest(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        batch = [beacon_event(kind="longtask", ts=index) for index in range(MAX_EVENTS + 25)]
        assert post_beacon(client, batch).status_code == 204
        assert len(store.signals_for_turn(TURN)) == MAX_EVENTS
        assert client.get("/beacon/health").json()["truncated"] == 25

    def test_a_deep_or_wide_payload_is_clipped_rather_than_dropped(self) -> None:
        deep: dict[str, object] = {"leaf": "bottom"}
        for _ in range(50):
            deep = {"down": deep}
        (signal,) = decode_batch(
            json.dumps(
                [
                    beacon_event(
                        payload={
                            "deep": deep,
                            "long": "x" * 100_000,
                            "many": {str(index): index for index in range(500)},
                            "list": list(range(500)),
                        }
                    )
                ]
            ).encode(),
            turn_id=TURN,
        )
        assert len(signal.payload["long"]) == 4096
        assert len(signal.payload["many"]) == 64
        assert len(signal.payload["list"]) == 64
        assert json.dumps(signal.payload)  # the clipped payload is still serialisable

    def test_a_number_no_double_can_hold_is_refused_not_crashed(
        self, client: TestClient, store: JsonlEvidenceStore
    ) -> None:
        body = b'[{"kind":"js-error","spanId":"a","ts":1,"payload":{"n":1e400}}]'
        assert client.post("/beacon", content=body).status_code == 400
        assert store.turn_ids() == ()

    @pytest.mark.parametrize(
        "ts", ["1e308", "-1e308", "1e30", "-1", '"not-a-time"', "null", "true", "[]"]
    )
    def test_one_events_absurd_timestamp_cannot_move_another_events(self, ts: str) -> None:
        arrival = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        body = (
            '[{"kind":"swap-ok","spanId":"a","ts":1000},'
            f'{{"kind":"overflow","spanId":"a","ts":{ts}}},'
            '{"kind":"animation","spanId":"a","ts":1500}]'
        )
        good, bad, later = decode_batch(body.encode(), turn_id=TURN, received_at=arrival)
        assert later.observed_at == arrival
        assert (later.observed_at - good.observed_at).total_seconds() == pytest.approx(0.5)
        assert arrival - bad.observed_at <= timedelta(minutes=5)
        assert bad.observed_at <= arrival

    def test_the_anchor_ignores_events_that_were_never_stored(self) -> None:
        """A skipped event must not get a vote on where the batch sits in time."""
        arrival = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        (signal,) = decode_batch(
            json.dumps(
                [beacon_event(kind=None, ts=80_000_000), beacon_event(kind="swap-ok", ts=1000)]
            ).encode(),
            turn_id=TURN,
            received_at=arrival,
        )
        assert signal.observed_at == arrival


class TestIngestHealth:
    def test_health_counts_what_the_route_did(self, client: TestClient) -> None:
        post_beacon(client, [beacon_event(), beacon_event(kind="")])
        client.post("/beacon", content=b"nonsense")
        health = client.get("/beacon/health")
        assert health.status_code == 200
        body = health.json()
        assert body | {"last_error": None} == {
            "ok": True,
            "batches": 2,
            "events": 2,
            "stored": 1,
            "skipped": 1,
            "truncated": 0,
            "refused": 1,
            "lost": 0,
            "last_error": None,
        }
        assert "not JSON" in body["last_error"]

    def test_a_store_that_refuses_an_append_is_counted_and_logged_never_silent(
        self, candidates: Path, contract: DirectionContract, caplog: pytest.LogCaptureFixture
    ) -> None:
        from ui_servo.ports.store import StoreError

        class BrokenStore:
            def append(self, signal: object) -> None: ...

            def append_many(self, signals: object) -> int:
                raise StoreError("the disk is full")

            def signals_for_turn(self, turn_id: str) -> tuple[()]:
                return ()

            def signals_for_span(self, span_id: str, *, turn_id: str | None = None) -> tuple[()]:
                return ()

        app = create_app(candidates, BrokenStore(), contract, TURN)
        with TestClient(app) as client, caplog.at_level("ERROR"):
            assert post_beacon(client, [beacon_event()]).status_code == 204
            health = client.get("/beacon/health")
        assert health.status_code == 503  # a liveness check must notice lost evidence
        assert health.json() == pytest.approx(
            {**health.json(), "ok": False, "lost": 1, "last_error": "the disk is full"}
        )
        assert "the disk is full" in caplog.text
        assert app.state.ingest_health.lost == 1

    def test_the_router_shares_the_apps_counters(self, tmp_path: Path) -> None:
        """The counters passed in are the ones the route mutates, not a copy.

        Exercised through a request rather than an attribute, because the
        object the route writes to is what matters -- ``create_app`` reaches
        the same counters through ``app.state.ingest_health`` (see the two
        tests above), and this is the same guarantee at the router's own
        boundary, one layer further in.
        """
        health = IngestHealth()
        router = create_beacon_router(
            JsonlEvidenceStore(tmp_path / "evidence"), turn_id=TURN, health=health
        )
        app = Litestar(route_handlers=[router], openapi_config=None, logging_config=None)
        with TestClient(app) as router_client:
            assert post_beacon(router_client, [beacon_event()]).status_code == 204
        assert health.batches == 1
        assert health.stored == 1


class TestDecodeBatch:
    """The decoder alone, where the transport's edges are cheapest to pin."""

    def test_a_json_scalar_is_not_a_batch(self) -> None:
        with pytest.raises(BeaconError):
            decode_batch(b'"hello"', turn_id=TURN)

    def test_an_events_envelope_is_unwrapped(self) -> None:
        signals = decode_batch(json.dumps({"events": [beacon_event()]}).encode(), turn_id=TURN)
        assert [signal.kind for signal in signals] == ["swap-ok"]

    def test_a_non_object_payload_is_kept_rather_than_dropped(self) -> None:
        (signal,) = decode_batch(json.dumps([beacon_event(payload=7)]).encode(), turn_id=TURN)
        assert signal.payload["value"] == 7

    def test_an_event_with_no_span_is_still_attributable(self) -> None:
        (signal,) = decode_batch(json.dumps([beacon_event(spanId=None)]).encode(), turn_id=TURN)
        assert signal.span_id == "unattributed"

    def test_the_source_is_the_sensor_family_not_the_kind(self) -> None:
        (signal,) = decode_batch(json.dumps([beacon_event()]).encode(), turn_id=TURN)
        assert signal.source == "probe"

    def test_an_iso_ts_is_taken_at_its_word(self) -> None:
        (signal,) = decode_batch(
            json.dumps([beacon_event(ts="2026-08-05T12:00:00+00:00")]).encode(), turn_id=TURN
        )
        assert signal.ts == "2026-08-05T12:00:00+00:00"

    def test_a_page_relative_ts_becomes_an_ordered_instant(self) -> None:
        first, second = decode_batch(
            json.dumps(
                [beacon_event(kind="swap-ok", ts=100), beacon_event(kind="overflow", ts=1600)]
            ).encode(),
            turn_id=TURN,
        )
        assert first.observed_at < second.observed_at
        assert (second.observed_at - first.observed_at).total_seconds() == pytest.approx(1.5)
        assert second.observed_at <= datetime.now(UTC)


class TestCandidateRoute:
    def test_dev_injects_the_probe_and_its_configuration(
        self, client: TestClient, contract: DirectionContract
    ) -> None:
        html = client.get("/candidate/hero").text
        assert '<script src="/assets/probe.js"></script>' in html
        assert "window.__UI_SERVO__" in html
        assert f'"turnId":"{TURN}"' in html
        assert '"beacon":"/beacon"' in html
        for duration in contract.motion_table().durations_ms:
            assert str(duration) in html
        assert 'data-span-id="hero"' in html

    def test_the_motion_block_is_complete_so_the_probe_never_reports_it_missing(
        self, client: TestClient, contract: DirectionContract
    ) -> None:
        html = client.get("/candidate/hero").text
        start = html.index("window.__UI_SERVO__")
        config = json.loads(html[html.index("{", start) : html.index(";</script>", start)])
        assert set(config["motion"]) == {
            "durations",
            "easings",
            "properties",
            "reducedMotionRequired",
        }
        assert all(config["motion"][key] for key in ("durations", "easings", "properties"))
        assert config["motion"]["durations"] == sorted(contract.motion_table().durations_ms)

    def test_the_probe_loads_before_anything_the_candidate_brought(
        self, candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract
    ) -> None:
        """The shim can only wrap definitions it precedes; order is the contract."""
        (candidates / "opinionated.html").write_text(
            "<html><head><style>.own {}</style>"
            "<script>window.__UI_SERVO__ = {turnId: 'stale'};</script></head>"
            "<body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        with TestClient(create_app(candidates, store, contract, TURN)) as client:
            html = client.get("/candidate/opinionated").text
        assert (
            html.index("--color-accent")
            < html.index(f'"turnId":"{TURN}"')
            < html.index('src="/assets/probe.js"')
            < html.index(".own {}")
            < html.index("'stale'")
        )

    def test_not_dev_serves_the_same_fragment_unsensed(
        self, candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract
    ) -> None:
        with TestClient(create_app(candidates, store, contract, TURN, dev=False)) as client:
            html = client.get("/candidate/hero").text
        assert "probe.js" not in html
        assert "__UI_SERVO__" not in html
        assert "A candidate" in html
        assert "--color-accent" in html  # the contract is not what dev switches off

    def test_tokens_are_inline_so_the_fragment_lays_out_on_contract(
        self, client: TestClient, contract: DirectionContract
    ) -> None:
        html = client.get("/candidate/hero").text
        assert contract.to_css_custom_properties() in html

    def test_the_fragment_is_served_verbatim(self, client: TestClient) -> None:
        assert FRAGMENT.strip() in client.get("/candidate/hero").text

    def test_the_name_may_carry_its_extension(self, client: TestClient) -> None:
        assert client.get("/candidate/hero.html").status_code == 200

    def test_an_unknown_candidate_is_a_404(self, client: TestClient) -> None:
        assert client.get("/candidate/nosuch").status_code == 404

    @pytest.mark.parametrize(
        "name", ["..", "../../etc/passwd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", ".hidden", "a b"]
    )
    def test_a_name_that_is_not_a_path_component_never_reaches_the_disk(
        self, client: TestClient, name: str
    ) -> None:
        assert client.get(f"/candidate/{name}").status_code == 404

    def test_a_symlink_out_of_the_candidates_directory_is_refused(
        self, candidates: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.html"
        secret.write_text("<p>not a candidate</p>", encoding="utf-8")
        (candidates / "escape.html").symlink_to(secret)
        with pytest.raises(Exception, match="404|no candidate"):
            candidate_path(candidates, "escape")


class TestAssets:
    def test_probe_js_is_served_as_javascript(self, client: TestClient) -> None:
        response = client.get("/assets/probe.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert response.text == PROBE_JS.read_text(encoding="utf-8")

    def test_tokens_css_is_served_as_css(
        self, client: TestClient, contract: DirectionContract
    ) -> None:
        response = client.get("/assets/tokens.css")
        assert "text/css" in response.headers["content-type"]
        assert response.text == contract.to_css_custom_properties()

    def test_the_probe_is_found_without_a_source_checkout(self, monkeypatch, tmp_path) -> None:
        """The wheel carries a copy; the checkout path is only the last resort."""
        packaged = tmp_path / "packaged-probe.js"
        packaged.write_text("/* the packaged probe */", encoding="utf-8")
        monkeypatch.setenv("UI_SERVO_PROBE_JS", str(packaged))
        assert probe_source() == "/* the packaged probe */"

    def test_a_missing_probe_is_refused_at_construction_not_at_request_time(
        self, candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract, tmp_path
    ) -> None:
        with pytest.raises(AssetMissing, match="cannot find probe.js"):
            create_app(candidates, store, contract, TURN, probe_js=tmp_path / "gone.js")
        # dev=False promises nothing, so it must still start.
        assert create_app(
            candidates, store, contract, TURN, dev=False, probe_js=tmp_path / "gone.js"
        )


class TestFixtureRoute:
    def test_the_fixture_is_served_through_the_shell(self, client: TestClient) -> None:
        html = client.get("/fixture").text
        assert "probe fixture" in html
        assert "__PROBE_EVENTS__" in html  # the fixture's own head survived
        assert "invented-utility" in html  # and its body
        assert "../../probe/probe.js" not in html  # but its relative probe tag did not
        assert html.count("/assets/probe.js") == 1
        assert f'"turnId":"{TURN}"' in html

    def test_the_fixture_body_is_left_unstamped(self, client: TestClient) -> None:
        """The synthetic ``anon-N`` span is part of what the sensor test verifies."""
        html = client.get("/fixture").text
        assert "<body>" in html
        assert 'data-span-id="span-fixture-mount"' in html

    def test_an_absent_fixture_says_what_is_missing_and_why(
        self, candidates: Path, store: JsonlEvidenceStore, contract: DirectionContract, tmp_path
    ) -> None:
        app = create_app(candidates, store, contract, TURN, fixture_html=tmp_path / "gone.html")
        with TestClient(app) as client:
            response = client.get("/fixture")
        assert response.status_code == 404
        assert "development route" in response.json()["detail"]


class TestHeadRequests:
    """Litestar, unlike Starlette, does not add ``HEAD`` to a route just
    because ``GET`` is declared -- a route that forgot to ask for both would
    405 a liveness probe or a ``curl -I`` that used to get a 200. This walks
    the live route table rather than naming routes one at a time, so a route
    added later is covered by construction instead of by remembering to.
    """

    def test_every_get_route_also_answers_head(self, client: TestClient) -> None:
        get_routes = [route for route in client.app.routes if "GET" in route.methods]
        assert get_routes, "the app has no GET routes to check"
        for route in get_routes:
            assert "HEAD" in route.methods, f"{route.path} takes GET but not HEAD"

    def test_head_matches_get_on_concrete_paths(self, client: TestClient) -> None:
        concrete_paths = [
            route.path
            for route in client.app.routes
            if "GET" in route.methods and "{" not in route.path
        ]
        assert concrete_paths, "no concrete (parameter-free) GET routes to check"
        for path in concrete_paths:
            assert client.head(path).status_code == client.get(path).status_code

    def test_head_matches_get_on_the_candidate_route(self, client: TestClient) -> None:
        assert client.head("/candidate/hero").status_code == client.get("/candidate/hero").status_code
        assert client.head("/candidate/nosuch").status_code == client.get("/candidate/nosuch").status_code


class TestSplitDocument:
    def test_a_bare_fragment_passes_through(self) -> None:
        assert split_document(FRAGMENT) == ("", "", FRAGMENT)

    def test_a_fragment_that_merely_mentions_body_is_not_a_document(self) -> None:
        """The reason this uses a parser: ``<body>`` inside a script is text."""
        fragment = (
            '<div class="card" data-span-id="x">\n'
            '  <script>const t = "<body>oops</body>"; if (a < b) render();</script>\n'
            "  <!-- <body> in a comment, too -->\n"
            "</div>\n"
        )
        assert split_document(fragment) == ("", "", fragment)

    def test_body_inside_an_attribute_value_is_not_a_document_either(self) -> None:
        fragment = '<div data-template="<body></body>">text</div>'
        assert split_document(fragment) == ("", "", fragment)

    def test_a_real_document_is_split(self) -> None:
        extra, attrs, body = split_document(
            '<html><head><style>p{}</style></head><body class="x" data-y="1"><p>hi</p></body></html>'
        )
        assert extra == "<style>p{}</style>"
        assert attrs == ' class="x" data-y="1"'
        assert body == "<p>hi</p>"

    def test_a_document_with_an_implied_body_still_splits(self) -> None:
        extra, attrs, body = split_document("<html><head><title>t</title></head><p>hi</p></html>")
        assert extra == "<title>t</title>"
        assert attrs == ""
        assert body.strip() == "<p>hi</p>"

    def test_body_attributes_are_re_quoted_not_echoed(self) -> None:
        _, attrs, _ = split_document("<html><body class=unquoted hidden><p>hi</p></body></html>")
        assert attrs == ' class="unquoted" hidden'

    @pytest.mark.parametrize(
        "tag",
        [
            '<script src="../../probe/probe.js"></script>',
            "<script src='/static/probe.js' defer></script>",
            '<script  type="module"  src="probe.js" ></script>',
            '<script src="/assets/probe.min.js"></script>',
        ],
    )
    def test_any_self_hosted_probe_tag_is_removed(self, tag: str) -> None:
        """Two probes double-report every event they both see."""
        extra, _, _ = split_document(f"<html><head>{tag}</head><body></body></html>")
        assert "probe" not in extra
        _, _, body = split_document(f"<html><body><p>a</p>{tag}<p>b</p></body></html>")
        assert "probe" not in body
        assert "<p>a</p>" in body and "<p>b</p>" in body

    def test_a_script_that_only_looks_like_the_probe_is_left_alone(self) -> None:
        extra, _, _ = split_document(
            '<html><head><script src="/assets/probe-helpers.js"></script></head><body></body></html>'
        )
        assert "probe-helpers.js" in extra


# ----------------------------------------------------------- browser round trip ---


MISSING_BROWSER = (
    "executable doesn't exist",
    "playwright install",
    "looks like playwright was just installed",
)
"""Substrings that mean "no chromium on this machine", and nothing else.

Deliberately narrow, and deliberately not ``BrowserType.launch`` -- that prefix
is on *every* launch failure, including a crash and a broken sandbox. Skipping on
the base ``playwright.Error`` would turn all of those into a green run, which is
the one thing a browser acceptance test must never do.
"""


def _missing_browser(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in MISSING_BROWSER)


def _launch_chromium(driver: object):
    import playwright.sync_api as api

    try:
        return driver.chromium.launch(headless=True)
    except api.Error as error:
        if os.environ.get("UI_SERVO_REQUIRE_BROWSER") == "1" or not _missing_browser(error):
            raise
        pytest.skip(f"chromium is not installed for playwright: {error}")


class TestBrowserSkipPolicy:
    """A skip must mean "no browser here", never "the browser broke"."""

    def test_a_missing_executable_is_the_only_skip(self) -> None:
        assert _missing_browser(
            Exception(
                "BrowserType.launch: Executable doesn't exist at "
                "/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome\n"
                "Please run the following command to download new browsers:\n"
                "playwright install"
            )
        )
        for real_breakage in [
            "BrowserType.launch: Target page, context or browser has been closed",
            "BrowserType.launch: Browser closed. Most likely the page has crashed",
            "BrowserType.launch: Protocol error (Browser.getVersion)",
            "Unknown option --nope",
        ]:
            assert not _missing_browser(Exception(real_breakage)), real_breakage


@pytest.mark.playwright
def test_a_real_browser_running_the_probe_fills_the_evidence_store(
    tmp_path: Path, contract: DirectionContract
) -> None:
    """The whole fast loop, end to end, in one page load.

    The fixture provokes each sensor deterministically, so the assertion is a
    subset check on kinds rather than a count: what is being verified is that
    every family of observation survives the trip from ``getAnimations()`` to a
    line of JSONL, not how many times the browser happened to fire each one.
    """
    playwright_api = pytest.importorskip("playwright.sync_api")
    if not FIXTURE_HTML.is_file():
        pytest.skip(f"no probe fixture at {FIXTURE_HTML}")

    expected = {"swap-ok", "unknown-class", "overflow", "motion-violation", "js-error"}
    store = JsonlEvidenceStore(tmp_path / "evidence-root")
    app = create_app(tmp_path, store, contract, TURN, dev=True)

    console: list[str] = []
    # A real port, because ``sendBeacon`` cannot talk to a TestClient.
    with serve(app) as base_url, playwright_api.sync_playwright() as driver:
        browser = _launch_chromium(driver)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.on("console", lambda message: console.append(message.text))
            page.goto(f"{base_url}/fixture", wait_until="load")

            # The probe batches non-urgent kinds on a 2 s timer; poll rather than
            # sleep a fixed amount so a fast machine is not punished for it.
            deadline = time.monotonic() + 25
            found: set[str] = set()
            while time.monotonic() < deadline and not expected <= found:
                page.wait_for_timeout(250)
                page.evaluate("window.__UI_SERVO_PROBE__?.flush()")
                found = {signal.kind for signal in store.signals_for_turn(TURN)}
            in_page = set(page.evaluate("window.__PROBE_KINDS__ ? __PROBE_KINDS__() : []"))
            health = page.evaluate(
                "fetch('/beacon/health').then((r) => r.json())"
            )
        finally:
            browser.close()

    signals = store.signals_for_turn(TURN)
    kinds = {signal.kind for signal in signals}
    assert expected <= kinds, (
        f"store has {sorted(kinds)}; the page itself saw {sorted(in_page)}; console: {console}"
    )

    # The server's own configuration is complete: the probe says so, by not
    # saying otherwise.
    assert "probe-config-incomplete" not in kinds
    assert "probe-drop" not in kinds
    assert health["ok"] is True and health["lost"] == 0

    # The evidence is filed under this server's turn, keyed by spans the page
    # declared, and joins back to the fixture's own mount.
    assert {signal.turn_id for signal in signals} == {TURN}
    assert {signal.source for signal in signals} == {"probe"}
    assert "span-fixture-mount" in {signal.span_id for signal in signals}

    # The new wire format: a top-level node descriptor, carried into the payload.
    described = [signal.payload["node"] for signal in signals if "node" in signal.payload]
    assert any("#mount" in node for node in described)

    # The unstamped fixture body: the sensor's synthetic fallback, as documented.
    boot = [signal for signal in signals if signal.kind == "swap-ok"]
    assert any(signal.span_id.startswith("anon-") for signal in boot)

    violations = [signal for signal in signals if signal.kind == "motion-violation"]
    assert any(signal.payload.get("reasons") for signal in violations)
    assert (tmp_path / "evidence-root" / "evidence" / f"{TURN}.jsonl").is_file()
