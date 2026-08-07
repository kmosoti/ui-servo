"""Evidence: the stock the loops read, and the joins that make it legible.

In stock-and-flow terms a :class:`Signal` is a *flow* -- one observation, emitted
once by one sensor about one span at one instant -- and the evidence store is the
*stock* those flows accumulate into. The stock is append-only, so a regression is
visible as a change in what is held rather than as a claim in a chat log, and it
is untruncated, so a later question can be asked of an earlier turn without
re-running the sensors.

The join is the whole value of this module. Sensors are deliberately independent
(an in-page probe, a shadow browser, the pre-ship gates, a critic panel) and none
of them can see the others; ``data-span-id`` on every rendered fragment is the
key that lets their separate reports be read as one story about one piece of UI.
The span id alone is not that key, though: the same fragment is rendered again
next turn under the same id, and merging those would make a fix and the bug it
fixed one indistinguishable pile. :func:`join_spans` therefore joins on
``(turn_id, span_id)`` -- one rendering of one fragment.

What is deliberately *absent* here is judgement. A signal carries a ``kind`` --
what was observed -- and never a severity, a score, or a verdict; this module
does not even hold an opinion about which kinds are failures, because that set is
policy and policy moves independently of what a sensor saw. Callers that want to
partition signals pass their own kind set in. A domain that shipped a default
would be a policy nobody voted for, quietly rewriting history every time it
changed.

Pure domain: standard library plus pydantic. Reading and writing the stock is a
port, and JSONL on a disk is an adapter.
"""

from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

type SpanId = str
type TurnId = str
type SignalKind = str
type Timestamp = str
type Payload = dict[str, Any]
type SignalSource = Literal["probe", "harness", "gate", "judge"]
type SpanKey = tuple[TurnId, SpanId]

SIGNAL_SOURCES: frozenset[str] = frozenset({"probe", "harness", "gate", "judge"})
"""The sensor families, mirroring :data:`SignalSource` for runtime validation.

A vocabulary, not a classification: it says who may speak, never what any of
them said is worth.
"""

_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")


class Signal(BaseModel):
    """One observation: the atomic flow into the evidence stock.

    Immutable by construction because the stock is append-only; a signal that
    could be edited after the fact would make the store a cache of current
    opinion rather than a record of what was seen.
    """

    model_config = _MODEL_CONFIG

    span_id: SpanId = Field(min_length=1)
    turn_id: TurnId = Field(min_length=1)
    source: SignalSource
    kind: SignalKind = Field(min_length=1)
    ts: Timestamp = Field(min_length=1)
    payload: Payload = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _parseable_instant(cls, ts: str) -> str:
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            raise ValueError(f"ts {ts!r} is not an ISO-8601 instant") from None
        return ts

    @property
    def observed_at(self) -> datetime:
        return datetime.fromisoformat(self.ts)

    @property
    def span_key(self) -> SpanKey:
        """The join key: one rendering of one fragment, not one fragment."""
        return (self.turn_id, self.span_id)


@dataclass(frozen=True, slots=True)
class SpanEvidence:
    """Every signal any sensor emitted about one rendering of one span.

    A derived view rather than a parsed input, so it is a plain immutable value:
    the gates, the regulator and the critique prompt all read it and none of them
    may mutate what was observed.
    """

    span_id: SpanId
    turn_id: TurnId
    signals: tuple[Signal, ...]

    def __len__(self) -> int:
        return len(self.signals)

    def __iter__(self) -> Iterator[Signal]:
        return iter(self.signals)

    @property
    def key(self) -> SpanKey:
        return (self.turn_id, self.span_id)

    @property
    def kinds(self) -> frozenset[SignalKind]:
        return frozenset(signal.kind for signal in self.signals)

    @property
    def sources(self) -> frozenset[SignalSource]:
        return frozenset(signal.source for signal in self.signals)

    def of_kind(self, kind: SignalKind | Collection[SignalKind]) -> tuple[Signal, ...]:
        return by_kind(self.signals, kind)

    def of_source(self, source: SignalSource | Collection[SignalSource]) -> tuple[Signal, ...]:
        return by_source(self.signals, source)

    def failures(self, failure_kinds: Collection[SignalKind]) -> tuple[Signal, ...]:
        """Signals whose kind the *caller* counts as a departure."""
        return failures_only(self.signals, failure_kinds=failure_kinds)

    def has_failure(self, failure_kinds: Collection[SignalKind]) -> bool:
        wanted = frozenset(failure_kinds)
        return any(signal.kind in wanted for signal in self.signals)


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """The spans of one turn: the unit a revision is proposed against.

    A turn is the loop's sampling interval. Holding its spans together is what
    lets "this turn is worse than the last one" be a comparison of two stocks
    instead of a recollection.
    """

    turn_id: TurnId
    spans: tuple[SpanEvidence, ...]

    @classmethod
    def from_signals(cls, signals: Iterable[Signal], *, turn_id: TurnId | None = None) -> Self:
        """Join *signals* into spans, rejecting a mixture of turns.

        Silently merging turns would destroy the only boundary the loop measures
        change across, so it is an error rather than a convenience.
        """
        collected = tuple(signals)
        turns = {signal.turn_id for signal in collected}
        match sorted(turns):
            case [] if turn_id is None:
                raise ValueError("cannot infer a turn_id from zero signals; pass turn_id=")
            case []:
                resolved = turn_id
            case [only] if turn_id is None or turn_id == only:
                resolved = only
            case [only]:
                raise ValueError(f"signals belong to turn {only!r}, not {turn_id!r}")
            case many:
                raise ValueError(f"signals span several turns {many}; a TurnEvidence holds one")
        return cls(turn_id=resolved, spans=tuple(join_spans(collected).values()))

    def __len__(self) -> int:
        return len(self.spans)

    def __iter__(self) -> Iterator[SpanEvidence]:
        return iter(self.spans)

    def __getitem__(self, span_id: SpanId) -> SpanEvidence:
        try:
            return next(span for span in self.spans if span.span_id == span_id)
        except StopIteration:
            raise KeyError(f"unknown span {span_id!r}; known: {list(self.span_ids)}") from None

    @property
    def span_ids(self) -> tuple[SpanId, ...]:
        return tuple(span.span_id for span in self.spans)

    @property
    def signals(self) -> tuple[Signal, ...]:
        return tuple(signal for span in self.spans for signal in span)

    def failing_spans(self, failure_kinds: Collection[SignalKind]) -> tuple[SpanEvidence, ...]:
        return tuple(span for span in self.spans if span.has_failure(failure_kinds))


