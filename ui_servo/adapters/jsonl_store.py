"""JSONL on a disk: the evidence stock made durable, and the exemplar corpus.

The format is chosen for the failure modes of a loop that runs unattended, with
several sensor processes writing at once and no coordinator between them.

*Append-only JSONL.* A record is one line; a line is committed only once its
newline is on disk. A crash can therefore cost at most the record in flight, and
the loss is detectable rather than silent, because an unterminated tail is the
only thing a complete record can never look like.

*One file per turn.* Small write sets, greppable by a human with no tooling, and
"what changed between turns" becomes a diff of two files rather than a query.

*An OS-level lock, not a process-level one.* The writers are separate processes
-- a beacon server, a harness, a CLI -- so a :class:`threading.Lock` would guard
nothing that matters. Every mutation takes an ``flock`` on ``evidence/.lock``,
which is what makes crash recovery safe: recovery shortens a file, and two
recoveries racing could shorten away a record another writer had committed.

*A sequence number per record.* Files order what is inside them, not what is
across them, so "what happened, in order" across turns needs a total order that
outlives the process. Each record is stamped with one under the lock, and it
lives in the storage envelope rather than in the domain ``Signal``, because it
describes when the store admitted the observation and not anything a sensor saw.

Payloads are stored losslessly. Truncating or coarsening what a sensor reported
would make the stock an interpretation, so bytes and non-finite floats are
tagged and reconstructed exactly rather than mangled into whatever JSON happens
to allow.

``orjson`` lives here and only here. The domain stays parseable by the
architecture test as stdlib-plus-pydantic, so serialisation is an adapter
concern by construction rather than by discipline.
"""

import base64
import contextlib
import fcntl
import math
import os
import re
import shutil
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import orjson

from ui_servo.domain.evidence import Signal, SpanId, TurnId, from_mappings, to_mappings
from ui_servo.ports.store import (
    META_FILENAME,
    Exemplar,
    ExemplarName,
    FileBytes,
    FileName,
    StoreError,
    StoreWriteError,
)

EVIDENCE_DIRNAME: Final[str] = "evidence"
EXEMPLAR_DIRNAME: Final[str] = "exemplars"
SUFFIX: Final[str] = ".jsonl"
LOCK_FILENAME: Final[str] = ".lock"
SEQUENCE_FILENAME: Final[str] = ".seq"
SEQUENCE_FIELD: Final[str] = "seq"
TAG: Final[str] = "__ui_servo__"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STAGING_PREFIX: Final[str] = ".staging-"
_TRASH_PREFIX: Final[str] = ".trash-"


def _json_default(value: Any) -> Any:
    """Widen the serialisable set for types with one obvious JSON shape.

    Only for values whose collapse is visible in the result: a path becomes its
    string, a set becomes a list. Anything whose JSON form would silently differ
    from what the sensor reported is tagged instead (see :func:`_encode_value`),
    and anything with no honest form at all raises.
    """
    match value:
        case Path():
            return str(value)
        case set() | frozenset() | tuple():
            return list(value)
        case _:
            raise TypeError(f"{type(value).__name__} has no JSON form")


