"""The judge port: one critic of the panel, stated as an interface.

Latency class 3 of the loop is a panel of models arguing about taste, and the
only property that makes a panel worth more than a single model is *variety*.
Ashby's law is the reason this port exists at all: a regulator can only absorb as
much variety as its own model contains, so a panel assembled from one vendor's
weights -- however many times it is sampled -- has one blind spot repeated N
times, not N views. The :attr:`JudgePort.family` property is therefore not a
label but the unit of account: the control loop counts *families*, not calls,
when it decides whether it has heard enough, and a rubric axis on which every
family agrees is evidence, while one on which a single family insists is a
finding to be weighed against the rest.

Families are also the rotation axis of the Gauntlet Loop. Critics are blind and
rotated so no single model's idiosyncrasies become the house style; rotation is
only meaningful across families, because rotating between two instances of the
same model rotates nothing.

What this port deliberately does not do is raise. A judge is a remote process on
someone else's machine and it will time out, be logged out, or be missing; a
panel that dies when one family is unavailable has *less* variety under stress
than a panel that records ``rc != 0`` for that family and proceeds with the
rest. Every failure mode is a :class:`JudgeResponse` with a non-zero ``rc``.

Pure port: standard library and :mod:`ui_servo.domain` only. Shelling out to a
locally authenticated CLI, speaking HTTP to a bridge, or calling a hosted API
are all adapter concerns and the panel must not be able to tell which it has.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, Self, runtime_checkable

type Family = str
type Prompt = str
type ResponseSchema = Mapping[str, Any]

DEFAULT_TIMEOUT_S: Final[int] = 120

RC_OK: Final[int] = 0
"""The judge answered. It says nothing about whether the answer was useful."""

RC_TRANSPORT: Final[int] = 1
"""The judge ran and reported its own failure (auth, quota, internal error)."""

RC_TIMEOUT: Final[int] = 124
"""The judge exceeded ``timeout_s``. Shell convention, so a human reading the
evidence stock recognises it without a lookup table."""

RC_UNAVAILABLE: Final[int] = 127
"""The judge could not be started at all: binary missing, bridge down, no
credentials. Distinguished from a timeout because the control loop's response
differs -- retry later versus drop the family from this round's panel."""


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """One question put to one critic, with everything it needs to answer it.

    Field shapes are immutable on purpose, which is a deliberate narrowing of the
    obvious ``list``/``dict``:

        prompt           ``str``, non-empty after stripping
        image_paths      ``tuple[Path, ...]`` -- any sequence of ``Path | str``
                         may be passed in and is coerced on construction
        response_schema  ``Mapping[str, Any] | None`` -- read, never mutated;
                         adapters serialise a copy
        timeout_s        ``int``, strictly positive

    A request is fanned out to several families, retried, and then written into
    an append-only stock. Any of those steps mutating a shared list would make
    the recorded prompt differ from the sent one, and a critique whose prompt
    cannot be trusted is not evidence.

    Images travel as *paths*, never as bytes. Every adapter here drives a CLI or
    an agent that already has filesystem access, so handing over an absolute path
    is both cheaper than base64 and more honest about what the critic can see;
    it also keeps a screenshot out of the evidence payload, where it would bloat
    an append-only stock that is meant to stay greppable. Adapters are required
    to resolve those paths before interpolating them and to refuse rather than
    guess when one is missing.

    ``response_schema`` is what turns a critique into data. When it is present
    the adapter is obliged to ask for JSON only and to parse defensively, so the
    control loop receives a rubric it can aggregate across families rather than
    three essays it would have to reconcile by hand.
    """

    prompt: Prompt
    image_paths: tuple[Path, ...] = ()
    response_schema: ResponseSchema | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("a judge request needs a prompt")
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be positive, got {self.timeout_s}")
        object.__setattr__(self, "image_paths", tuple(Path(path) for path in self.image_paths))

    @classmethod
    def of(
        cls,
        prompt: Prompt,
        *,
        images: Sequence[Path | str] = (),
        response_schema: ResponseSchema | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> Self:
        """Build a request from the loosely typed values a call site actually has."""
        return cls(
            prompt=prompt,
            image_paths=tuple(Path(image) for image in images),
            response_schema=response_schema,
            timeout_s=timeout_s,
        )

    @property
    def wants_json(self) -> bool:
        return self.response_schema is not None

    def with_prompt(self, prompt: Prompt) -> Self:
        """The same question asked differently -- the shape of a schema re-ask."""
        return type(self)(
            prompt=prompt,
            image_paths=self.image_paths,
            response_schema=self.response_schema,
            timeout_s=self.timeout_s,
        )

    def describe(self) -> Mapping[str, Any]:
        """The request as plain data, for the evidence payload.

        A critique that cannot be re-read later is an opinion; recording the
        exact prompt is what makes a panel's verdict auditable after the model
        behind a family has been updated underneath it.
        """
        return {
            "prompt": self.prompt,
            "image_paths": [str(path) for path in self.image_paths],
            "response_schema": (
                dict(self.response_schema) if self.response_schema is not None else None
            ),
            "timeout_s": self.timeout_s,
        }


@dataclass(frozen=True, slots=True)
class JudgeResponse:
    """What one family said, and what it cost to hear it.

    ``raw`` is kept even when ``parsed`` succeeded. The schema is the loop's
    convenience, not the record: a critic's prose frequently contains the reason
    behind a score, and discarding it at the moment of parsing would leave the
    stock holding numbers whose provenance is gone.
    """

    family: Family
    raw: str
    parsed: dict[str, Any] | None = None
    rc: int = RC_OK
    duration_s: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.rc == RC_OK

    def __bool__(self) -> bool:
        return self.ok

    @property
    def usable(self) -> bool:
        """Answered *and* structured: what an aggregator over a rubric requires."""
        return self.ok and self.parsed is not None

    @classmethod
    def failure(
        cls, family: Family, *, rc: int, raw: str, duration_s: float = 0.0, attempts: int = 1
    ) -> Self:
        if rc == RC_OK:
            raise ValueError("a failure carries a non-zero rc")
        return cls(family=family, raw=raw, parsed=None, rc=rc, duration_s=duration_s, attempts=attempts)

    def describe(self) -> Mapping[str, Any]:
        """The response as plain data, for the evidence payload."""
        return {
            "family": self.family,
            "raw": self.raw,
            "parsed": self.parsed,
            "rc": self.rc,
            "duration_s": self.duration_s,
            "attempts": self.attempts,
        }


@runtime_checkable
class JudgePort(Protocol):
    """One critic the panel can call, identified by the family it draws variety from.

    Implementations must be total: no transport failure, timeout, missing binary
    or malformed envelope may escape as an exception, because the panel's job is
    to survive the loss of a family without losing the round.
    """

    @property
    def family(self) -> Family:
        """The variety source. Two judges sharing a family are one vote, not two."""
        ...

    def judge(self, request: JudgeRequest) -> JudgeResponse:
        """Answer *request*, returning a non-zero ``rc`` rather than raising."""
        ...


def families(judges: Iterable[JudgePort]) -> tuple[Family, ...]:
    """Distinct families among *judges*, in first-seen order.

    The panel's actual variety, as opposed to its headcount.
    """
    return tuple(dict.fromkeys(judge.family for judge in judges))


@dataclass(frozen=True, slots=True)
class ScriptedJudge:
    """A :class:`JudgePort` that replays canned answers, for tests downstream.

    Kept beside the port rather than in ``tests/`` for the same reason as the
    trivial sanitiser: if the cheapest possible implementation is awkward to
    write, the port is shaped wrong.
    """

    family: Family = "scripted"
    responses: tuple[JudgeResponse, ...] = ()
    calls: list[JudgeRequest] = field(default_factory=list, compare=False)

    def judge(self, request: JudgeRequest) -> JudgeResponse:
        index = len(self.calls)
        self.calls.append(request)
        if index < len(self.responses):
            return self.responses[index]
        return JudgeResponse(family=self.family, raw="", parsed=None, rc=RC_UNAVAILABLE)
