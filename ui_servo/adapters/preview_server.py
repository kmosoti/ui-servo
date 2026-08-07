"""The preview server: a candidate fragment, in a real browser, wired to the loop.

This adapter exists so that "does the browser accept this?" is answered by a
browser rather than by a model reading its own output. It serves one generated
fragment at a time inside the *smallest shell that is still honest*: the token
sheet the contract emits, the probe, and nothing else. No framework CSS, no
reset the site does not ship, no helpful wrapper markup -- every byte the shell
adds that production would not is a way for the preview to pass while the site
fails.

The shell closes the loop in one page load:

    contract -> tokens.css (inline) -> the fragment renders on-contract
    contract -> motion table -> window.__UI_SERVO__ -> the probe judges motion
    probe    -> POST /beacon -> EvidenceStorePort -> the gates and the panel read it

Two things about the shell are load-bearing rather than cosmetic:

* **The probe goes first.** ``probe.js`` shims ``customElements.define`` and
  installs its ``error`` listener at load, so anything the candidate brings with
  it must run *after* the sensor or its failures are invisible to the sensor that
  exists to see them. The order is tokens, configuration, probe, then whatever
  the page had in its own ``<head>``.
* **The document is parsed, never pattern-matched.** A candidate is generated
  markup: it can contain ``<body>`` inside a script string, a comment, or an
  attribute. :func:`split_document` uses :mod:`html.parser`, which handles those
  the way the browser will, because a shell that mangles a fragment turns a
  passing candidate into a failing one for a reason no evidence will explain.

``dev`` is the one switch. With it on, the shell injects the probe and its
configuration; with it off the same route serves the same markup *unsensed*, so a
screenshot or a pixel diff is taken of the fragment and not of the instrument
watching it. Nothing else changes between the two, which is what makes the sensed
and unsensed views comparable at all.

Everything the server needs is injected: the contract (the setpoint), the store
(the stock) and the turn id (the sampling interval). The app never reads a
config file, so a test drives it with a temporary directory and the CLI at the
bottom of this file is just one caller among several.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

import orjson
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from ui_servo.adapters.beacon_ingest import (
    BEACON_PATH,
    IngestHealth,
    create_beacon_router,
    validate_turn_id,
)
from ui_servo.domain.contract import DirectionContract, MotionTable
from ui_servo.domain.evidence import TurnId
from ui_servo.ports.store import EvidenceStorePort

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PROBE_JS: Final[Path] = REPO_ROOT / "probe" / "probe.js"
"""The authored probe, in a source checkout. Not the only place it may live: see
:func:`probe_source`."""

PACKAGED_PROBE: Final[tuple[str, str, str]] = ("ui_servo.adapters", "data", "probe.js")
"""Where ``hatch`` force-includes ``probe/probe.js`` into the wheel.

