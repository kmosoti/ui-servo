"""Three judges from three families, riding the CLIs already logged in on this box.

The panel needs decorrelated critics (see :mod:`ui_servo.ports.judge`), and the
cheapest honest source of decorrelation available to a personal project is the
set of agent CLIs the owner is already authenticated against: Anthropic's
``claude``, OpenAI's ``codex`` and Google's ``agy``. Shelling out buys three
genuinely different weight families, three different system prompts and three
different refusal styles for no API key, no billing setup and no secret in the
repository -- the credentials stay in each vendor's own config, and this package
never learns them.

The cost of that choice is that a "model call" is now a process, and processes
fail in ways an SDK does not: a binary is missing, a login expired, a bridge
daemon is down, a wrapper prints its banner to stdout. Every one of those is
translated into a :class:`~ui_servo.ports.judge.JudgeResponse` with a non-zero
``rc``, because a panel that raises on one family loses the whole round's
variety to a transient failure in one third of it.

Two behaviours are shared across the families and both exist because CLI agents
are chat surfaces, not JSON endpoints. :func:`extract_json` digs a structured
answer out of whatever prose, fence or banner it arrived wrapped in; and when a
schema was requested and nothing parseable came back, the judge re-asks exactly
once with the failed reply quoted and an explicit demand for bare JSON. One
retry rather than a loop: a family that cannot produce JSON twice is telling the
panel something, and burning further turns on it would spend the round's budget
on the least informative critic.
"""

import re
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self

import httpx
import orjson

from ui_servo.domain.evidence import Signal, SpanId, TurnId
from ui_servo.ports.judge import (
    RC_OK,
    RC_TIMEOUT,
    RC_TRANSPORT,
    RC_UNAVAILABLE,
    Family,
    JudgePort,
    JudgeRequest,
    JudgeResponse,
    Prompt,
)
from ui_servo.ports.store import EvidenceStorePort

CLAUDE_BIN: Final[Path] = Path.home() / ".local" / "bin" / "claude"
CODEX_BIN: Final[Path] = Path.home() / ".codex" / "packages" / "standalone" / "current" / "codex"
AGY_BIN: Final[Path] = Path.home() / ".local" / "bin" / "agy"
AGY_BRIDGE_URL: Final[str] = "http://localhost:4401/prompt"
AGY_BRIDGE_HEALTH_URL: Final[str] = "http://localhost:4401/healthz"
AGY_BRIDGE_TOKEN: Final[Path] = Path.home() / ".config" / "agy-bridge" / "token"

CLAUDE_RESTRICTIONS: Final[tuple[str, ...]] = (
    "--permission-mode",
    "plan",
    "--disallowedTools",
    "Bash,Edit,Write,NotebookEdit",
)
"""Read-only critique, enforced by the CLI rather than by the prompt.

These are agentic CLIs with the owner's ambient permissions, and a judge is fed
exactly the kind of input an attacker controls: markup and screenshots produced
by a builder that may itself have been prompt-injected. Without the flags, "the
fragment under review told the critic to run a command" is a working exploit
against the developer's machine. Plan mode plus an explicit denial of every
mutating tool keeps a critic able to read the artefacts it is judging and unable
to change them -- which is also the Gauntlet Loop's rule that a critic never
becomes an implementer, enforced by the process boundary instead of by manners.
"""

AGY_RESTRICTIONS: Final[tuple[str, ...]] = ("--mode", "plan", "--sandbox")
"""The same restriction for the agy fallback: plan mode, terminal sandboxed.

The bridge path cannot be constrained from here -- the daemon's own
configuration decides what that session may do -- so the fallback is the
narrower of the two transports, not the wider one.
"""

JUDGE_SOURCE: Final[str] = "judge"
RESPONSE_KIND: Final[str] = "judge-response"
ERROR_KIND: Final[str] = "judge-error"

