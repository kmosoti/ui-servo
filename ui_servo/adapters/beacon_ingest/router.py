"""``POST /beacon``: the probe's flushed queue, decoded into the evidence stock.

This is the hot path of the fast loop and it is deliberately the dumbest module
in the system. A beacon arrives from a page that may be in the act of dying --
``sendBeacon`` fires on ``visibilitychange`` precisely because the page will not
be there to retry -- so the route does exactly three things: decode, validate
shape, and append. No model, no gate, no judgement; those read the stock later,
out of band. Anything slower here is evidence lost.

Three properties of ``navigator.sendBeacon`` shape the decoder:

* **The content type is a lie.** The probe sends ``JSON.stringify(batch)``, a
  string, so the browser labels it ``text/plain;charset=UTF-8``. The body is read
  as bytes and handed to ``orjson`` regardless of what the headers claim;
  trusting the header would drop every real beacon this system sends.
* **The payload is a batch.** One flush is many events across possibly several
  spans, which is why the store's ``append_many`` -- not ``append`` -- is the
  interface used, and why the response is ``204`` with no body: the sender is
  already gone and cannot act on anything we say.
* **The sender is not trusted.** This route is reachable by anything that can
  reach the preview server, and it writes to the stock the loop steers on. Every
  limit in this module (:data:`MAX_BODY_BYTES`, :data:`MAX_EVENTS`,
  :data:`MAX_PAGE_MS`, the payload clipping) exists because the probe's own caps
  are a courtesy from the client, and a courtesy is not a bound.

Three fields need translation on the way in, and all three translations are
recorded here rather than in the domain, because all three are facts about *this*
transport:

* ``ts`` is ``performance.now()``: monotonic, page-relative milliseconds, not an
  instant. The batch is anchored at its arrival instant and each event is placed
  back from it by its *own* clamped offset, so one absurd value can move only its
  own record -- never its neighbours', and never out of the representable range.
* ``node`` is the probe's compact element descriptor (``tag #id classes``). The
  domain's :class:`Signal` has no such field and must not grow one per sensor, so
  it is carried into the payload, where it is the join between a signal and the
  element a human is looking at.
* ``turnId`` is whatever the page was told. A page can be reloaded with a stale
  or hostile value and the store turns a turn id into a filename, so an id that
  is not a safe path component is replaced by the server's own -- which is itself
  validated when the router is built, so the fallback can never be the thing that
  makes an append fail.

Unknown ``kind`` values are stored verbatim. Sensors evolve faster than this
router is redeployed, and a kind this build has never heard of is still an
observation; refusing it would make the ingest a silent policy gate, which is the
one thing evidence must never be.
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import orjson
from litestar import Request, Response, Router, get, post

from ui_servo.domain.evidence import Signal, TurnId
from ui_servo.ports.store import EvidenceStorePort, StoreError

_log = logging.getLogger(__name__)

BEACON_PATH: Final[str] = "/beacon"
HEALTH_PATH: Final[str] = "/beacon/health"
PROBE_SOURCE: Final[str] = "probe"
UNATTRIBUTED_SPAN: Final[str] = "unattributed"
NODE_KEY: Final[str] = "node"
NODE_FALLBACK_KEY: Final[str] = "node_selector"

MAX_BODY_BYTES: Final[int] = 1 << 20
"""1 MiB. The probe chunks at 32 KiB, so this is 32 honest flushes of headroom
and still small enough that a hostile sender cannot buffer the process to death."""

MAX_EVENTS: Final[int] = 2000
"""Events kept from one body. The probe's queue caps at 500; a body claiming four
times that is not this probe, and the excess is counted rather than believed."""

MAX_PAYLOAD_DEPTH: Final[int] = 6
MAX_PAYLOAD_KEYS: Final[int] = 64
MAX_PAYLOAD_ITEMS: Final[int] = 64
MAX_FIELD_CHARS: Final[int] = 4096
"""Payload shape bounds. The probe truncates stacks at 800 chars and offender
lists at 12; these are the same idea enforced at the boundary that has to hold
when the sender is not the probe."""

MAX_PAGE_MS: Final[float] = 86_400_000.0
"""One day of ``performance.now()``. Beyond this a page-relative offset is not a
page-relative offset."""

MAX_BATCH_SPAN_MS: Final[float] = 300_000.0
"""How far back from a batch's anchor an event may be placed.

The probe flushes every 2 s, so a real batch spans seconds; five minutes is
generous room for a chunk that failed and was re-queued. Past it the offset is
not describing this flush, and pretending otherwise would file evidence under an
instant that never happened."""

SAFE_TURN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
"""Mirror of the JSONL store's path-component rule.