One authored file, two locations, copied at build time so they cannot drift --
the same arrangement ``ui_servo.domain.contract`` uses for the contract itself.
"""

FIXTURE_HTML: Final[Path] = REPO_ROOT / "tests" / "fixtures" / "probe.html"
"""The sensor rig. Deliberately *not* packaged: ``/fixture`` verifies the probe
against a checkout and has no meaning in a deployed wheel."""

PROBE_URL: Final[str] = "/assets/probe.js"
TOKENS_URL: Final[str] = "/assets/tokens.css"
CANDIDATE_SUFFIXES: Final[tuple[str, ...]] = (".html", ".htm", "")
"""Tried in order, so ``/candidate/hero`` finds ``hero.html`` and
``/candidate/hero.html`` finds it too -- the name in the URL is the name a
builder wrote, not a filename."""

_SAFE_CANDIDATE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PROBE_SRC: Final[re.Pattern[str]] = re.compile(r"(^|/)probe(\.min)?\.js(\?.*)?$")


class AssetMissing(RuntimeError):
    """A file the shell promises the browser is not on this machine."""


# --------------------------------------------------------------------- assets ---


def probe_source(override: Path | None = None) -> str:
    """The probe's source, from *override*, the wheel, or the checkout.

    Read rather than resolved to a path, because the packaged copy may live
    inside a zip where there is no path to hand out. Read on every call rather
    than cached, because the probe is edited while a preview is open and a stale
    sensor is worse than a slow one.
    """
    origins = probe_origins(override)
    for origin in origins:
        try:
            return origin.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
            continue
    raise AssetMissing(
        f"cannot find probe.js; looked at {[str(origin) for origin in origins]}. "
        "Install the package (the wheel carries a copy) or run from a checkout."
    )


def probe_origins(override: Path | None = None) -> tuple[Any, ...]:
    """Where the probe may live, most explicit first."""
    found: list[Any] = []
    if override is not None:
        found.append(override)
    else:
        match os.environ.get("UI_SERVO_PROBE_JS"):
            case None | "":
                pass
            case path:
                found.append(Path(path))
        package, *parts = PACKAGED_PROBE
        try:
            resource = files(package)
            for part in parts:
                resource = resource / part
            found.append(resource)
        except (ModuleNotFoundError, TypeError):  # pragma: no cover - import-time only
            pass
        found.append(PROBE_JS)
    return tuple(found)


# ------------------------------------------------------------------ the shell ---


def motion_config(table: MotionTable) -> dict[str, Any]:
    """:class:`MotionTable` as the probe's ``motion`` block.

    Sorted lists, because the table's fields are ``frozenset``s (which no JSON
    encoder will take) and because an unordered dump would make two identical
    contracts produce two different pages -- which would make a diff of the
    preview meaningless as evidence. Every list is non-empty for a valid
    contract, which is what keeps the probe from emitting
    ``probe-config-incomplete`` against this server.
    """
    return {
        "durations": sorted(table.durations_ms),
        "easings": sorted(table.easings),
        "properties": sorted(table.animatable_properties),
        "reducedMotionRequired": table.reduced_motion_required,
    }


def probe_config(
    contract: DirectionContract, turn_id: TurnId, *, span_id: str | None = None
) -> dict[str, Any]:
    """The whole of ``window.__UI_SERVO__``: setpoint plus where to report."""
    config: dict[str, Any] = {
        "beacon": BEACON_PATH,
        "turnId": turn_id,
        "motion": motion_config(contract.motion_table()),
    }
    if span_id is not None:
        config["spanId"] = span_id
    return config


def _script_json(value: Mapping[str, Any]) -> str:
    """JSON safe to inline in a ``<script>``.

    An HTML parser ends the script at the first ``</script>`` -- inside a string
    literal included -- so a fragment name or an easing containing one would let
    contract data close the tag it was written into.
    """
    return orjson.dumps(dict(value), option=orjson.OPT_SORT_KEYS).decode().replace("</", "<\\/")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_shell(
    *,
    title: str,
    tokens_css: str,
    body_html: str,
    dev: bool,
    config: Mapping[str, Any],
    probe_js: str = PROBE_URL,
    head_extra: str = "",
    body_attrs: str = "",
    span_id: str | None = None,
) -> str:
    """Wrap *body_html* in the minimal document a candidate is judged inside.

    The order of the head is the contract with ``probe/probe.js`` and is not
    negotiable: tokens first so the fragment lays out against the contract's
    values; the servo configuration next, because the probe reads it once at
    load; the probe itself next, so its ``customElements.define`` shim and its
    capture-phase ``error`` listener precede anything that could need them; and
    only then whatever the page brought in its own ``<head>``. A page carrying
    its own ``window.__UI_SERVO__`` (the probe fixture does) therefore cannot
    redirect this server's evidence -- by the time it runs, the probe has already
    read the configuration it was given.
    """
    sensor = (
        f"<script>window.__UI_SERVO__ = {_script_json(config)};</script>\n"
        f'<script src="{probe_js}"></script>'
        if dev
        else "<!-- probe not injected: dev=False -->"
    )
    stamp = "" if span_id is None else f' data-span-id="{_escape(span_id)}"'
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_escape(title)}</title>\n"
        f'<style data-ui-servo="tokens">\n{tokens_css}</style>\n'
        f"{sensor}\n"
        f"{head_extra}\n"
        "</head>\n"
        f"<body{body_attrs}{stamp}>\n{body_html}\n</body>\n"
        "</html>\n"
    )


# ------------------------------------------------------------ document splitting ---


@dataclass(frozen=True, slots=True)
class _Token:
    """One thing the parser saw, and where it started in the source."""

    at: int
    event: str
    tag: str = ""
    attrs: tuple[tuple[str, str | None], ...] = ()


class _Tokenizer(HTMLParser):
    """Every token with its absolute source offset, so nothing is re-guessed.

    Offsets rather than reconstructed markup: the served fragment must be the
    bytes the builder wrote, not this parser's idea of them. And a parser rather
    than a pattern, because ``<body>`` inside a script string or a comment is
    text to a browser and must be text here too -- ``html.parser`` switches to
    CDATA mode inside ``<script>`` and ``<style>`` exactly as the HTML spec says.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.tokens: list[_Token] = []
        self._lines = [0]
        for index, char in enumerate(source):
            if char == "\n":
                self._lines.append(index + 1)
        self.feed(source)
        self.close()

    def _at(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column if line - 1 < len(self._lines) else len(self.source)

    def _mark(self, event: str, tag: str = "", attrs: Sequence[tuple[str, str | None]] = ()) -> None:
        self.tokens.append(_Token(at=self._at(), event=event, tag=tag, attrs=tuple(attrs)))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._mark("start", tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._mark("startend", tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self._mark("end", tag)

    def handle_data(self, data: str) -> None:
        self._mark("data")

    def handle_comment(self, data: str) -> None:
        self._mark("comment")

    def handle_decl(self, decl: str) -> None:
        self._mark("decl")

    def handle_pi(self, data: str) -> None:
        self._mark("pi")

    def unknown_decl(self, data: str) -> None:
        self._mark("decl")

    def handle_entityref(self, name: str) -> None:
        self._mark("data")

    def handle_charref(self, name: str) -> None:
        self._mark("data")

    # -- queries -------------------------------------------------------------

    def end_of(self, index: int) -> int:
        """Where token *index* stops: where the next one starts."""
        following = index + 1
        return self.tokens[following].at if following < len(self.tokens) else len(self.source)

    def find(self, event: str, tag: str, *, after: int = -1) -> int | None:
        for index in range(after + 1, len(self.tokens)):
            token = self.tokens[index]
            if token.event == event and token.tag == tag:
                return index
        return None

    def probe_holes(self) -> tuple[tuple[int, int], ...]:
        """Source ranges holding a ``<script src=...probe.js>`` and its end tag.

        Excised wherever they appear. The shell serves the sensor itself from
        :data:`PROBE_URL`; a page's own copy would either 404 (a phantom
        ``js-error`` in the evidence) or load a second probe, and two probes
        double-report every event they both see.
        """
        holes: list[tuple[int, int]] = []
        for index, token in enumerate(self.tokens):
            if token.tag != "script" or token.event not in {"start", "startend"}:
                continue
            src = next((value for name, value in token.attrs if name == "src"), None)
            if src is None or not _PROBE_SRC.search(src.strip()):
                continue
            if token.event == "startend":
                holes.append((token.at, self.end_of(index)))
                continue
            closing = self.find("end", "script", after=index)
            holes.append((token.at, self.end_of(closing) if closing is not None else len(self.source)))
        return tuple(holes)


def _slice(source: str, start: int, stop: int, holes: Sequence[tuple[int, int]]) -> str:
    kept: list[str] = []
    cursor = start
    for hole_start, hole_stop in holes:
        if hole_stop <= start or hole_start >= stop:
            continue
        kept.append(source[cursor : max(cursor, min(hole_start, stop))])
        cursor = max(cursor, min(hole_stop, stop))
    kept.append(source[cursor:stop])
    return "".join(kept)


def _attributes(attrs: Sequence[tuple[str, str | None]]) -> str:
    """Rebuild a start tag's attributes, quoted and escaped.

    Rebuilt rather than sliced because this is the one place the shell *writes*
    markup around what it was given, so it may not simply trust the source's
    quoting.
    """
    rendered = []
    for name, value in attrs:
        safe = re.sub(r"[^A-Za-z0-9:_.-]", "", name)
        if not safe:
            continue
        rendered.append(safe if value is None else f'{safe}="{_escape(value)}"')
    return "".join(f" {part}" for part in rendered)


def split_document(html: str) -> tuple[str, str, str]:
    """Split a *whole* HTML document into (head extra, body attributes, body).

    Used for ``/fixture`` and for any candidate that was authored as a full page,
    so both are served through the same shell -- otherwise the route that
    verifies the sensor would be verifying a different page from the one the
    sensor is used on. A file that is only a fragment comes back as
    ``("", "", html)``: the common case, and the one that must not be touched.
    """
    parsed = _Tokenizer(html)
    holes = parsed.probe_holes()
    head_open = parsed.find("start", "head")
    body_open = parsed.find("start", "body")
    if head_open is None and body_open is None:
        return "", "", _slice(html, 0, len(html), holes)

    head_close = parsed.find("end", "head", after=head_open) if head_open is not None else None
    body_close = parsed.find("end", "body", after=body_open) if body_open is not None else None
    html_close = parsed.find("end", "html")

    def offset(index: int | None, default: int) -> int:
        return parsed.tokens[index].at if index is not None else default

    head_extra = ""
    if head_open is not None:
        head_stop = offset(head_close, offset(body_open, len(html)))
        head_extra = _slice(html, parsed.end_of(head_open), head_stop, holes).strip()

    if body_open is not None:
        body_attrs = _attributes(parsed.tokens[body_open].attrs)
        body_start = parsed.end_of(body_open)
        body_stop = offset(body_close, offset(html_close, len(html)))
    else:  # a document with an implied <body>: everything after the head
        body_attrs = ""
        body_start = parsed.end_of(head_close) if head_close is not None else len(html)
        body_stop = offset(html_close, len(html))
    return head_extra, body_attrs, _slice(html, body_start, max(body_start, body_stop), holes)


# ----------------------------------------------------------------- the routes ---


def candidate_path(candidates_dir: Path, name: str) -> Path:
    """Resolve *name* to a file inside *candidates_dir*, or refuse.

    Two walls, because this route takes a name from a URL and the process can
    read the whole filesystem: the name must be a plain path component, and the
    resolved file must still be under the directory afterwards (a symlink inside
    the candidates directory is otherwise a way out of it).
    """
    if not _SAFE_CANDIDATE.fullmatch(name):
        raise HTTPException(status_code=404, detail=f"no candidate named {name!r}")
    root = candidates_dir.resolve()
    for suffix in CANDIDATE_SUFFIXES:
        found = (root / f"{name}{suffix}").resolve()
        if found.is_relative_to(root) and found.is_file():
            return found
    raise HTTPException(status_code=404, detail=f"no candidate named {name!r}")


def create_app(
    candidates_dir: Path,
    store: EvidenceStorePort,
    contract: DirectionContract,
    turn_id: TurnId,
    dev: bool = True,
    *,
    probe_js: Path | None = None,
    fixture_html: Path | None = FIXTURE_HTML,
) -> FastAPI:
    """Build the preview app around one contract, one store and one turn.

    Fails at construction rather than at request time for the two things that
    would otherwise surface as mysterious 500s an hour into a run: a turn id the
    evidence store could not file, and a missing ``probe.js`` when ``dev`` has
    promised the browser a ``<script src>`` that will resolve.
    """
    validate_turn_id(turn_id)
    if dev:
        probe_source(probe_js)  # fail fast: the shell must not promise what it lacks

    tokens_css = contract.to_css_custom_properties()
    health = IngestHealth()
    app = FastAPI(title="ui-servo preview", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.candidates_dir = candidates_dir
    app.state.contract = contract
    app.state.store = store
    app.state.turn_id = turn_id
    app.state.dev = dev
    app.state.ingest_health = health

    app.include_router(create_beacon_router(store, turn_id=turn_id, health=health))

    @app.get("/candidate/{name}", response_class=HTMLResponse)
    async def candidate(name: str) -> HTMLResponse:
        path = candidate_path(candidates_dir, name)
        head_extra, body_attrs, body_html = split_document(
            path.read_text(encoding="utf-8", errors="replace")
        )
        return HTMLResponse(
            render_shell(
                title=f"candidate {name}",
                tokens_css=tokens_css,
                head_extra=head_extra,
                body_attrs=body_attrs,
                body_html=body_html,
                dev=dev,
                config=probe_config(contract, turn_id, span_id=name),
                # The fragment usually stamps its own spans; the body carries the
                # candidate's name so anything outside them is still attributable
                # instead of landing under a synthetic anon-N.
                span_id=name,
            )
        )

    @app.get("/fixture", response_class=HTMLResponse)
    async def fixture() -> HTMLResponse:
        if fixture_html is None or not fixture_html.is_file():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"the probe fixture is not on this machine (looked at {fixture_html}). "
                    "/fixture is a development route: it serves tests/fixtures/probe.html "
                    "from a source checkout and is absent from an installed package."
                ),
            )
        head_extra, body_attrs, body_html = split_document(
            fixture_html.read_text(encoding="utf-8")
        )
        return HTMLResponse(
            render_shell(
                title="ui-servo probe fixture",
                tokens_css=tokens_css,
                head_extra=head_extra,
                body_attrs=body_attrs,
                body_html=body_html,
                dev=dev,
                config=probe_config(contract, turn_id),
                # Deliberately unstamped: the fixture verifies the sensor's own
                # fallback, including the synthetic span for a body with no
                # data-span-id.
                span_id=None,
            )
        )

    @app.get(PROBE_URL, response_class=PlainTextResponse)
    async def probe_asset() -> PlainTextResponse:
        try:
            source = probe_source(probe_js)
        except AssetMissing as error:  # pragma: no cover - fail-fast covers construction
            raise HTTPException(status_code=503, detail=str(error)) from error
        return PlainTextResponse(source, media_type="text/javascript; charset=utf-8")

    @app.get(TOKENS_URL, response_class=PlainTextResponse)
    async def tokens() -> PlainTextResponse:
        return PlainTextResponse(tokens_css, media_type="text/css; charset=utf-8")

    return app


if __name__ == "__main__":
    import argparse
    import sys
    from datetime import UTC, datetime

    import uvicorn

    from ui_servo.adapters.jsonl_store import JsonlEvidenceStore

    parser = argparse.ArgumentParser(
        prog="python -m ui_servo.adapters.preview_server",
        description="Serve candidate fragments in the contract's shell, with the probe wired up.",
    )
    parser.add_argument("--candidates", type=Path, required=True, help="directory of fragments")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--contract", type=Path, default=REPO_ROOT / "direction" / "direction.toml")
    parser.add_argument(
        "--evidence", type=Path, default=REPO_ROOT / ".ui-servo", help="evidence store root"
    )
    parser.add_argument(
        "--turn",
        default=f"turn-{datetime.now(UTC):%Y%m%d-%H%M%S}",
        help="turn id this preview's evidence is filed under",
    )
    parser.add_argument(
        "--no-dev",
        dest="dev",
        action="store_false",
        help="serve fragments without the probe (for screenshots and pixel diffs)",
    )
    args = parser.parse_args()

    if not args.candidates.is_dir():
        print(f"error: {args.candidates} is not a directory", file=sys.stderr)
        raise SystemExit(2)
    try:
        loaded = DirectionContract.from_toml(args.contract.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"error: cannot load contract {args.contract}: {error}", file=sys.stderr)
        raise SystemExit(2) from None

    evidence = JsonlEvidenceStore(args.evidence)
    try:
        application = create_app(args.candidates, evidence, loaded, args.turn, args.dev)
    except (AssetMissing, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None

    print(f"turn     {args.turn}")
    print(f"evidence {evidence.path_for_turn(args.turn)}")
    print(f"preview  http://{args.host}:{args.port}/candidate/<name>  (dev={args.dev})")
    uvicorn.run(application, host=args.host, port=args.port)