def join_spans(signals: Iterable[Signal]) -> dict[SpanKey, SpanEvidence]:
    """Group *signals* by ``(turn_id, span_id)``, preserving order at both levels.

    The composite key is the correction that makes the join safe across turns:
    span ids are stable by design (they are how a fragment is addressed), so
    keying on the id alone would fuse this turn's rendering with last turn's and
    hide exactly the regression the stock exists to reveal.

    Order is preserved because sequence is itself evidence -- an overflow
    reported before a swap completed means something different from one after.
    """
    grouped: dict[SpanKey, list[Signal]] = {}
    for signal in signals:
        grouped.setdefault(signal.span_key, []).append(signal)
    return {
        key: SpanEvidence(span_id=key[1], turn_id=key[0], signals=tuple(collected))
        for key, collected in grouped.items()
    }


def spans_in_turn(signals: Iterable[Signal], turn_id: TurnId) -> dict[SpanId, SpanEvidence]:
    """The single-turn join, keyed by span id because the turn is already fixed."""
    return {
        key[1]: span for key, span in join_spans(signals).items() if key[0] == turn_id
    }


def by_kind(
    signals: Iterable[Signal], kind: SignalKind | Collection[SignalKind]
) -> tuple[Signal, ...]:
    """Signals whose kind is *kind* (or is in it), in input order."""
    wanted = frozenset({kind}) if isinstance(kind, str) else frozenset(kind)
    return tuple(signal for signal in signals if signal.kind in wanted)


def by_source(
    signals: Iterable[Signal], source: SignalSource | Collection[SignalSource]
) -> tuple[Signal, ...]:
    """Signals from one sensor family, in input order.

    Useful precisely because the families are independent: agreement between
    ``probe`` and ``harness`` on the same span is decorrelated corroboration.
    """
    wanted = frozenset({source}) if isinstance(source, str) else frozenset(source)
    return tuple(signal for signal in signals if signal.source in wanted)


def failures_only(
    signals: Iterable[Signal], *, failure_kinds: Collection[SignalKind]
) -> tuple[Signal, ...]:
    """Signals whose kind is in *failure_kinds*, in input order.

    ``failure_kinds`` is required and has no default on purpose. Which kinds
    count as failures is policy -- it depends on the gate configuration and the
    rubric, both of which move on their own schedule -- and a default here would
    be that policy smuggled into the evidence layer, where nothing could see it
    change.
    """
    wanted = frozenset(failure_kinds)
    return tuple(signal for signal in signals if signal.kind in wanted)


def group_by_kind(signals: Iterable[Signal]) -> dict[SignalKind, tuple[Signal, ...]]:
    """Every kind present, mapped to its signals in input order."""
    grouped: dict[SignalKind, list[Signal]] = {}
    for signal in signals:
        grouped.setdefault(signal.kind, []).append(signal)
    return {kind: tuple(collected) for kind, collected in grouped.items()}


def kind_counts(signals: Iterable[Signal]) -> dict[SignalKind, int]:
    """Histogram of kinds: the cheapest turn-over-turn diff of the stock."""
    counts: dict[SignalKind, int] = {}
    for signal in signals:
        counts[signal.kind] = counts.get(signal.kind, 0) + 1
    return counts


def chronological(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    """Stable sort by observation instant.

    Sensors write into the stock concurrently, so store order is admission
    order, not event order; a reader that needs causality asks for it explicitly
    and ties keep their admission order.
    """
    return tuple(sorted(signals, key=lambda signal: signal.observed_at))


def spans_of(signals: Iterable[Signal]) -> tuple[SpanKey, ...]:
    """Distinct span keys in first-seen order."""
    return tuple(dict.fromkeys(signal.span_key for signal in signals))


def to_mappings(signals: Iterable[Signal]) -> tuple[Mapping[str, Any], ...]:
    """Plain-mapping view for a serialiser, so no adapter reaches into pydantic."""
    return tuple(signal.model_dump(mode="python") for signal in signals)


def from_mappings(rows: Iterable[Mapping[str, Any]]) -> tuple[Signal, ...]:
    """Inverse of :func:`to_mappings`; the parse step of any store's read path."""
    return tuple(Signal.model_validate(dict(row)) for row in rows)
