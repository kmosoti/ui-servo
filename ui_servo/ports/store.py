"""The stores: where the stock lives, stated as an interface.

Two stocks, and they are different in kind, which is why they are two ports.

:class:`EvidenceStorePort` holds what the *machine* observed. It is append-only
and write-cheap: the beacon hot path calls it on every batch, so nothing here
may block on judgement, and nothing may rewrite an earlier record -- a stock you
can edit cannot show you a regression.

:class:`ExemplarStorePort` holds what the *human* picked. Under Conant-Ashby the
regulator's model of taste is the direction contract plus these exemplars and
nothing else, so this is the one stock a model may never write to on its own:
taste injection is a human act, and keeping it behind a separate narrow port is
how that stays structurally true rather than merely intended.

Domain types only. A JSONL file, an object store or a database are all fine
implementations and the loops must not be able to tell which they have.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ui_servo.domain.evidence import Signal, SpanId, TurnId

type ExemplarName = str
type FileName = str
type FileBytes = bytes

META_FILENAME: FileName = "meta.json"


class StoreError(RuntimeError):
    """A stock could not be read or extended. Adapters raise this, not OSError."""


class StoreWriteError(StoreError):
    """A batch failed part-way through, naming the turns that did land.

    An append-only stock cannot roll back, so the honest thing is to say exactly
    how far the write got. Without ``turns_written`` a caller's only recovery is
    a blind retry, which duplicates whatever already landed and corrupts the
    very counts the stock exists to support.
    """

    def __init__(self, message: str, *, turns_written: tuple[TurnId, ...] = ()) -> None:
        super().__init__(message)
        self.turns_written = turns_written


@dataclass(frozen=True, slots=True)
class Exemplar:
    """One human-picked artefact: a point of the taste model, on disk and in git.

    Deliberately files plus a mapping rather than a parsed model: an exemplar is
    whatever the human found worth keeping (markup, a screenshot, a note), and
    the store's job is to hold it verbatim, not to have an opinion about it.
    """

    name: ExemplarName
    files: tuple[FileName, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class EvidenceStorePort(Protocol):
    """Append-only accumulation of observations, readable by turn and by span.

    The two read paths mirror the two questions the loops ask: a regulator asks
    about one span (did this fragment pass?), a critique or a human asks about a
    turn (what happened in this round?).

    **Ordering contract.** Both reads return signals in *admission order*: the
    order in which the store accepted them, which is a single total order over
    everything it holds and does not depend on which turn a signal landed in.
    Concurrent writers are interleaved by admission, not grouped by writer. The
    order is stable across reads and across process restarts -- a reader must be
    able to say "these three signals arrived in this order" a week later, or
    sequence stops being evidence.

    Admission order is not observation order: sensors buffer, so ``ts`` may run
    backwards within a batch. Callers that need causality sort by ``ts``
    explicitly (``ui_servo.domain.evidence.chronological``).
    """

    def append(self, signal: Signal) -> None:
        """Extend the stock by one observation. Never overwrites."""
        ...

    def append_many(self, signals: Iterable[Signal]) -> int:
        """Extend the stock by a batch, returning how many were written.

        The batch form exists for the beacon: one flush from the probe is many
        signals, and the ingest route must not pay a round trip per event.

        A batch is all-or-nothing as far as validation and encoding go: if any
        signal in it cannot be stored, none of them are. If the write itself
        fails part-way the store raises :class:`StoreWriteError` naming the
        turns that landed, because an append-only stock has no rollback.
        """
        ...

    def signals_for_turn(self, turn_id: TurnId) -> tuple[Signal, ...]:
        """Everything recorded for *turn_id*, in admission order. Unknown -> ()."""
        ...

    def signals_for_span(
        self, span_id: SpanId, *, turn_id: TurnId | None = None
    ) -> tuple[Signal, ...]:
        """Everything recorded for *span_id*, in admission order.

        ``turn_id`` narrows the search when the caller knows the turn, which it
        usually does; without it the store finds the span wherever it was
        written and still returns one admission-ordered sequence, so a span
        rendered in several turns reads as the history it is.
        """
        ...


@runtime_checkable
class ExemplarStorePort(Protocol):
    """The human's taste corpus: written rarely, read by every critique prompt."""

    def save_exemplar(
        self, name: ExemplarName, files: Mapping[FileName, FileBytes], meta: Mapping[str, Any]
    ) -> Exemplar:
        """Replace *name* with exactly *files* and *meta*, or leave it untouched.

        Replacement, not merge: what comes back from :meth:`list_exemplars` is
        the set that was passed in, with nothing surviving from an earlier save.
        A half-updated exemplar would be a taste model that is neither the old
        one nor the new one, and the regulator would be reasoning from it
        without knowing.
        """
        ...

    def list_exemplars(self) -> tuple[Exemplar, ...]:
        """Every exemplar held, in a stable order."""
        ...