Duplicated on purpose: this is the boundary that must reject a hostile id, and a
boundary that can only find out it was hostile by watching the store raise is not
a boundary.
"""

KNOWN_PROBE_KINDS: frozenset[str] = frozenset(
    {
        "swap-ok",
        "swap-error",
        "motion-violation",
        "animation",
        "unknown-class",
        "overflow",
        "longtask",
        "layout-shift",
        "interaction",
        "element-timing",
        "js-error",
        "rejection",
        "csp",
        "ce-error",
        "wasm-error",
        "wasm-panic",
        "probe-drop",
        "probe-config-incomplete",
    }
)
"""What ``probe/probe.js`` emits today, including the two self-reports
(``probe-drop`` when its queue cap sheds events, ``probe-config-incomplete`` when
this server wired it up with a missing motion list).

Documentation and a telemetry hook, never a filter: :func:`decode_batch` accepts
any non-empty kind, and this set only says which ones this build was written
against.
"""


class BeaconError(ValueError):
    """The body was not a decodable batch. Raised only for the whole request."""


class BeaconTooLarge(BeaconError):
    """The body exceeded :data:`MAX_BODY_BYTES` and was not read to the end."""


@dataclass(slots=True)
class IngestHealth:
    """Counters for what this route did with what it was sent.

    The store is append-only and the sender cannot retry, so the failure mode
    this exists for is the quiet one: appends that were refused, events that were
    skipped, batches that were truncated. A loop steering on evidence it never
    actually recorded is worse than a loop that stops, so the numbers are exposed
    on ``GET /beacon/health`` and ``ok`` is false the moment any signal is lost.
    """

    batches: int = 0
    events: int = 0
    stored: int = 0
    skipped: int = 0
    truncated: int = 0
    refused: int = 0
    lost: int = 0
    last_error: str | None = field(default=None)

    @property
    def ok(self) -> bool:
        return self.lost == 0

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


def validate_turn_id(turn_id: TurnId) -> TurnId:
    """Refuse a turn id the store could not file, at the moment it is configured.

    Called when the router and the app are built, not when a beacon arrives: a
    server whose fallback turn cannot be written is broken on startup, and
    discovering that from a swallowed ``StoreError`` an hour into a run means an
    hour of evidence is already gone.
    """
    if not isinstance(turn_id, str) or not SAFE_TURN_ID.fullmatch(turn_id):
        raise ValueError(
            f"turn_id {turn_id!r} is not a safe path component; "
            "expected [A-Za-z0-9] followed by [A-Za-z0-9._-]"
        )
    return turn_id


def _batch_of(decoded: Any) -> Sequence[Any]:
    """Accept the shapes a sender might plausibly produce.

    The probe sends a bare array. A hand-rolled client, a curl in a bug report or
    a future sensor may send one object or wrap the array in a key; all three are
    unambiguous, so refusing them would be pedantry paid for in lost evidence.
    """
    match decoded:
        case list():
            return decoded
        case {"events": list() as events}:
            return events
        case Mapping():
            return [decoded]
        case _:
            raise BeaconError(f"expected a JSON array of events, got {type(decoded).__name__}")


def _offset(value: Any) -> float | None:
    """``performance.now()`` as a bounded offset, or ``None`` if it is not one.

    Clamped rather than rejected: a real page that has been open a long time is
    indistinguishable here from a hostile number, and both are safe once bounded.
    ``nan``/``inf`` have no bounded form and are refused outright -- a comparison
    against ``nan`` is silently false, which is how they would otherwise leak
    past every check below.
    """
    match value:
        case bool():
            return None
        case int() | float():
            number = float(value)
            if not math.isfinite(number):
                return None
            return min(max(number, 0.0), MAX_PAGE_MS)
        case _:
            return None


def _iso(value: Any) -> str | None:
    """The event's own instant, if it sent one this store can parse."""
    match value:
        case str() as text if text:
            try:
                datetime.fromisoformat(text)
            except ValueError:
                return None
            return text
        case _:
            return None


def _text(value: Any) -> str | None:
    match value:
        case str() as text if text.strip():
            return text
        case _:
            return None


def _clip(value: Any, depth: int = 0) -> Any:
    """Bound a payload's size and shape without changing what it says.

    Truncation is visible (a clipped string keeps its head, a clipped container
    keeps its first entries) because the alternative -- dropping the payload --
    loses the one part of a signal that explains it. Depth is bounded because a
    deeply nested body is a cheap way to make the JSON encoder in the store do
    unbounded work on the hot path.
    """
    if depth >= MAX_PAYLOAD_DEPTH:
        return "…"
    match value:
        case str() as text:
            return text[:MAX_FIELD_CHARS]
        case bool() | int() | None:
            return value
        case float() as number:
            # orjson refuses non-finite numbers while *parsing*, so this branch is
            # for callers that hand decode_batch a mapping from somewhere else.
            return number if math.isfinite(number) else str(number)
        case Mapping() as mapping:
            return {
                str(key)[:MAX_FIELD_CHARS]: _clip(item, depth + 1)
                for key, item in list(mapping.items())[:MAX_PAYLOAD_KEYS]
            }
        case list() | tuple() as items:
            return [_clip(item, depth + 1) for item in items[:MAX_PAYLOAD_ITEMS]]
        case other:
            return str(other)[:MAX_FIELD_CHARS]


