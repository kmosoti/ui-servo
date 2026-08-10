"""The judges, exercised without spending a token.

Every family is reached through a process or a socket, so the interesting
behaviour is not "did the model answer well" -- that is the panel's job -- but
"what does this adapter do when the envelope is odd, the binary is missing, the
bridge is down or the reply is prose". Those paths are where a panel silently
loses a third of its variety, and they are all reproducible with a fake
:func:`subprocess.run` and a fake ``httpx.Client``.

One live smoke test at the bottom asks each real family for ``{"ping": "pong"}``
against a trivial schema. It is gated behind ``UI_SERVO_LIVE=1`` because it costs
money and needs three logins, and it exists because envelope formats are the
kind of thing that changes under a CLI update without anyone announcing it.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from ui_servo.adapters import cli_judges
from ui_servo.adapters.cli_judges import (
    ClaudeJudge,
    CodexJudge,
    GeminiJudge,
    SignalRecorder,
    compose_prompt,
    extract_json,
)
from ui_servo.adapters.jsonl_store import JsonlEvidenceStore
from ui_servo.ports.judge import (
    RC_OK,
    RC_TIMEOUT,
    RC_TRANSPORT,
    RC_UNAVAILABLE,
    JudgePort,
    JudgeRequest,
    JudgeResponse,
    ScriptedJudge,
    families,
)

PING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ping": {"type": "string"}},
    "required": ["ping"],
    "additionalProperties": False,
}


@dataclass
class FakeRun:
    """A stand-in for :func:`subprocess.run` that records how it was called."""

    outcomes: list[Any]
    calls: list[list[str]] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(item) for item in argv])
        self.kwargs.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(self.calls[-1], kwargs)
        return outcome

    @property
    def prompts(self) -> list[str]:
        """What each call actually put in front of the model, stdin or argv."""
        sent: list[str] = []
        for call, kwargs in zip(self.calls, self.kwargs, strict=True):
            piped = kwargs.get("input")
            sent.append(piped if piped is not None else call[call.index("-p") + 1])
        return sent


def completed(stdout: str = "", *, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["fake"], returncode=rc, stdout=stdout, stderr=stderr)


def claude_envelope(result: str, *, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "duration_ms": 2205,
            "session_id": "663b1789",
            "total_cost_usd": 0.13,
            "result": result,
        }
    )


def codex_writes(text: str, *, rc: int = 0, stdout: str = "banner\ntokens used\n22940\n"):
    def run(argv: list[str], kwargs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        Path(argv[argv.index("-o") + 1]).write_text(text, encoding="utf-8")
        return completed(stdout, rc=rc)

    return run


def agy_stream(response: str, *, status: str = "SUCCESS", trailer: bool = True) -> str:
    lines = [
        {"event": "init", "conversation_id": "abc", "init": {"cwd": "/home"}},
        {"event": "step_update", "step_update": {"step_index": 3, "text_delta": response[:4]}},
        {
            "event": "result",
            "result": {
                "conversation_id": "abc",
                "status": status,
                "response": response,
                "duration_seconds": 4.8,
            },
        },
    ]
    if trailer:
        lines.append({"bridge_status": "ok", "duration_s": 7.75, "rc": 0, "stderr": ""})
    return "\n".join(json.dumps(line) for line in lines) + "\n"


@dataclass
class FakeHttp:
    """Replaces ``httpx.Client`` for the bridge path."""

    body: str = ""
    status_code: int = 200
    error: BaseException | None = None
    health_status: int = 200
    health_error: BaseException | None = None
    posts: list[dict[str, Any]] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    client_kwargs: dict[str, Any] = field(default_factory=dict)

    def __call__(self, **kwargs: Any) -> FakeHttp:
        self.client_kwargs = kwargs
        return self

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.gets.append(url)
        if self.health_error is not None:
            raise self.health_error
        return httpx.Response(
            status_code=self.health_status, request=httpx.Request("GET", url), text="{}"
        )

    def __enter__(self) -> FakeHttp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return httpx.Response(
            status_code=self.status_code, text=self.body, request=httpx.Request("POST", url)
        )


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch):
    def install(*outcomes: Any) -> FakeRun:
        fake = FakeRun(outcomes=list(outcomes))
        monkeypatch.setattr(cli_judges.subprocess, "run", fake)
        return fake

    return install


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def install(**kwargs: Any) -> tuple[FakeHttp, Path]:
        fake = FakeHttp(**kwargs)
        monkeypatch.setattr(cli_judges.httpx, "Client", fake)
        token = tmp_path / "token"
        token.write_text("s3cret\n", encoding="utf-8")
        return fake, token

    return install


class TestJsonExtraction:
    """The one piece of glue every family leans on."""

    def test_bare_object(self) -> None:
        assert extract_json('{"ping": "pong"}') == {"ping": "pong"}

    def test_fenced_object(self) -> None:
        text = 'Sure!\n\n```json\n{"ping": "pong"}\n```\n\nHope that helps.'
        assert extract_json(text) == {"ping": "pong"}

    def test_unlabelled_fence(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_object_buried_in_prose(self) -> None:
        assert extract_json('Here you go: {"a": {"b": [1, 2]}} -- done.') == {"a": {"b": [1, 2]}}

    def test_brace_inside_a_string_does_not_close_the_object(self) -> None:
        text = 'note:\n{"comment": "use } sparingly", "score": 3}\n'
        assert extract_json(text) == {"comment": "use } sparingly", "score": 3}

    def test_escaped_quote_inside_a_string(self) -> None:
        assert extract_json(r'x {"q": "say \"hi\"", "n": 1} y') == {"q": 'say "hi"', "n": 1}

    def test_prose_only_is_none(self) -> None:
        assert extract_json("I cannot comply with that request.") is None

    def test_json_array_is_not_an_object(self) -> None:
        assert extract_json("[1, 2, 3]") is None

    def test_empty(self) -> None:
        assert extract_json("") is None


class TestPromptComposition:
    def test_images_are_listed_as_absolute_paths(self) -> None:
        request = JudgeRequest.of("Critique this.", images=["/tmp/shot.png", Path("/tmp/b.png")])
        prompt = compose_prompt(request)
        assert "/tmp/shot.png" in prompt
        assert "/tmp/b.png" in prompt
        assert prompt.startswith("Critique this.")

    def test_schema_demands_json_only(self) -> None:
        prompt = compose_prompt(JudgeRequest.of("Rate it.", response_schema=PING_SCHEMA))
        assert "ONLY" in prompt
        assert '"ping"' in prompt

    def test_no_schema_no_json_demand(self) -> None:
        assert compose_prompt(JudgeRequest.of("Rate it.")) == "Rate it."


class TestClaudeJudge:
    def test_unwraps_the_result_envelope(self, run) -> None:
        fake = run(completed(claude_envelope("PONG")))
        response = ClaudeJudge(binary="/x/claude").judge(JudgeRequest.of("ping"))
        assert response.family == "claude"
        assert response.raw == "PONG"
        assert response.rc == RC_OK
        assert response.attempts == 1
        assert fake.calls[0][0] == "/x/claude"
        assert fake.calls[0][-3:] == ["--output-format", "json", "-p"]
        assert fake.prompts == ["ping"]

    def test_parses_a_fenced_payload_out_of_the_envelope(self, run) -> None:
        run(completed(claude_envelope('```json\n{"ping": "pong"}\n```')))
        response = ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert response.parsed == {"ping": "pong"}
        assert response.usable

    def test_is_error_envelope_is_a_failure(self, run) -> None:
        run(completed(claude_envelope("rate limited", is_error=True)))
        response = ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert response.rc == RC_TRANSPORT
        assert not response

    def test_unparseable_stdout_is_a_failure_not_an_exception(self, run) -> None:
        run(completed("Usage: claude [options]"))
        response = ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert response.rc == RC_TRANSPORT
        assert response.raw == "Usage: claude [options]"

    def test_nonzero_exit_keeps_stderr(self, run) -> None:
        run(completed("", rc=2, stderr="not logged in"))
        response = ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert response.rc == 2
        assert "not logged in" in response.raw

    def test_timeout_is_a_response_not_an_exception(self, run) -> None:
        run(subprocess.TimeoutExpired(cmd="claude", timeout=1, output="partial"))
        response = ClaudeJudge().judge(JudgeRequest.of("ping", timeout_s=1))
        assert response.rc == RC_TIMEOUT
        assert response.parsed is None
        assert response.raw == "partial"

    def test_missing_binary_is_unavailable(self, run) -> None:
        run(FileNotFoundError(2, "No such file or directory"))
        response = ClaudeJudge(binary="/nope/claude").judge(JudgeRequest.of("ping"))
        assert response.rc == RC_UNAVAILABLE

    def test_prompt_travels_on_stdin_not_in_argv(self, run) -> None:
        fake = run(completed(claude_envelope("ok")))
        ClaudeJudge().judge(JudgeRequest.of("--version"))
        assert fake.kwargs[0]["input"] == "--version"
        assert "--version" not in fake.calls[0]
        assert "stdin" not in fake.kwargs[0]


class TestCodexJudge:
    def test_reads_the_final_message_file_not_stdout(self, run) -> None:
        fake = run(codex_writes('{"ping": "pong"}'))
        response = CodexJudge(binary="/x/codex").judge(
            JudgeRequest.of("ping", response_schema=PING_SCHEMA)
        )
        assert response.raw.strip() == '{"ping": "pong"}'
        assert response.parsed == {"ping": "pong"}
        assert fake.calls[0][1:5] == ["exec", "-s", "read-only", "--skip-git-repo-check"]
        assert fake.calls[0][-1] == "-"
        assert fake.prompts[0].startswith("ping")

    def test_output_schema_file_carries_the_schema(self, run) -> None:
        captured: dict[str, Any] = {}

        def run_one(argv: list[str], kwargs: dict[str, Any]):
            captured["schema"] = json.loads(
                Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8")
            )
            Path(argv[argv.index("-o") + 1]).write_text('{"ping": "pong"}', encoding="utf-8")
            return completed("banner")

        run(run_one)
        CodexJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert captured["schema"] == PING_SCHEMA

    def test_no_schema_means_no_output_schema_flag(self, run) -> None:
        fake = run(codex_writes("plain prose"))
        response = CodexJudge().judge(JudgeRequest.of("ping"))
        assert "--output-schema" not in fake.calls[0]
        assert response.raw == "plain prose"
        assert response.parsed is None

    def test_salvages_the_last_transcript_object_not_the_echoed_schema(self, run) -> None:
        transcript = (
            "OpenAI Codex v0.146.1\nuser\n"
            + json.dumps(PING_SCHEMA)
            + '\ncodex\n{"ping": "pong"}\ntokens used\n22,940\n'
        )
        run(codex_writes("", stdout=transcript))
        response = CodexJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert response.rc == RC_OK
        assert response.parsed == {"ping": "pong"}

    def test_empty_output_file_with_no_recoverable_json_is_a_failure(self, run) -> None:
        run(codex_writes("", stdout="OpenAI Codex v0.146.1\ntokens used\n12\n"))
        response = CodexJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert response.rc == RC_TRANSPORT

    def test_empty_output_file_without_a_schema_is_a_failure(self, run) -> None:
        run(codex_writes("", stdout='banner\n{"ping": "pong"}\n'))
        response = CodexJudge().judge(JudgeRequest.of("ping"))
        assert response.rc == RC_TRANSPORT

    def test_a_flag_shaped_prompt_still_reaches_the_model(self, run) -> None:
        fake = run(codex_writes("fine"))
        CodexJudge().judge(JudgeRequest.of("--help"))
        assert fake.calls[0][-1] == "-"
        assert fake.kwargs[0]["input"] == "--help"
        assert "--help" not in fake.calls[0]

    def test_timeout_is_a_response_not_an_exception(self, run) -> None:
        run(subprocess.TimeoutExpired(cmd="codex", timeout=1))
        response = CodexJudge().judge(JudgeRequest.of("ping", timeout_s=1))
        assert response.rc == RC_TIMEOUT
        assert response.family == "codex"

    def test_missing_binary_is_unavailable(self, run) -> None:
        run(FileNotFoundError(2, "No such file or directory"))
        assert CodexJudge(binary="/nope").judge(JudgeRequest.of("ping")).rc == RC_UNAVAILABLE


class TestGeminiJudge:
    def test_reads_the_final_result_event(self, bridge) -> None:
        fake, token = bridge(body=agy_stream("PONG\n"))
        response = GeminiJudge(token_path=token).judge(JudgeRequest.of("ping"))
        assert response.family == "gemini"
        assert response.raw == "PONG\n"
        assert response.rc == RC_OK
        assert fake.posts[0]["headers"]["Authorization"] == "Bearer s3cret"
        assert fake.posts[0]["json"] == {"prompt": "ping"}

    def test_the_bridge_client_ignores_proxy_environment(self, bridge) -> None:
        fake, token = bridge(body=agy_stream("PONG"))
        GeminiJudge(token_path=token).judge(JudgeRequest.of("ping"))
        assert fake.client_kwargs["trust_env"] is False

    def test_parses_json_out_of_the_result_event(self, bridge) -> None:
        _, token = bridge(body=agy_stream('```json\n{"ping": "pong"}\n```'))
        response = GeminiJudge(token_path=token).judge(
            JudgeRequest.of("ping", response_schema=PING_SCHEMA)
        )
        assert response.parsed == {"ping": "pong"}

    def test_stream_without_a_result_event_is_a_failure(self, bridge) -> None:
        _, token = bridge(body='{"event": "init"}\n{"bridge_status": "ok", "rc": 0}\n')
        response = GeminiJudge(token_path=token).judge(JudgeRequest.of("ping"))
        assert response.rc == RC_TRANSPORT

    def test_non_success_status_is_a_failure(self, bridge) -> None:
        _, token = bridge(body=agy_stream("blocked", status="ERROR"))
        assert GeminiJudge(token_path=token).judge(JudgeRequest.of("ping")).rc == RC_TRANSPORT

    def test_connect_error_falls_back_to_the_cli(self, bridge, run) -> None:
        _, token = bridge(error=httpx.ConnectError("connection refused"))
        fake = run(completed("PONG from cli"))
        response = GeminiJudge(token_path=token, binary="/x/agy").judge(JudgeRequest.of("ping"))
        assert response.rc == RC_OK
        assert response.raw == "PONG from cli"
        assert fake.calls[0] == ["/x/agy", "--mode", "plan", "--sandbox", "-p", "ping"]

    def test_connect_timeout_also_falls_back(self, bridge, run) -> None:
        _, token = bridge(error=httpx.ConnectTimeout("no route"))
        fake = run(completed("PONG from cli"))
        response = GeminiJudge(token_path=token).judge(JudgeRequest.of("ping"))
        assert response.rc == RC_OK
        assert response.raw == "PONG from cli"
        assert len(fake.calls) == 1

    def test_http_error_status_falls_back_to_the_cli(self, bridge, run) -> None:
        _, token = bridge(body="nope", status_code=502)
        fake = run(completed("PONG from cli"))
        assert GeminiJudge(token_path=token).judge(JudgeRequest.of("ping")).raw == "PONG from cli"
        assert fake.calls

    def test_missing_token_falls_back_to_the_cli(self, run, tmp_path: Path) -> None:
        fake = run(completed("PONG from cli"))
        response = GeminiJudge(token_path=tmp_path / "absent").judge(JudgeRequest.of("ping"))
        assert response.raw == "PONG from cli"
        assert fake.calls

    def test_read_timeout_does_not_double_spend_on_the_cli(self, bridge, run) -> None:
        _, token = bridge(error=httpx.ReadTimeout("too slow"))
        fake = run(completed("should not be called"))
        response = GeminiJudge(token_path=token).judge(JudgeRequest.of("ping", timeout_s=1))
        assert response.rc == RC_TIMEOUT
        assert fake.calls == []

    def test_missing_cli_after_a_dead_bridge_is_unavailable(self, bridge, run) -> None:
        _, token = bridge(error=httpx.ConnectError("refused"))
        run(FileNotFoundError(2, "No such file"))
        assert GeminiJudge(token_path=token).judge(JudgeRequest.of("ping")).rc == RC_UNAVAILABLE


class TestToolRestrictions:
    """A critic must not be able to act on the workspace it is judging.

    The prompt carries builder-authored markup and screenshot paths, so it is
    attacker-influenced text handed to an agent holding the owner's ambient
    permissions. The restriction has to live in the argv, where the CLI enforces
    it, and not in the wording of the prompt, which is the thing under attack.
    """

    def test_claude_runs_in_plan_mode_with_mutating_tools_denied(self, run) -> None:
        fake = run(completed(claude_envelope("ok")))
        ClaudeJudge().judge(JudgeRequest.of("ping"))
        argv = fake.calls[0]
        assert argv[argv.index("--permission-mode") + 1] == "plan"
        denied = argv[argv.index("--disallowedTools") + 1]
        assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(denied.split(","))

    def test_claude_never_bypasses_permissions(self, run) -> None:
        fake = run(completed(claude_envelope("ok")))
        ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert not any("dangerously" in item for item in fake.calls[0])

    def test_codex_runs_read_only_outside_a_repo(self, run) -> None:
        fake = run(codex_writes("ok"))
        CodexJudge().judge(JudgeRequest.of("ping"))
        assert fake.calls[0][fake.calls[0].index("-s") + 1] == "read-only"
        assert "--skip-git-repo-check" in fake.calls[0]
        assert not any("dangerously" in item for item in fake.calls[0])

    def test_agy_fallback_runs_in_plan_mode_and_sandboxed(self, bridge, run) -> None:
        _, token = bridge(error=httpx.ConnectError("refused"))
        fake = run(completed("ok"))
        GeminiJudge(token_path=token).judge(JudgeRequest.of("ping"))
        assert fake.calls[0][fake.calls[0].index("--mode") + 1] == "plan"
        assert "--sandbox" in fake.calls[0]
        assert not any("dangerously" in item for item in fake.calls[0])

    def test_every_family_restricts_every_attempt_including_the_re_ask(self, run) -> None:
        fake = run(completed(claude_envelope("prose")))
        ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert len(fake.calls) == 2
        assert all("--permission-mode" in call for call in fake.calls)


class TestImagePaths:
    """A screenshot the critic cannot open is worse than no screenshot."""

    def test_relative_paths_are_resolved_before_interpolation(
        self, run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG")
        monkeypatch.chdir(tmp_path)
        fake = run(completed(claude_envelope("ok")))
        ClaudeJudge().judge(JudgeRequest.of("Critique.", images=["shot.png"]))
        sent = fake.prompts[0]
        assert str(shot.resolve()) in sent
        assert "\n- shot.png" not in sent

    def test_dot_dot_segments_are_collapsed(self, run, tmp_path: Path) -> None:
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG")
        fake = run(completed(claude_envelope("ok")))
        ClaudeJudge().judge(
            JudgeRequest.of("Critique.", images=[tmp_path / "sub" / ".." / "shot.png"])
        )
        assert f"- {shot.resolve()}" in fake.prompts[0]

    def test_a_missing_image_is_refused_without_spending_a_call(
        self, run, tmp_path: Path
    ) -> None:
        fake = run(completed(claude_envelope("ok")))
        response = ClaudeJudge().judge(
            JudgeRequest.of("Critique.", images=[tmp_path / "absent.png"])
        )
        assert response.rc == RC_UNAVAILABLE
        assert "absent.png" in response.raw
        assert fake.calls == []

    def test_a_directory_is_not_an_image(self, run, tmp_path: Path) -> None:
        fake = run(completed(claude_envelope("ok")))
        response = ClaudeJudge().judge(JudgeRequest.of("Critique.", images=[tmp_path]))
        assert response.rc == RC_UNAVAILABLE
        assert fake.calls == []

    def test_the_refusal_is_recorded_as_evidence(self, run, tmp_path: Path) -> None:
        run(completed(claude_envelope("ok")))
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge(recorder=SignalRecorder(store, "hero", "t1")).judge(
            JudgeRequest.of("Critique.", images=[tmp_path / "absent.png"])
        )
        (signal,) = store.signals_for_turn("t1")
        assert signal.kind == "judge-error"
        assert signal.payload["attempts"][0]["transport_detail"] == {"sent": False}

    def test_every_family_refuses_the_same_way(self, run, tmp_path: Path) -> None:
        run(completed("unused"))
        request = JudgeRequest.of("Critique.", images=[tmp_path / "absent.png"])
        for judge in (ClaudeJudge(), CodexJudge(), GeminiJudge()):
            assert judge.judge(request).rc == RC_UNAVAILABLE


class TestSchemaReask:
    """The one retry, and the cases that must not trigger it."""

    def test_prose_is_re_asked_once_and_then_parses(self, run) -> None:
        fake = run(
            completed(claude_envelope("I think it looks fine, honestly.")),
            completed(claude_envelope('{"ping": "pong"}')),
        )
        response = ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert response.parsed == {"ping": "pong"}
        assert response.attempts == 2
        assert len(fake.calls) == 2
        assert "ONLY valid JSON" in fake.prompts[1]
        assert "I think it looks fine" in fake.prompts[1]

    def test_re_ask_happens_at_most_once(self, run) -> None:
        fake = run(completed(claude_envelope("still prose")))
        response = ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert len(fake.calls) == 2
        assert response.parsed is None
        assert response.rc == RC_OK
        assert not response.usable

    def test_good_json_is_never_re_asked(self, run) -> None:
        fake = run(completed(claude_envelope('{"ping": "pong"}')))
        ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert len(fake.calls) == 1

    def test_no_schema_means_no_re_ask(self, run) -> None:
        fake = run(completed(claude_envelope("prose is fine here")))
        ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert len(fake.calls) == 1

    def test_a_failed_call_is_not_re_asked(self, run) -> None:
        fake = run(completed("", rc=3, stderr="boom"))
        ClaudeJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert len(fake.calls) == 1

    def test_re_ask_can_be_disabled(self, run) -> None:
        fake = run(completed(claude_envelope("prose")))
        judge = ClaudeJudge(retry_on_schema_miss=False)
        assert judge.judge(JudgeRequest.of("p", response_schema=PING_SCHEMA)).attempts == 1
        assert len(fake.calls) == 1

    def test_codex_re_asks_too(self, run) -> None:
        fake = run(codex_writes("no json here"), codex_writes('{"ping": "pong"}'))
        response = CodexJudge().judge(JudgeRequest.of("ping", response_schema=PING_SCHEMA))
        assert response.parsed == {"ping": "pong"}
        assert len(fake.calls) == 2


class TestEvidenceRecording:
    def test_a_full_exchange_lands_in_the_stock(self, run, tmp_path: Path) -> None:
        run(completed(claude_envelope('{"ping": "pong"}')))
        store = JsonlEvidenceStore(tmp_path)
        recorder = SignalRecorder(store=store, span_id="hero", turn_id="t1")
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG")
        request = JudgeRequest.of("Critique this.", images=[shot], response_schema=PING_SCHEMA)
        ClaudeJudge(recorder=recorder).judge(request)
        (signal,) = store.signals_for_span("hero", turn_id="t1")
        assert signal.source == "judge"
        assert signal.kind == "judge-response"
        assert signal.payload["family"] == "claude"
        assert signal.payload["request"]["prompt"] == "Critique this."
        assert signal.payload["request"]["image_paths"] == [str(tmp_path / "shot.png")]
        assert signal.payload["request"]["response_schema"] == PING_SCHEMA
        assert signal.payload["response"]["raw"] == '{"ping": "pong"}'
        assert signal.payload["response"]["parsed"] == {"ping": "pong"}
        assert signal.payload["attempts"][0]["transport_detail"]["transport"] == "cli"

    def test_an_empty_schema_is_recorded_as_a_schema_not_as_none(self, run, tmp_path: Path) -> None:
        run(completed(claude_envelope("{}")))
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge(recorder=SignalRecorder(store, "hero", "t1")).judge(
            JudgeRequest.of("ping", response_schema={})
        )
        (signal,) = store.signals_for_turn("t1")
        assert signal.payload["request"]["response_schema"] == {}

    def test_a_failure_is_recorded_under_its_own_kind(self, run, tmp_path: Path) -> None:
        run(subprocess.TimeoutExpired(cmd="claude", timeout=1))
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge(recorder=SignalRecorder(store, "hero", "t1")).judge(
            JudgeRequest.of("ping", timeout_s=1)
        )
        (signal,) = store.signals_for_turn("t1")
        assert signal.kind == "judge-error"
        assert signal.payload["response"]["rc"] == RC_TIMEOUT

    def test_every_attempt_records_the_prompt_it_sent_and_the_reply_it_got(
        self, run, tmp_path: Path
    ) -> None:
        run(
            completed(claude_envelope("that is a lovely hero, honestly")),
            completed(claude_envelope('{"ping": "x"}')),
        )
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge(recorder=SignalRecorder(store, "hero", "t1")).judge(
            JudgeRequest.of("Rate the hero.", response_schema=PING_SCHEMA)
        )
        (signal,) = store.signals_for_turn("t1")
        first, second = signal.payload["attempts"]
        assert first["prompt_sent"].startswith("Rate the hero.")
        assert first["raw"] == "that is a lovely hero, honestly"
        assert "ONLY valid JSON" in second["prompt_sent"]
        assert "that is a lovely hero, honestly" in second["prompt_sent"]
        assert second["raw"] == '{"ping": "x"}'
        assert second["prompt_sent"] != first["prompt_sent"]
        assert all(attempt["duration_s"] >= 0 for attempt in (first, second))
        assert signal.payload["response"]["attempts"] == 2

    def test_a_long_reply_is_recorded_whole(self, run, tmp_path: Path) -> None:
        verbose = "the spacing is wrong. " * 500
        run(completed("", rc=1, stderr=verbose))
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge(recorder=SignalRecorder(store, "hero", "t1")).judge(JudgeRequest.of("ping"))
        (signal,) = store.signals_for_turn("t1")
        assert signal.payload["attempts"][0]["transport_detail"]["stderr"] == verbose
        assert signal.payload["response"]["raw"] == verbose

    def test_no_recorder_means_no_stock_writes(self, run, tmp_path: Path) -> None:
        run(completed(claude_envelope("ok")))
        store = JsonlEvidenceStore(tmp_path)
        ClaudeJudge().judge(JudgeRequest.of("ping"))
        assert store.turn_ids() == ()


class TestPortConformance:
    def test_each_family_satisfies_the_port(self) -> None:
        for judge in (ClaudeJudge(), CodexJudge(), GeminiJudge()):
            assert isinstance(judge, JudgePort)

    def test_the_default_panel_is_three_distinct_families(self) -> None:
        panel = cli_judges.default_panel()
        assert families(panel) == ("claude", "codex", "gemini")
        assert len(panel) == len(families(panel))

    def test_scripted_judge_is_a_usable_double(self) -> None:
        scripted = ScriptedJudge(
            family="fake", responses=(JudgeResponse(family="fake", raw="{}", parsed={}),)
        )
        assert isinstance(scripted, JudgePort)
        assert scripted.judge(JudgeRequest.of("q")).parsed == {}
        assert scripted.judge(JudgeRequest.of("q")).rc == RC_UNAVAILABLE
        assert len(scripted.calls) == 2

    def test_a_request_rejects_a_nonsense_timeout(self) -> None:
        with pytest.raises(ValueError):
            JudgeRequest.of("q", timeout_s=0)

    def test_a_request_rejects_an_empty_prompt(self) -> None:
        with pytest.raises(ValueError):
            JudgeRequest.of("   ")

    def test_image_paths_are_normalised_to_paths(self) -> None:
        request = JudgeRequest.of("q", images=["/tmp/a.png"])
        assert request.image_paths == (Path("/tmp/a.png"),)

    def test_availability_check_does_not_run_anything(self, tmp_path: Path) -> None:
        present = tmp_path / "claude"
        present.write_text("#!/bin/sh\n", encoding="utf-8")
        judges = (ClaudeJudge(binary=present), CodexJudge(binary=tmp_path / "absent"))
        assert cli_judges.available(judges) == ("claude",)

    def test_gemini_counts_as_available_on_the_bridge_alone(self, bridge, tmp_path: Path) -> None:
        fake, _ = bridge()
        judge = GeminiJudge(binary=tmp_path / "absent", health_url="http://localhost:4401/healthz")
        assert cli_judges.available((judge,)) == ("gemini",)
        assert fake.gets == ["http://localhost:4401/healthz"]

    def test_gemini_counts_as_available_on_the_cli_alone(self, bridge, tmp_path: Path) -> None:
        bridge(health_error=httpx.ConnectError("refused"))
        installed = tmp_path / "agy"
        installed.write_text("#!/bin/sh\n", encoding="utf-8")
        assert cli_judges.available((GeminiJudge(binary=installed),)) == ("gemini",)

    def test_gemini_is_unavailable_only_when_both_transports_are_gone(
        self, bridge, tmp_path: Path
    ) -> None:
        bridge(health_status=503)
        assert cli_judges.available((GeminiJudge(binary=tmp_path / "absent"),)) == ()


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("UI_SERVO_LIVE") != "1", reason="live panel smoke; set UI_SERVO_LIVE=1"
)
@pytest.mark.parametrize(
    "judge",
    [ClaudeJudge(), CodexJudge(), GeminiJudge()],
    ids=["claude", "codex", "gemini"],
)
def test_live_family_answers_a_trivial_schema(judge: JudgePort) -> None:
    """Each real family, one trivial structured question.

    The assertion is about the *plumbing*, not the model: envelopes, flags and
    the bridge protocol are all owned by tools that update themselves, and this
    is the only test that would notice when one of them changes shape.
    """
    response = judge.judge(
        JudgeRequest.of(
            'Reply with the JSON object {"ping": "pong"} and nothing else.',
            response_schema=PING_SCHEMA,
            timeout_s=120,
        )
    )
    assert response.rc == RC_OK, f"{judge.family} rc={response.rc}: {response.raw[:500]}"
    assert response.parsed is not None, f"{judge.family} returned unparseable: {response.raw[:500]}"
    assert response.parsed.get("ping") == "pong", response.parsed