_FENCE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\s*\n(.*?)(?:\n)?```", re.DOTALL)

_IMAGE_HEADER: Final[str] = (
    "Read these image files from the local filesystem before answering "
    "(absolute paths, they exist on this machine):"
)
_SCHEMA_INSTRUCTION: Final[str] = (
    "Return ONLY a single JSON object conforming to this JSON Schema. "
    "No preamble, no explanation, no markdown code fence.\n\nJSON Schema:\n{schema}"
)
_REASK_INSTRUCTION: Final[str] = (
    "Your previous reply could not be parsed as JSON.\n\n"
    "--- previous reply ---\n{raw}\n--- end previous reply ---\n\n"
    "{original}\n\n"
    "Return ONLY valid JSON matching the schema above. "
    "Start your reply with '{{' and end it with '}}'. Nothing else."
)


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every top-level ``{...}`` span, quote- and escape-aware.

    A brace counter that cannot see strings will close an object early on a
    ``"}"`` inside a rubric comment, which is exactly the kind of text a critic
    writes; the extra state is what makes the extractor safe on real prose.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            match character:
                case _ if escaped:
                    escaped = False
                case "\\":
                    escaped = True
                case '"':
                    in_string = False
            continue
        match character:
            case '"' if depth:
                in_string = True
            case "{":
                if depth == 0:
                    start = index
                depth += 1
            case "}" if depth:
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
    return


def _candidates(text: str) -> Iterator[str]:
    stripped = text.strip()
    if stripped:
        yield stripped
    for block in _FENCE.findall(text):
        candidate = block.strip()
        if candidate:
            yield candidate
    yield from _balanced_objects(text)


def _object_spans(text: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for candidate in _candidates(text):
        try:
            value = orjson.loads(candidate)
        except orjson.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found.append((candidate, value))
    return found


def extract_json(text: str) -> dict[str, Any] | None:
    """The first JSON *object* recoverable from *text*, or ``None``.

    Tries the whole reply, then fenced blocks, then balanced brace spans, in
    that order: the cheapest and least ambiguous reading wins, and a family that
    answered cleanly is never punished for the habits of one that did not.
    """
    spans = _object_spans(text)
    return spans[0][1] if spans else None


def last_json_span(text: str) -> str | None:
    """The *last* JSON object in *text*, returned as its verbatim substring.

    For transcripts rather than replies. A CLI that echoes the prompt before the
    answer puts the requested schema on stdout ahead of anything the model said,
    so first-match extraction there would parse the question as the answer --
    a failure that looks exactly like a valid, empty critique.
    """
    spans = _object_spans(text)
    return spans[-1][0] if spans else None


def resolved_images(request: JudgeRequest) -> tuple[Path, ...]:
    """Every image path made absolute, symlinks and ``..`` collapsed.

    The prompt tells the critic these are absolute paths that exist on this
    machine, so they had better be: a relative path interpolated verbatim is
    resolved against whatever working directory the CLI happened to inherit,
    which is a different file or no file at all.
    """
    return tuple(path.resolve() for path in request.image_paths)


def missing_images(request: JudgeRequest) -> tuple[Path, ...]:
    """Resolved image paths that are not files on this machine.

    Checked before spending a call, because a critic asked to look at a
    screenshot that is not there does not error -- it answers anyway, from the
    text alone, and a confident review of an image nobody saw is the most
    expensive kind of wrong a panel can produce.
    """
    return tuple(path for path in resolved_images(request) if not path.is_file())


def compose_prompt(request: JudgeRequest) -> Prompt:
    """The prompt as it is actually sent: question, image paths, schema demand."""
    parts = [request.prompt]
    if request.image_paths:
        listed = "\n".join(f"- {path}" for path in resolved_images(request))
        parts.append(f"{_IMAGE_HEADER}\n{listed}")
    if request.response_schema is not None:
        schema = orjson.dumps(dict(request.response_schema), option=orjson.OPT_INDENT_2).decode()
        parts.append(_SCHEMA_INSTRUCTION.format(schema=schema))
    return "\n\n".join(parts)


def reask_prompt(original: Prompt, raw: str) -> Prompt:
    """The single retry: quote what failed, then demand bare JSON."""
    return _REASK_INSTRUCTION.format(raw=raw.strip(), original=original)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One round trip to one family, before any schema parsing.

    Carries the prompt it sent as well as the reply it got, because the second
    attempt of a schema re-ask is a *different* question and a stock that
    recorded only the first would misattribute the answer.
    """

    raw: str
    rc: int = RC_OK
    prompt: Prompt = ""
    duration_s: float = 0.0
    detail: Mapping[str, Any] = field(default_factory=dict)

    def with_context(self, *, prompt: Prompt, duration_s: float) -> Self:
        return type(self)(
            raw=self.raw, rc=self.rc, prompt=prompt, duration_s=duration_s, detail=self.detail
        )

    def describe(self) -> Mapping[str, Any]:
        """The attempt as plain data, verbatim.

        Nothing here is truncated. The evidence stock is untruncated by design
        (see :mod:`ui_servo.domain.evidence`) -- a critique clipped at some
        arbitrary column is exactly the one whose reasoning you needed when the
        panel later disagrees with itself, and bounding the stock is the job of
        turn rotation, not of the writer.
        """
        return {
            "rc": self.rc,
            "prompt_sent": self.prompt,
            "raw": self.raw,
            "duration_s": self.duration_s,
            "transport_detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class SignalRecorder:
    """Files a judge exchange into the evidence stock as a ``judge`` signal.

    The whole exchange goes in -- prompt, schema, raw reply, parsed reply, rc,
    duration, attempt count -- because a panel verdict whose prompt has been
    thrown away cannot be re-read once the model behind a family changes under
    it, and an unreadable verdict is an opinion rather than evidence.
    """

    store: EvidenceStorePort
    span_id: SpanId = "panel"
    turn_id: TurnId = "adhoc"

    def record(
        self,
        family: Family,
        request: JudgeRequest,
        response: JudgeResponse,
        attempts: Sequence[Attempt] = (),
    ) -> Signal:
        signal = Signal(
            span_id=self.span_id,
            turn_id=self.turn_id,
            source=JUDGE_SOURCE,
            kind=RESPONSE_KIND if response.ok else ERROR_KIND,
            ts=datetime.now(UTC).isoformat(),
            payload={
                "family": family,
                "request": dict(request.describe()),
                "response": dict(response.describe()),
                "attempts": [dict(attempt.describe()) for attempt in attempts],
            },
        )
        self.store.append(signal)
        return signal


@dataclass(frozen=True, slots=True)
class _Process:
    rc: int
    stdout: str
    stderr: str


def _run(argv: Sequence[str], *, timeout_s: int, stdin_text: str | None = None) -> _Process:
    """Run *argv* to completion, turning every startup failure into an ``rc``.

    Prompts are handed over on stdin wherever the CLI accepts them there, and
    never as a trailing positional argument. A judge prompt contains attacker-
    influenced text; as an argument, a reply beginning ``--help`` or ``--version``
    is parsed as flags, and the process then exits 0 having never spoken to a
    model -- a panel that reads that as a successful, empty critique has been
    silenced without noticing. On stdin there is no argument parser to confuse,
    and no ``ARG_MAX`` ceiling on how much markup a critic can be shown.

    When nothing is piped, stdin is closed rather than inherited: these CLIs read
    stdin when it is a pipe, and a judge blocked forever on input that will never
    arrive holds the panel's slot without ever producing a verdict.
    """
    piped: dict[str, Any] = (
        {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    )
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            **piped,
        )
    except subprocess.TimeoutExpired as expired:
        return _Process(rc=RC_TIMEOUT, stdout=_decode(expired.stdout), stderr=_decode(expired.stderr))
    except OSError as error:
        return _Process(rc=RC_UNAVAILABLE, stdout="", stderr=str(error))
    return _Process(rc=completed.returncode, stdout=completed.stdout or "", stderr=completed.stderr or "")


def _decode(value: str | bytes | None) -> str:
    match value:
        case None:
            return ""
        case bytes() | bytearray():
            return value.decode("utf-8", errors="replace")
        case _:
            return value


class _CliJudge:
    """Shared skeleton: compose, invoke, parse, re-ask once, record.

    Subclasses supply only :meth:`_invoke` -- the family-specific transport and
    envelope. Everything the panel depends on (totality, one retry, evidence)
    lives here so that adding a fourth family cannot accidentally opt out of it.
    """

    family: Family = "unset"

    def __init__(
        self, *, recorder: SignalRecorder | None = None, retry_on_schema_miss: bool = True
    ) -> None:
        self.recorder = recorder
        self.retry_on_schema_miss = retry_on_schema_miss

    def judge(self, request: JudgeRequest) -> JudgeResponse:
        started = time.perf_counter()
        absent = missing_images(request)
        if absent:
            return self._refuse(
                request,
                rc=RC_UNAVAILABLE,
                raw="cannot judge: image file(s) not found: "
                + ", ".join(str(path) for path in absent),
                duration_s=time.perf_counter() - started,
            )
        prompt = compose_prompt(request)
        attempts: list[Attempt] = [self._safe_invoke(prompt, request)]
        parsed = self._parse(attempts[-1], request)
        if self._should_reask(attempts[-1], parsed, request):
            attempts.append(self._safe_invoke(reask_prompt(prompt, attempts[-1].raw), request))
            parsed = self._parse(attempts[-1], request)
        last = attempts[-1]
        response = JudgeResponse(
            family=self.family,
            raw=last.raw,
            parsed=parsed,
            rc=last.rc,
            duration_s=time.perf_counter() - started,
            attempts=len(attempts),
        )
        if self.recorder is not None:
            self.recorder.record(self.family, request, response, attempts)
        return response

    def _refuse(
        self, request: JudgeRequest, *, rc: int, raw: str, duration_s: float
    ) -> JudgeResponse:
        """Decline before spending a call, still as a response and still recorded."""
        response = JudgeResponse.failure(self.family, rc=rc, raw=raw, duration_s=duration_s)
        attempt = Attempt(raw=raw, rc=rc, prompt="", duration_s=duration_s, detail={"sent": False})
        if self.recorder is not None:
            self.recorder.record(self.family, request, response, (attempt,))
        return response

    def _safe_invoke(self, prompt: Prompt, request: JudgeRequest) -> Attempt:
        """No adapter bug may become an exception the panel has to survive."""
        started = time.perf_counter()
        try:
            attempt = self._invoke(prompt, request)
        except Exception as error:  # noqa: BLE001 - totality is the port's contract
            attempt = Attempt(
                raw=f"{type(error).__name__}: {error}",
                rc=RC_TRANSPORT,
                detail={"family": self.family, "error": type(error).__name__},
            )
        return attempt.with_context(prompt=prompt, duration_s=time.perf_counter() - started)

    def available(self) -> bool:
        """Whether this family's transport exists here, without running anything."""
        return Path(getattr(self, "binary", "")).exists()

    def _parse(self, attempt: Attempt, request: JudgeRequest) -> dict[str, Any] | None:
        if not request.wants_json:
            return None
        return extract_json(attempt.raw)

    def _should_reask(
        self, attempt: Attempt, parsed: Mapping[str, Any] | None, request: JudgeRequest
    ) -> bool:
        return (
            self.retry_on_schema_miss
            and request.wants_json
            and parsed is None
            and attempt.rc == RC_OK
        )

    def _invoke(self, prompt: Prompt, request: JudgeRequest) -> Attempt:
        raise NotImplementedError


class ClaudeJudge(_CliJudge):
    """Anthropic family, via ``claude -p ... --output-format json``.

    The CLI wraps the reply in a result envelope carrying cost, usage and an
    ``is_error`` flag; ``result`` is unwrapped as the reply and ``is_error`` is
    honoured, because a CLI that exits 0 having refused is a failed judgement,
    not a successful one.

    Runs under :data:`CLAUDE_RESTRICTIONS` with the prompt on stdin.
    """

    family: Family = "claude"

    def __init__(self, binary: Path | str = CLAUDE_BIN, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.binary = Path(binary)

    def _invoke(self, prompt: Prompt, request: JudgeRequest) -> Attempt:
        argv = [str(self.binary), *CLAUDE_RESTRICTIONS, "--output-format", "json", "-p"]
        process = _run(argv, timeout_s=request.timeout_s, stdin_text=prompt)
        detail: dict[str, Any] = {"transport": "cli", "binary": str(self.binary)}
        if process.rc != RC_OK:
            return Attempt(
                raw=process.stdout or process.stderr,
                rc=process.rc,
                detail={**detail, "stderr": process.stderr},
            )
        envelope = extract_json(process.stdout)
        if envelope is None:
            return Attempt(raw=process.stdout, rc=RC_TRANSPORT, detail={**detail, "envelope": False})
        text = envelope.get("result")
        rc = RC_TRANSPORT if envelope.get("is_error") or not isinstance(text, str) else RC_OK
        return Attempt(
            raw=text if isinstance(text, str) else process.stdout,
            rc=rc,
            detail={
                **detail,
                "envelope": True,
                "subtype": envelope.get("subtype"),
                "session_id": envelope.get("session_id"),
                "duration_ms": envelope.get("duration_ms"),
            },
        )


class CodexJudge(_CliJudge):
    """OpenAI family, via ``codex exec -s read-only --skip-git-repo-check``.

    ``-o`` is what makes this parseable: stdout carries a banner, the session
    transcript and a token count, while the file receives the final message
    alone. With a schema, ``--output-schema`` constrains generation server-side,
    so the shared extractor is a second line of defence rather than the first.
    The prompt is passed as ``-`` on stdin, so no reply text can be read as a
    flag. An empty final-message file after a clean exit is treated as a failed
    call rather than an empty critique -- the only exception being a schema run
    whose answer can still be recovered from the transcript's *last* JSON object,
    which is the model's, unlike the first, which is the echoed schema.

    The sandbox flags are not incidental. A critic is asked to look at a
    screenshot and a fragment and say what is wrong; giving it write access to
    the repository it is judging would let a critic silently become an
    implementer, which is the one invariant of the Gauntlet Loop.
    """

    family: Family = "codex"

    def __init__(self, binary: Path | str = CODEX_BIN, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.binary = Path(binary)

    def _invoke(self, prompt: Prompt, request: JudgeRequest) -> Attempt:
        with tempfile.TemporaryDirectory(prefix="ui-servo-codex-") as workspace:
            outfile = Path(workspace) / "final-message.txt"
            argv = [
                str(self.binary),
                "exec",
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "-o",
                str(outfile),
            ]
            detail: dict[str, Any] = {"transport": "cli", "binary": str(self.binary)}
            if request.response_schema is not None:
                schema_path = Path(workspace) / "schema.json"
                schema_path.write_bytes(orjson.dumps(dict(request.response_schema)))
                argv += ["--output-schema", str(schema_path)]
                detail["output_schema"] = True
            argv.append("-")
            process = _run(argv, timeout_s=request.timeout_s, stdin_text=prompt)
            final = _read_text(outfile)
        if process.rc != RC_OK:
            return Attempt(
                raw=final or process.stdout or process.stderr,
                rc=process.rc,
                detail={**detail, "stderr": process.stderr},
            )
        if final.strip():
            return Attempt(raw=final, rc=RC_OK, detail={**detail, "final_message_file": True})
        salvaged = last_json_span(process.stdout) if request.response_schema is not None else None
        return Attempt(
            raw=salvaged if salvaged is not None else (process.stdout or process.stderr),
            rc=RC_OK if salvaged is not None else RC_TRANSPORT,
            detail={
                **detail,
                "final_message_file": False,
                "salvaged_from_stdout": salvaged is not None,
                "stdout": process.stdout,
            },
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class GeminiJudge(_CliJudge):
    """Google family, via the local agy bridge with the CLI as a fallback.

    The bridge is preferred because it answers with a structured NDJSON stream
    whose final ``result`` event carries the response verbatim, which is a far
    better parse target than a TUI's stdout. It is a local daemon, though, so it
    is also the component most likely to be down; when the socket refuses, the
    judge drops to ``agy -p`` rather than dropping the family, since losing
    Google entirely costs the panel a third of its variety.

    Only failures to *reach* the bridge fall back -- a refused connection or a
    connect timeout, both of which mean nothing was spent. A read timeout is the
    opposite situation: the model is working and re-asking it through a second
    transport would pay twice for one answer.

    The fallback runs under :data:`AGY_RESTRICTIONS`.
    """

    family: Family = "gemini"

    def __init__(
        self,
        bridge_url: str = AGY_BRIDGE_URL,
        token_path: Path | str = AGY_BRIDGE_TOKEN,
        binary: Path | str = AGY_BIN,
        health_url: str = AGY_BRIDGE_HEALTH_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.bridge_url = bridge_url
        self.token_path = Path(token_path)
        self.binary = Path(binary)
        self.health_url = health_url

    def _invoke(self, prompt: Prompt, request: JudgeRequest) -> Attempt:
        token = _read_text(self.token_path).strip()
        if not token:
            return self._via_cli(prompt, request, reason="no-bridge-token")
        try:
            body = self._via_bridge(prompt, token, request.timeout_s)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return self._via_cli(prompt, request, reason="bridge-unreachable")
        except httpx.TimeoutException:
            return Attempt(raw="agy bridge timed out", rc=RC_TIMEOUT, detail={"transport": "bridge"})
        except httpx.HTTPError as error:
            return Attempt(
                raw=f"agy bridge error: {error}", rc=RC_TRANSPORT, detail={"transport": "bridge"}
            )
        match body:
            case None:
                return self._via_cli(prompt, request, reason="bridge-rejected")
            case text:
                return _bridge_attempt(text)

    def _via_bridge(self, prompt: Prompt, token: str, timeout_s: int) -> str | None:
        """POST the prompt to the local daemon, deliberately ignoring the environment.

        ``trust_env=False`` because this call carries a bearer token and the
        critique prompt to ``localhost``: with the default, an ``HTTP_PROXY`` or
        ``ALL_PROXY`` in the shell silently re-routes both through a third party,
        which is an exfiltration channel opened by an environment variable.
        """
        with httpx.Client(timeout=timeout_s, trust_env=False) as client:
            response = client.post(
                self.bridge_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"prompt": prompt},
            )
        if response.status_code >= 400:
            return None
        return response.text

    def _via_cli(self, prompt: Prompt, request: JudgeRequest, *, reason: str) -> Attempt:
        argv = [str(self.binary), *AGY_RESTRICTIONS, "-p", prompt]
        process = _run(argv, timeout_s=request.timeout_s)
        detail = {"transport": "cli", "fallback_reason": reason, "binary": str(self.binary)}
        if process.rc != RC_OK:
            return Attempt(
                raw=process.stdout or process.stderr,
                rc=process.rc,
                detail={**detail, "stderr": process.stderr},
            )
        return Attempt(raw=process.stdout, rc=RC_OK, detail=detail)

    def bridge_healthy(self, timeout_s: float = 1.0) -> bool:
        """Whether the daemon answers its unauthenticated health route."""
        try:
            with httpx.Client(timeout=timeout_s, trust_env=False) as client:
                return client.get(self.health_url).status_code < 400
        except httpx.HTTPError:
            return False

    def available(self) -> bool:
        """Either transport suffices: the family is lost only if both are gone."""
        return self.bridge_healthy() or self.binary.exists()


def _ndjson(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = orjson.loads(stripped)
        except orjson.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _bridge_attempt(text: str) -> Attempt:
    """Read the agy stream: the last ``result`` event, then the bridge trailer.

    The trailer is checked *after* the result because the bridge reports its own
    health separately from the agent's: a non-zero trailer with a good result
    means the wrapper stumbled on the way out, not that the critic failed.
    """
    result: dict[str, Any] | None = None
    trailer: dict[str, Any] | None = None
    for record in _ndjson(text):
        if record.get("event") == "result" and isinstance(record.get("result"), dict):
            result = record["result"]
        elif "bridge_status" in record:
            trailer = record
    detail: dict[str, Any] = {"transport": "bridge"}
    if trailer is not None:
        detail |= {"bridge_status": trailer.get("bridge_status"), "bridge_rc": trailer.get("rc")}
    if result is None:
        return Attempt(raw=text, rc=RC_TRANSPORT, detail={**detail, "result_event": False})
    response = result.get("response")
    status = result.get("status")
    detail |= {"result_event": True, "status": status, "conversation_id": result.get("conversation_id")}
    if not isinstance(response, str):
        return Attempt(raw=text, rc=RC_TRANSPORT, detail=detail)
    rc = RC_OK if status in (None, "SUCCESS") else RC_TRANSPORT
    return Attempt(raw=response, rc=rc, detail=detail)


def default_panel(recorder: SignalRecorder | None = None) -> tuple[JudgePort, ...]:
    """One judge per family, in a fixed order.

    Fixed so that a rotation policy upstream has something stable to rotate, and
    one-per-family so that the panel's size is its variety rather than its cost.
    """
    return (
        ClaudeJudge(recorder=recorder),
        CodexJudge(recorder=recorder),
        GeminiJudge(recorder=recorder),
    )


def available(judges: Iterable[JudgePort]) -> tuple[Family, ...]:
    """Families whose transport is reachable, asked cheaply and without a prompt.

    A family with *any* working transport counts, because the panel's cost is
    measured in variety and dropping Gemini for a stopped daemon while the CLI
    is installed would discard a third of it for nothing.
    """
    present: list[Family] = []
    for judge in judges:
        probe = getattr(judge, "available", None)
        if probe is None or probe():
            present.append(judge.family)
    return tuple(present)