def _payload_of(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event's payload, bounded, with its ``node`` descriptor folded in.

    ``node`` is top-level on the wire and has nowhere to go in a :class:`Signal`,
    so it joins the payload. If a sensor ever ships a payload that already uses
    the key, the payload keeps it and the descriptor moves aside: an ingest that
    overwrites what a sensor reported is worse than one with an odd key name.
    """
    raw = event.get("payload")
    match raw:
        case Mapping():
            payload = _clip(dict(raw))
        case None:
            payload = {}
        case other:
            payload = {"value": _clip(other)}

    node = _text(event.get(NODE_KEY))
    if node is not None:
        key = NODE_KEY if NODE_KEY not in payload else NODE_FALLBACK_KEY
        payload[key] = node[:MAX_FIELD_CHARS]
    return payload


@dataclass(frozen=True, slots=True)
class _Decoded:
    """One event that has survived validation, with its offset kept separately."""

    span_id: str
    turn_id: TurnId
    kind: str
    payload: dict[str, Any]
    iso: str | None
    offset: float | None


def _decode_event(event: Mapping[str, Any], *, turn_id: TurnId) -> _Decoded | None:
    kind = _text(event.get("kind"))
    if kind is None:
        return None  # An event that does not say what was observed is not evidence.
    claimed = _text(event.get("turnId")) or _text(event.get("turn_id"))
    return _Decoded(
        span_id=_text(event.get("spanId")) or _text(event.get("span_id")) or UNATTRIBUTED_SPAN,
        turn_id=claimed if claimed and SAFE_TURN_ID.fullmatch(claimed) else turn_id,
        kind=kind,
        payload=_payload_of(event),
        iso=_iso(event.get("ts")),
        offset=_offset(event.get("ts")),
    )


def decode_batch(
    body: bytes,
    *,
    turn_id: TurnId,
    received_at: datetime | None = None,
    source: str = PROBE_SOURCE,
    health: IngestHealth | None = None,
) -> tuple[Signal, ...]:
    """Decode one flushed queue into signals, in the order the page emitted them.

    *turn_id* is the server's own: the fallback for an event whose ``turnId`` is
    missing or unusable as a filename. Malformed *individual* events are skipped
    rather than failing the batch -- one bad event must not cost the fifty good
    ones queued behind it -- but a body that is not JSON at all is an error the
    caller decides about.
    """
    if not body.strip():
        return ()
    try:
        decoded = orjson.loads(body)
    except orjson.JSONDecodeError as error:
        raise BeaconError(f"body is not JSON: {error}") from error

    raw = _batch_of(decoded)
    kept = raw[:MAX_EVENTS]
    if health is not None:
        health.events += len(raw)
        health.truncated += len(raw) - len(kept)
    if len(raw) > len(kept):
        _log.warning("beacon batch of %d events truncated to %d", len(raw), MAX_EVENTS)

    events = [
        found
        for event in kept
        if isinstance(event, Mapping) and (found := _decode_event(event, turn_id=turn_id))
    ]
    if health is not None:
        health.skipped += len(kept) - len(events)

    arrival = received_at or datetime.now(UTC)
    anchor = _anchor([event.offset for event in events if event.offset is not None])

    return tuple(
        Signal(
            span_id=event.span_id,
            turn_id=event.turn_id,
            source=source,  # type: ignore[arg-type]
            kind=event.kind,
            ts=_instant(event, arrival=arrival, anchor=anchor),
            payload=event.payload,
        )
        for event in events
    )


def _anchor(offsets: Sequence[float]) -> float:
    """The offset that stands for "now" for this batch: robust by construction.

    The obvious anchor -- the largest offset -- is the wrong one, because it is
    the statistic a single absurd value controls completely: one event claiming
    ``1e308`` would drag every honest event in the batch back by decades. The
    median cannot be moved by a minority of bad values at all, so it is used to
    decide which offsets are *plausible*, and the anchor is the newest of those.
    The flush happens just after the newest real event, which is why the newest
    plausible offset is the closest thing to "now" a page-relative clock offers.
    """
    if not offsets:
        return 0.0
    ordered = sorted(offsets)
    median = ordered[len(ordered) // 2]
    return max(offset for offset in ordered if offset <= median + MAX_BATCH_SPAN_MS)


def _instant(event: _Decoded, *, arrival: datetime, anchor: float) -> str:
    """When the event happened, in a form the stock can hold.

    Never raises and never returns an unparseable string: a timestamp is the one
    field with no sensible default, so the failure mode is "filed at arrival",
    not "batch refused".
    """
    if event.iso is not None:
        return event.iso
    if event.offset is None:
        return arrival.isoformat()
    behind = min(max(anchor - event.offset, 0.0), MAX_BATCH_SPAN_MS)
    try:
        return (arrival - timedelta(milliseconds=behind)).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - bounded above
        return arrival.isoformat()


async def read_limited_body(request: Request, *, limit: int = MAX_BODY_BYTES) -> bytes:
    """Buffer at most *limit* bytes, refusing before the memory is spent.

    The declared length is checked first so an honest oversized sender is turned
    away without transferring anything, and the stream is counted as it arrives
    so a dishonest one is cut off mid-flight rather than believed.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BeaconTooLarge(f"declared body of {declared} bytes exceeds {limit}")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BeaconTooLarge(f"body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class BeaconRouter(Router):
    """A router that carries the counters its own routes write to.

    Litestar's :class:`~litestar.router.Router` declares ``__slots__``, so the
    counters cannot be stapled onto an instance after the fact -- which is the
    better outcome, because the sharing was never incidental. Whatever mounts
    this router (the preview server today, an ingest daemon tomorrow) and
    whatever reads the health route are looking at *one* :class:`IngestHealth`,
    and declaring the attribute here makes that part of the type rather than a
    convention held together by a ``type: ignore``.
    """

    __slots__ = ("health",)

    def __init__(self, *args: Any, health: IngestHealth, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.health = health


def create_beacon_router(
    store: EvidenceStorePort,
    *,
    turn_id: TurnId,
    path: str = BEACON_PATH,
    health_path: str = HEALTH_PATH,
    source: str = PROBE_SOURCE,
    health: IngestHealth | None = None,
) -> BeaconRouter:
    """A router exposing ``POST {path}`` for ``probe/probe.js``.

    The store is injected rather than constructed so the same router serves the
    preview server, a test's temporary directory and a future ingest daemon
    without any of them knowing where the stock lives. *turn_id* is validated
    here, at startup, because it is the fallback every unusable client id lands
    on -- if it were bad, the failure would surface as evidence quietly going
    missing under load.

    Both routes answer with an explicit :class:`~litestar.response.Response`
    rather than a declared status code, because the status *is* the report: 204
    for accepted, 400 for a body that was not JSON, 413 for one that was too
    big, and 200-or-503 on health. Nothing here may fall through to a
    framework default.
    """
    validate_turn_id(turn_id)
    counters = health if health is not None else IngestHealth()

    def _refuse(status: int, message: str) -> Response[bytes]:
        counters.refused += 1
        counters.last_error = message
        return Response(
            orjson.dumps({"error": message}),
            media_type="application/json",
            status_code=status,
        )

    @post(path, include_in_schema=False)
    async def ingest(request: Request) -> Response[bytes]:
        try:
            body = await read_limited_body(request)
        except BeaconTooLarge as error:
            return _refuse(413, str(error))
        counters.batches += 1
        try:
            signals = decode_batch(body, turn_id=turn_id, source=source, health=counters)
        except BeaconError as error:
            # 400 is for the human holding curl. The probe cannot read it: a
            # beacon is fire-and-forget, which is also why nothing above this
            # line is allowed to be slow enough to matter.
            return _refuse(400, str(error))
        _append(store, signals, counters)
        return Response(b"", status_code=204)

    @get(health_path, include_in_schema=False)
    async def ingest_health() -> Response[bytes]:
        # 503 when evidence has been lost, so a liveness check notices the one
        # failure that would otherwise be invisible until a gate read an empty turn.
        return Response(
            orjson.dumps(counters.as_dict()),
            media_type="application/json",
            status_code=200 if counters.ok else 503,
        )

    return BeaconRouter(path="/", route_handlers=[ingest, ingest_health], health=counters)


def _append(store: EvidenceStorePort, signals: Iterable[Signal], health: IngestHealth) -> int:
    batch = tuple(signals)
    if not batch:
        return 0
    try:
        written = store.append_many(batch)
    except StoreError as error:
        # A stock that cannot be extended is a real fault, but it is the loop's
        # fault and not the page's: the page is often already unloading and has
        # no retry left, so failing its request buys nothing. It is logged at
        # error *and* counted, because a silent one would mean the loop is
        # steering on evidence it never actually recorded.
        health.lost += len(batch)
        health.last_error = str(error)
        _log.error("cannot append %d beacon signals to the evidence store: %s", len(batch), error)
        return 0
    health.stored += written
    if written < len(batch):  # pragma: no cover - no store does this today
        health.lost += len(batch) - written
        health.last_error = f"store wrote {written} of {len(batch)} signals"
        _log.error("store wrote %d of %d beacon signals", written, len(batch))
    return written
