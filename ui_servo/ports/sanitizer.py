"""The sanitiser port: latency class 0 of the loop, the gate before anything renders.

Every other tier of ui-servo costs a browser, a model call or a human glance.
This one costs a parse. It is the comparator that runs *first*, on the fragment
as text, and its job is not taste at all -- it is the small set of facts that
must be true before a fragment is allowed to exist in a page: no script, no
inline event handler, no off-origin request, no class that the contract cannot
justify, and a ``data-span-id`` so that anything the later tiers observe can be
attributed back to the fragment that caused it.

Failing closed is the point. An unknown utility class is not an aesthetic
disagreement to be argued with a critic later, it is an unreviewed escape hatch
out of the direction contract, and it is cheaper to reject it here than to
discover it in a screenshot diff.

The module is pure: standard library only, no ``nh3``, no parser, no filesystem.
Adapters (see ``ui_servo.adapters.nh3_sanitizer``) supply the teeth; this file
fixes the vocabulary they must speak, so the control loop can be written against
the vocabulary and tested with a two-line fake.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, Self, runtime_checkable

type FragmentHtml = str
type Locator = str


class ViolationKind(StrEnum):
    """Why a fragment was refused, at the granularity a fixer can act on.

    Kinds are a closed vocabulary rather than free text because the control loop
    routes on them: a ``DISALLOWED_TAG`` is a builder bug worth a retry with a
    pointed instruction, while a ``MALFORMED_FRAGMENT`` usually means the model
    truncated its output and the right move is to ask again.
    """

    MALFORMED_FRAGMENT = "malformed-fragment"
    """The HTML did not parse as a balanced fragment (usually a truncated response)."""

    DISALLOWED_TAG = "disallowed-tag"
    """An element outside the hypermedia policy (``script``, ``iframe``, ``object``...)."""

    DISALLOWED_ATTRIBUTE = "disallowed-attribute"
    """An attribute outside the policy: inline ``on*`` handlers, ``style``, unknown globals."""

    DISALLOWED_URL = "disallowed-url"
    """A URL with a dangerous scheme or an off-origin target."""

    UNKNOWN_CLASS = "unknown-class"
    """A class name no token in the direction contract can account for."""

    UNKNOWN_ATTRIBUTE = "unknown-attribute"
    """An ``hx-*``/``data-*`` attribute outside the documented schema."""

    INVALID_ATTRIBUTE_VALUE = "invalid-attribute-value"
    """A known attribute carrying a value the schema rejects (e.g. a bogus ``hx-swap``)."""

    MISSING_SPAN_ID = "missing-span-id"
    """The fragment root carries no ``data-span-id``, so observations cannot be attributed."""


@dataclass(frozen=True, slots=True, order=True)
class Violation:
    """One reason, in one place, with enough detail to fix it without guessing.

    ``locator`` is selector-ish (``section > a:nth-of-type(2)``) rather than a
    byte offset: the downstream consumers are a model being asked for a patch
    and a human reading a report, and both think in elements.
    """

    kind: ViolationKind
    locator: Locator
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail} [{self.locator}]"


@dataclass(frozen=True, slots=True)
class SanitizeResult:
    """The verdict on one fragment.

    ``cleaned_html`` is populated only when the fragment is accepted. There is no
    "here is what it would have been" path on purpose: a sanitiser that silently
    hands back a repaired fragment trains a builder to emit garbage and lean on
    the gate, which destroys the signal the loop is trying to measure.
    """

    accepted: bool
    cleaned_html: FragmentHtml | None = None
    violations: tuple[Violation, ...] = ()

    def __post_init__(self) -> None:
        """Refuse to exist in a contradictory state.

        A verdict that says "accepted" while carrying violations, or "rejected"
        while carrying usable output, is worse than no gate at all: every caller
        downstream reads one of the two fields and believes it. The invariants
        are checked here, and any sequence handed in is frozen into a tuple, so
        there is no ``result.violations.append(...)`` after the fact either.
        """
        object.__setattr__(self, "violations", tuple(self.violations))
        match (self.accepted, self.cleaned_html, self.violations):
            case (True, None, _):
                raise ValueError("an accepted fragment must carry its cleaned output")
            case (True, _, violations) if violations:
                raise ValueError("an accepted fragment cannot carry violations")
            case (False, None, ()):
                raise ValueError("a rejected fragment must say why")
            case (False, cleaned, _) if cleaned is not None:
                raise ValueError("a rejected fragment has no cleaned output")
            case _:
                return

    def __bool__(self) -> bool:
        return self.accepted

    @classmethod
    def ok(cls, cleaned_html: FragmentHtml) -> Self:
        return cls(accepted=True, cleaned_html=cleaned_html, violations=())

    @classmethod
    def rejected(cls, violations: Iterable[Violation]) -> Self:
        return cls(accepted=False, cleaned_html=None, violations=tuple(violations))

    def kinds(self) -> frozenset[ViolationKind]:
        """The distinct reasons for refusal -- what the control loop routes on."""
        return frozenset(violation.kind for violation in self.violations)

    def report(self) -> str:
        match self.violations:
            case ():
                return "accepted"
            case found:
                return "\n".join(str(violation) for violation in found)


@runtime_checkable
class SanitizerPort(Protocol):
    """Decide whether a hypermedia fragment may enter a page.

    Implementations must report *every* violation they can see, not just the
    first: a fixer that gets one problem per round trip turns a single cheap
    gate into N model calls, which is the opposite of what latency class 0 is
    for. They must also be pure functions of their input -- no network, no
    filesystem -- so the gate stays fast enough to run on every candidate.
    """

    def check(self, fragment_html: FragmentHtml) -> SanitizeResult:
        """Return the verdict on *fragment_html*, listing all violations found."""
        ...


@dataclass(frozen=True, slots=True)
class AcceptAllSanitizer:
    """A trivial in-memory :class:`SanitizerPort` for tests of things downstream.

    Kept in the port module (rather than in ``tests/``) because it is part of the
    contract's usability: if the cheapest possible implementation is awkward to
    write, the port is shaped wrong.
    """

    violations: tuple[Violation, ...] = field(default=())

    def check(self, fragment_html: FragmentHtml) -> SanitizeResult:
        match self.violations:
            case ():
                return SanitizeResult.ok(fragment_html)
            case found:
                return SanitizeResult.rejected(found)