def _encode_value(value: Any) -> Any:
    """Rewrite a payload into JSON that decodes back to exactly this value.

    JSON has no bytes and no non-finite floats; orjson's answers are a lossy
    decode and a silent ``null``. Both are tagged here instead, and a mapping
    that already contains the tag key is itself wrapped, so the encoding has no
    ambiguous case for a payload to fall into by accident.

    Mapping keys stay as they are: JSON has only string keys, and quietly
    renaming an integer one is the same class of rewrite, so a payload with
    non-string keys is refused rather than altered.
    """
    match value:
        case bytes() | bytearray():
            return {TAG: "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
        case bool():
            return value
        case float() if not math.isfinite(value):
            return {TAG: "float", "value": "nan" if math.isnan(value) else repr(value)}
        case Mapping():
            encoded = {key: _encode_value(item) for key, item in value.items()}
            return {TAG: "map", "value": encoded} if TAG in encoded else encoded
        case list() | tuple() | set() | frozenset():
            return [_encode_value(item) for item in value]
        case _:
            return value


def _decode_value(value: Any) -> Any:
    """Inverse of :func:`_encode_value`; run on every read so round-trip is exact."""
    if not isinstance(value, Mapping):
        return [_decode_value(item) for item in value] if isinstance(value, list) else value
    match value.get(TAG):
        case "bytes":
            return base64.b64decode(value["value"])
        case "float":
            return float(value["value"])
        case "map":
            return {key: _decode_value(item) for key, item in value["value"].items()}
        case _:
            return {key: _decode_value(item) for key, item in value.items()}


def _checked_name(name: str, *, what: str) -> str:
    if not _SAFE_NAME.fullmatch(name):
        raise StoreError(
            f"{what} {name!r} is not a safe path component; "
            "expected [A-Za-z0-9] followed by [A-Za-z0-9._-]"
        )
    return name


def _dumps(row: Any, *, option: int = 0) -> bytes:
    try:
        return orjson.dumps(row, default=_json_default, option=option)
    except orjson.JSONEncodeError as error:
        raise StoreError(f"cannot serialise: {error.__cause__ or error}") from error


def _encode(signals: Iterable[Signal], *, first_seq: int) -> bytes:
    """The batch as newline-terminated records, stamped with admission numbers."""
    return b"".join(
        _dumps(
            {SEQUENCE_FIELD: first_seq + offset, **row, "payload": _encode_value(row["payload"])}
        )
        + b"\n"
        for offset, row in enumerate(to_mappings(signals))
    )


def _write_all(fd: int, blob: bytes) -> None:
    """Write the whole blob, retrying a short write while the lock is still held.

    ``O_APPEND`` makes each write land at end-of-file atomically, but a short
    write would let another writer's record land in the middle of this one --
    which is why every caller holds the exclusive lock across the retry.
    """
    written = 0
    while written < len(blob):
        written += os.write(fd, blob[written:])


def _records(path: Path) -> Iterator[tuple[int, bytes]]:
    """Yield ``(line number, raw line)`` for every *complete* record in *path*.

    A trailing fragment without a newline is a write interrupted by a crash: it
    is skipped here, because the alternative is an unreadable turn. Any other
    malformed line is corruption and is raised by the caller, because silently
    dropping evidence is how a stock starts lying.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as error:
        raise StoreError(f"cannot read {path}: {error}") from error
    if not data:
        return
    lines = data.split(b"\n")
    if lines[-1]:
        lines.pop()
    for number, line in enumerate(lines, start=1):
        if line.strip():
            yield number, line


class JsonlEvidenceStore:
    """:class:`~ui_servo.ports.store.EvidenceStorePort` over ``<root>/evidence``.

    Safe to instantiate many times over one root, in one process or many: the
    mutual exclusion that matters lives in the filesystem, not in the object.
    """

    def __init__(self, root: Path | str, *, fsync: bool = False) -> None:
        self.root = Path(root)
        self.directory = self.root / EVIDENCE_DIRNAME
        self._fsync = fsync
        self._threads = threading.RLock()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StoreError(f"cannot create {self.directory}: {error}") from error
        self._lock_path = self.directory / LOCK_FILENAME
        self._sequence_path = self.directory / SEQUENCE_FILENAME

    @contextlib.contextmanager
    def _flock(self, mode: int) -> Iterator[None]:
        """Hold an advisory lock on ``evidence/.lock`` for the whole block.

        The thread lock is the cheap outer gate; the ``flock`` is the one that
        holds against other processes. Nothing inside a held block may take the
        lock again -- separate open file descriptions contend even within one
        process -- so internal readers use the unlocked paths deliberately.
        """
        with self._threads:
            try:
                descriptor = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
                fcntl.flock(descriptor, mode)
            except OSError as error:
                raise StoreError(f"cannot lock {self._lock_path}: {error}") from error
            try:
                yield
            finally:
                os.close(descriptor)

    def path_for_turn(self, turn_id: TurnId) -> Path:
        return self.directory / f"{_checked_name(turn_id, what='turn_id')}{SUFFIX}"

    def append(self, signal: Signal) -> None:
        self.append_many((signal,))

    def append_many(self, signals: Iterable[Signal]) -> int:
        """Stamp, encode and write a batch under one exclusive lock.

        Everything that can fail without touching the disk -- name validation,
        payload encoding, serialisation -- happens before the first byte is
        written, so a poisoned signal anywhere in the batch means no file is
        extended at all. Only an actual I/O failure can leave part of a batch on
        disk, and that raises :class:`StoreWriteError` naming what landed.
        """
        batched: dict[TurnId, list[Signal]] = {}
        for signal in signals:
            batched.setdefault(signal.turn_id, []).append(signal)
        if not batched:
            return 0
        paths = {turn_id: self.path_for_turn(turn_id) for turn_id in batched}
        total = sum(len(batch) for batch in batched.values())

        with self._flock(fcntl.LOCK_EX):
            first_seq = self._allocate(total)
            blobs: list[tuple[TurnId, bytes]] = []
            offset = 0
            for turn_id, batch in batched.items():
                blobs.append((turn_id, _encode(batch, first_seq=first_seq + offset)))
                offset += len(batch)
            landed: list[TurnId] = []
            for turn_id, blob in blobs:
                try:
                    self._append_blob(paths[turn_id], blob)
                except StoreError as error:
                    raise StoreWriteError(str(error), turns_written=tuple(landed)) from error
                landed.append(turn_id)
        return total

    def _allocate(self, count: int) -> int:
        """Reserve *count* consecutive admission numbers. Caller holds the lock.

        A crash between reserving and writing leaves a gap, which costs nothing:
        the numbers are an order, not a count. Losing the counter entirely is
        recoverable by reading the stock back, so the ordering survives anything
        short of losing the evidence itself.
        """
        current = self._read_sequence()
        temporary = self._sequence_path.with_name(f"{SEQUENCE_FILENAME}.{os.getpid()}.tmp")
        try:
            temporary.write_text(str(current + count), encoding="utf-8")
            os.replace(temporary, self._sequence_path)
        except OSError as error:
            raise StoreError(f"cannot update {self._sequence_path}: {error}") from error
        return current

    def _read_sequence(self) -> int:
        try:
            return int(self._sequence_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            return self._recover_sequence()
        except (OSError, ValueError):
            return self._recover_sequence()

    def _recover_sequence(self) -> int:
        """Rebuild the counter from the stock: the records are the source of truth."""
        highest = -1
        for path in self._turn_paths():
            for _, line in _records(path):
                with contextlib.suppress(orjson.JSONDecodeError, AttributeError, TypeError):
                    highest = max(highest, int(orjson.loads(line).get(SEQUENCE_FIELD, -1)))
        return highest + 1

    def _append_blob(self, path: Path, blob: bytes) -> None:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError as error:
            raise StoreError(f"cannot open {path} for append: {error}") from error
        try:
            _quarantine_torn_tail(descriptor, path)
            _write_all(descriptor, blob)
            if self._fsync:
                os.fsync(descriptor)
        except OSError as error:
            raise StoreError(f"cannot append to {path}: {error}") from error
        finally:
            os.close(descriptor)

    def signals_for_turn(self, turn_id: TurnId) -> tuple[Signal, ...]:
        path = self.path_for_turn(turn_id)
        with self._flock(fcntl.LOCK_SH):
            stamped = self._read(path)
        stamped.sort(key=lambda pair: pair[0])
        return tuple(signal for _, signal in stamped)

    def signals_for_span(
        self, span_id: SpanId, *, turn_id: TurnId | None = None
    ) -> tuple[Signal, ...]:
        with self._flock(fcntl.LOCK_SH):
            match turn_id:
                case None:
                    paths = self._turn_paths()
                case known:
                    paths = (self.path_for_turn(known),)
            stamped = [pair for path in paths for pair in self._read(path)]
        stamped.sort(key=lambda pair: pair[0])
        return tuple(signal for _, signal in stamped if signal.span_id == span_id)

    def turn_ids(self) -> tuple[TurnId, ...]:
        """Turns on disk, sorted by name so a listing is reproducible."""
        return tuple(path.stem for path in self._turn_paths())

    def _turn_paths(self) -> tuple[Path, ...]:
        try:
            return tuple(sorted(self.directory.glob(f"*{SUFFIX}")))
        except OSError as error:
            raise StoreError(f"cannot list {self.directory}: {error}") from error

    def _read(self, path: Path) -> list[tuple[int, Signal]]:
        """Parse one turn file into ``(admission number, signal)`` pairs.

        Records the store did not stamp sort ahead of everything it did, keeping
        their file order: hand-written or migrated lines are still evidence, and
        a reader should see them where they are rather than not at all.
        """
        stamped: list[tuple[int, Signal]] = []
        for number, line in _records(path):
            try:
                row = orjson.loads(line)
                sequence = int(row.pop(SEQUENCE_FIELD, -1))
                row["payload"] = _decode_value(row.get("payload", {}))
                stamped.append((sequence, from_mappings((row,))[0]))
            except orjson.JSONDecodeError as error:
                raise StoreError(f"{path}:{number} is not valid JSON: {error}") from error
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                raise StoreError(f"{path}:{number} is not a Signal: {error}") from error
        return stamped


def _quarantine_torn_tail(fd: int, path: Path) -> None:
    """Move a crash's half-written record aside before appending after it.

    A record is committed only once its newline is on disk, so a tail without
    one was never part of the stock and moving it to ``<turn>.jsonl.torn``
    removes nothing that was ever recorded -- while gluing the next record onto
    it would destroy one that was. This is the single place an evidence file is
    shortened; it runs only under the exclusive lock, because a second writer
    recovering concurrently could cut away records this one had committed.
    """
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0 or os.pread(fd, 1, size - 1) == b"\n":
        return
    data = os.pread(fd, size, 0)
    cut = data.rfind(b"\n") + 1
    with path.with_name(f"{path.name}.torn").open("ab") as handle:
        handle.write(data[cut:] + b"\n")
    os.ftruncate(fd, cut)


class JsonlExemplarStore:
    """:class:`~ui_servo.ports.store.ExemplarStorePort` over ``<root>/exemplars``.

    Each exemplar is a directory of verbatim files plus ``meta.json``, so the
    taste corpus is reviewable in a pull request and rollable back with ``git
    revert`` -- the whole point of keeping the regulator's model as data.

    A save is staged in full and then swapped in by rename, so a reader never
    sees a mixture of two picks. The swap is two renames, and a crash between
    them leaves the old exemplar in ``.trash-*``: bounded, visible, and restored
    by the sweep that runs on the next construction or save.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.directory = self.root / EXEMPLAR_DIRNAME
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StoreError(f"cannot create {self.directory}: {error}") from error
        self._sweep()

    def path_for(self, name: ExemplarName) -> Path:
        return self.directory / _checked_name(name, what="exemplar name")

    def save_exemplar(
        self, name: ExemplarName, files: Mapping[FileName, FileBytes], meta: Mapping[str, Any]
    ) -> Exemplar:
        if not files:
            raise StoreError(f"exemplar {name!r} has no files; an empty exemplar teaches nothing")
        if META_FILENAME in files:
            raise StoreError(f"{META_FILENAME} is written from meta, not from files")
        target = self.path_for(name)
        for filename in files:
            _checked_name(filename, what="exemplar file")
        encoded_meta = _dumps(dict(meta), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)

        self._sweep()
        unique = f"{os.getpid()}-{time.time_ns()}"
        staging = self.directory / f"{_STAGING_PREFIX}{name}-{unique}"
        trash = self.directory / f"{_TRASH_PREFIX}{name}-{unique}"
        try:
            staging.mkdir()
            for filename, blob in files.items():
                (staging / filename).write_bytes(blob)
            (staging / META_FILENAME).write_bytes(encoded_meta)
            if target.exists():
                os.rename(target, trash)
            os.rename(staging, target)
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            if trash.exists() and not target.exists():
                os.rename(trash, target)
            raise StoreError(f"cannot write exemplar {name!r}: {error}") from error
        shutil.rmtree(trash, ignore_errors=True)
        return Exemplar(name=name, files=tuple(sorted(files)), meta=dict(meta))

    def list_exemplars(self) -> tuple[Exemplar, ...]:
        return tuple(self._load(path) for path in self._exemplar_paths())

    def _exemplar_paths(self) -> tuple[Path, ...]:
        try:
            candidates = sorted(
                path
                for path in self.directory.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            )
        except OSError as error:
            raise StoreError(f"cannot list {self.directory}: {error}") from error
        return tuple(path for path in candidates if (path / META_FILENAME).is_file())

    def _sweep(self) -> None:
        """Finish or undo whatever a previous crash left half-done.

        Staging directories are always discardable -- nothing has been promised
        about them. A trash directory means the swap was interrupted: if its
        exemplar is missing, the old pick is put back, because losing a human's
        taste injection to a power cut is not an acceptable failure mode.
        """
        try:
            leftovers = sorted(self.directory.iterdir())
        except OSError as error:
            raise StoreError(f"cannot list {self.directory}: {error}") from error
        for path in leftovers:
            if not path.is_dir():
                continue
            if path.name.startswith(_STAGING_PREFIX):
                shutil.rmtree(path, ignore_errors=True)
            elif path.name.startswith(_TRASH_PREFIX):
                name = path.name[len(_TRASH_PREFIX) :].rsplit("-", 2)[0]
                restored = self.directory / name
                if name and not restored.exists():
                    os.rename(path, restored)
                else:
                    shutil.rmtree(path, ignore_errors=True)

    def _load(self, path: Path) -> Exemplar:
        try:
            meta = orjson.loads((path / META_FILENAME).read_bytes())
        except (OSError, orjson.JSONDecodeError) as error:
            raise StoreError(f"cannot read {path / META_FILENAME}: {error}") from error
        files = tuple(
            sorted(
                child.name
                for child in path.iterdir()
                if child.is_file() and child.name != META_FILENAME
            )
        )
        return Exemplar(name=path.name, files=files, meta=meta)
