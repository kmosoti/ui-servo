"""The deployed shape of the ingest service: its configuration, and its refusals.

``tests/test_preview_server.py`` already covers what the router *does* with a
batch. What is tested here is the part that only exists in production -- the
factory Granian imports -- and in particular every way it declines to start.
A write endpoint that comes up pointed at the wrong directory is worse than one
that does not come up at all: the first loses evidence silently for as long as
nobody looks, and the second shows red in ``systemctl status`` immediately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from litestar.testing import TestClient

from ui_servo.adapters.beacon_ingest.app import (
    FSYNC_ENV,
    ROOT_ENV,
    TURN_ENV,
    ConfigError,
    create_app,
)


class TestConfigurationRefusals:
    def test_a_missing_root_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError, match=ROOT_ENV):
            create_app({})

    def test_a_blank_root_is_refused_like_a_missing_one(self) -> None:
        with pytest.raises(ConfigError, match=ROOT_ENV):
            create_app({ROOT_ENV: "   "})

    def test_a_relative_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="absolute"):
            create_app({ROOT_ENV: "var/lib/ui-servo"})

    def test_a_root_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not an existing directory"):
            create_app({ROOT_ENV: str(tmp_path / "absent")})

    def test_a_root_ending_in_evidence_is_refused_with_the_path_it_would_have_used(
        self, tmp_path: Path
    ) -> None:
        # The store appends evidence/ itself, so this would land in
        # <root>/evidence/evidence -- the same doubling UI_SERVO_PROMOTED_ROOT
        # invites on the Rust side.
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        with pytest.raises(ConfigError, match=r"evidence[/\\]evidence"):
            create_app({ROOT_ENV: str(evidence)})

    def test_a_turn_id_that_is_a_path_is_refused(self, tmp_path: Path) -> None:
        # The turn id becomes a filename. This one is the traversal attempt.
        with pytest.raises(ConfigError, match=TURN_ENV):
            create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "../../etc/passwd"})

    def test_a_non_boolean_fsync_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=FSYNC_ENV):
            create_app({ROOT_ENV: str(tmp_path), FSYNC_ENV: "sometimes"})


class TestTheServiceItServes:
    def test_the_default_turn_id_is_date_stamped_so_a_restart_opens_a_new_file(
        self, tmp_path: Path
    ) -> None:
        app = create_app({ROOT_ENV: str(tmp_path)})
        with TestClient(app=app) as client:
            assert client.post("/beacon", json=[{"kind": "probe.ready"}]).status_code == 204
        written = list((tmp_path / "evidence").glob("prod-*.jsonl"))
        assert len(written) == 1, written
        # prod-YYYY-MM-DD
        assert len(written[0].stem) == len("prod-2026-08-10")

    def test_an_explicit_turn_id_names_the_file(self, tmp_path: Path) -> None:
        app = create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "canary"})
        with TestClient(app=app) as client:
            assert client.post("/beacon", json=[{"kind": "probe.ready"}]).status_code == 204
        assert (tmp_path / "evidence" / "canary.jsonl").is_file()

    def test_a_beacon_batch_lands_as_evidence(self, tmp_path: Path) -> None:
        app = create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "t"})
        with TestClient(app=app) as client:
            response = client.post(
                "/beacon",
                json=[{"kind": "lcp", "value": 812.5, "node": "main h1"}],
            )
        assert response.status_code == 204
        lines = (tmp_path / "evidence" / "t.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["kind"] == "lcp"

    def test_health_reports_and_stays_200_while_nothing_is_lost(self, tmp_path: Path) -> None:
        app = create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "t"})
        with TestClient(app=app) as client:
            client.post("/beacon", json=[{"kind": "probe.ready"}])
            health = client.get("/beacon/health")
        assert health.status_code == 200
        assert health.json()["stored"] == 1

    def test_the_schema_route_is_not_mounted(self, tmp_path: Path) -> None:
        # A write endpoint should not also publish a description of itself.
        app = create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "t"})
        with TestClient(app=app) as client:
            assert client.get("/schema").status_code == 404

    def test_debug_is_off_so_a_failure_cannot_render_a_traceback(self, tmp_path: Path) -> None:
        assert create_app({ROOT_ENV: str(tmp_path), TURN_ENV: "t"}).debug is False
