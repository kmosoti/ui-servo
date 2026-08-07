"""The Tier 0 comparator: nh3, a class allowlist and an attribute schema, composed.

Three independent checks run over the same fragment and every one of them
reports. They are independent because they fail for different reasons and want
different fixes:

1. **nh3** (the Rust ``ammonia`` cleaner) answers "would a browser be asked to do
   something we never allowed?" -- scripts, iframes, inline ``on*`` handlers,
   ``style``, ``javascript:`` URLs. We do not ship nh3's repaired output; we diff
   it against the input, element by element, and treat any difference as a
   rejection, because a gate that silently launders bad markup teaches the
   builder that bad markup is fine.
2. **class allowlist** answers "is every class one the direction contract can
   account for?" -- seeded from :meth:`DirectionContract.class_allowlist_seed`
   and optionally widened from the token stylesheet via :func:`class_names_from_css`.
   Unknown class means unreviewed CSS, which is how generated UI drifts.
3. **attribute schema** (pydantic) answers "is the hypermedia wiring well
   formed?" -- ``hx-get``/``hx-post``/... must be same-origin relative paths,
   ``hx-swap`` and ``hx-trigger`` must match htmx's documented grammar *including
   modifier values*, and the fragment root must carry a ``data-span-id`` so that
   whatever the probe measures later can be attributed to the fragment that
   caused it.

Three policies are stricter than htmx itself, deliberately, and are called out
here because they are the ones a builder will trip over:

* **``data-hx-*`` is the same attribute as ``hx-*``.** htmx honours both
  spellings; so does this gate, after normalisation, or the ``data-`` form would
  be a schema bypass wearing a hat.
* **No ``hx-on`` in any spelling, and no ``hx-trigger`` event filters.** htmx
  evaluates both with ``Function``; ``click[alert(1)]`` is script execution
  through an attribute the sanitiser would otherwise call structural. Fragments
  that need behaviour get it from a reviewed script file, not from markup.
* **URLs must be ASCII, backslash-free, scheme-free, same-origin relative
  paths.** Python's ``urlsplit`` and a browser's WHATWG parser disagree about
  strings like ``/\\evil.example`` (relative to one, cross-origin to the other),
  and where they disagree the browser wins. So the grammar here is an explicit
  character-set allowlist rather than "whatever urlsplit says".

Nothing in this module may raise on hostile input: a gate that crashes is a gate
that is not applied. URL parsing in particular is wrapped -- a string that cannot
be parsed is a violation, never an exception.

This is an adapter: it may import the world (``nh3``, ``tinycss2``, ``pydantic``)
and it may read the domain. It performs no I/O -- callers hand it text.
"""

import re
import string
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

import nh3
import tinycss2
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import AfterValidator

from ui_servo.domain.contract import DirectionContract
from ui_servo.ports.sanitizer import (
    FragmentHtml,
    Locator,
    SanitizeResult,
    Violation,
    ViolationKind,
)

type ClassName = str
type AttrName = str
type AttrValue = str
type Attributes = tuple[tuple[AttrName, AttrValue], ...]

# --------------------------------------------------------------------------- #
# Policy: what a hypermedia fragment is allowed to be made of.
# --------------------------------------------------------------------------- #

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        # sectioning + flow
        "address", "article", "aside", "blockquote", "details", "div", "dd",
        "dl", "dt", "figcaption", "figure", "footer", "header", "hgroup", "hr",
        "li", "main", "menu", "nav", "ol", "p", "pre", "search", "section",
        "summary", "ul",
        "h1", "h2", "h3", "h4", "h5", "h6",
        # phrasing
        "a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "data", "del",
        "dfn", "em", "i", "ins", "kbd", "mark", "q", "rp", "rt", "ruby", "s",
        "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var",
        "wbr",
        # embedded (sources still go through the URL check)
        "img", "picture", "source", "audio", "video", "track",
        # tabular
        "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot",
        "tr", "th", "td",
        # forms: htmx posts them, so they are part of the hypermedia surface
        "form", "label", "input", "button", "select", "option", "optgroup",
        "textarea", "fieldset", "legend", "output", "progress", "meter",
    }
)

#: Removed *with their content*, not merely unwrapped: keeping the body of a
#: ``<script>`` as text would turn a blocked script into visible garbage.
CLEAN_CONTENT_TAGS: frozenset[str] = frozenset({"script", "style", "template", "noscript"})

#: ``style`` is deliberately absent everywhere: values come from tokens.css, and
#: an inline style is a value the contract never approved.
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "id", "title", "role", "lang", "dir", "hidden", "tabindex", "slot", "translate"},
    "a": {"href", "target", "rel", "download", "hreflang", "type"},
    "img": {"src", "alt", "width", "height", "loading", "decoding", "srcset", "sizes", "fetchpriority"},
    "source": {"src", "srcset", "sizes", "type", "media", "width", "height"},
    "video": {"src", "poster", "controls", "muted", "loop", "playsinline", "preload", "width", "height"},
    "audio": {"src", "controls", "loop", "muted", "preload"},
    "track": {"src", "kind", "srclang", "label", "default"},
    "form": {"action", "method", "enctype", "novalidate", "target", "autocomplete"},
    "label": {"for"},
    "input": {
        "type", "name", "value", "placeholder", "checked", "disabled", "required",
        "readonly", "min", "max", "step", "pattern", "minlength", "maxlength",
        "autocomplete", "multiple", "accept", "list", "inputmode",
    },
    "button": {"type", "name", "value", "disabled", "form"},
    "select": {"name", "multiple", "required", "disabled", "size", "autocomplete"},
    "option": {"value", "selected", "disabled", "label"},
    "optgroup": {"label", "disabled"},
    "textarea": {"name", "rows", "cols", "placeholder", "required", "disabled", "readonly", "wrap", "maxlength"},
    "fieldset": {"disabled", "name"},
    "output": {"for", "name"},
    "progress": {"value", "max"},
    "meter": {"value", "min", "max", "low", "high", "optimum"},
    "details": {"open", "name"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "td": {"colspan", "rowspan", "headers"},
    "th": {"colspan", "rowspan", "headers", "scope", "abbr"},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime"},
    "data": {"value"},
    "del": {"datetime", "cite"},
    "ins": {"datetime", "cite"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "bdo": {"dir"},
}

#: ``hx-*`` is the hypermedia wiring, ``data-*`` carries span ids and probe hooks,
#: ``aria-*`` is accessibility. Everything else needs an explicit entry above.
GENERIC_ATTRIBUTE_PREFIXES: frozenset[str] = frozenset({"hx-", "data-", "aria-"})

URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

VOID_ELEMENTS: frozenset[str] = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)

# --------------------------------------------------------------------------- #
# HTML's optional end tags, as a table rather than as a vibe.
# --------------------------------------------------------------------------- #

#: Start tags that implicitly close an open ``<p>`` (HTML spec, "a p element's
#: end tag may be omitted if ... followed by ...").
_P_CLOSERS: frozenset[str] = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "div", "dl",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
        "p", "pre", "search", "section", "table", "ul",
    }
)

#: open tag -> start tags that implicitly close it.
_CLOSED_BY_START: dict[str, frozenset[str]] = {
    "p": _P_CLOSERS,
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "option": frozenset({"option", "optgroup"}),
    "optgroup": frozenset({"optgroup"}),
    "rt": frozenset({"rt", "rp"}),
    "rp": frozenset({"rt", "rp"}),
    "thead": frozenset({"tbody", "tfoot"}),
    "tbody": frozenset({"tbody", "tfoot"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
}

#: ``<colgroup>`` closes on anything that is not another column.
_COLGROUP_KEEPS: frozenset[str] = frozenset({"col", "template"})

#: Elements whose end tag HTML lets you omit; leaving one open at the end of a
#: fragment is ordinary markup, not the truncated model output we screen for.
OPTIONAL_END_TAGS: frozenset[str] = frozenset(_CLOSED_BY_START) | {"colgroup"}

SPAN_ID_ATTRIBUTE: str = "data-span-id"
_SPAN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

#: HTML splits ``class`` on ASCII whitespace and on nothing else. U+00A0 is *not*
#: a separator, so ``"p-lg p-sm"`` is one (unknown) class, not two known ones.
_ASCII_WHITESPACE = re.compile(r"[ \t\n\f\r]+")

# --------------------------------------------------------------------------- #
# htmx attribute schema.
# --------------------------------------------------------------------------- #

HX_URL_ATTRIBUTES: tuple[str, ...] = ("hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")

#: htmx honours ``data-hx-*`` identically to ``hx-*``; so must the gate.
HX_DATA_PREFIX: str = "data-hx-"

#: Any spelling of htmx's inline-JavaScript attribute. htmx runs these through
#: ``Function``, which makes them event handlers with a friendlier name.
_HX_ON = re.compile(r"^(?:data-)?hx-on(?:$|[:-])")

#: htmx's documented swap positions. ``hx-swap`` outside this set is not a style
#: choice, it is a silent no-op at runtime -- exactly the class of bug a Tier 0
#: gate exists to catch before a browser is ever started.
HX_SWAP_POSITIONS: frozenset[str] = frozenset(
    {"innerHTML", "outerHTML", "textContent", "beforebegin", "afterbegin", "beforeend", "afterend", "delete", "none"}
)

_DANGEROUS_SCHEMES: frozenset[str] = frozenset(
    {"javascript", "vbscript", "data", "file", "blob", "about", "filesystem", "jar", "view-source"}
)

#: Attributes whose value is one URL.
_URL_ATTRIBUTES: frozenset[str] = frozenset(
    {"href", "src", "action", "formaction", "poster", "cite", "data", "background", "longdesc"}
)
#: Attributes whose value is a comma-separated candidate list (``url 1x, url 2x``).
_SRCSET_ATTRIBUTES: frozenset[str] = frozenset({"srcset", "imagesrcset"})
#: Attributes whose value is a whitespace-separated URL list.
_URL_LIST_ATTRIBUTES: frozenset[str] = frozenset({"ping"})

#: The character set a fragment URL may be written in. Anything outside it --
#: backslashes, control characters, raw whitespace, non-ASCII -- is rejected
#: rather than normalised, because normalising is where parser differentials live.
_URL_SAFE_CHARS: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "-._~%!$&'()*+,;=:@/?#"
)
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
#: C0 controls and space, which browsers strip from the ends of a URL, plus the
#: tab/newline characters they strip from *inside* one (``java\tscript:`` works).
_URL_TRIM = "".join(chr(code) for code in range(0x21)) + "\x7f"

_TIMING = re.compile(r"^\d+(?:\.\d+)?(?:ms|s)$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_BOOLEAN: frozenset[str] = frozenset({"true", "false"})
_SCROLL_TARGET: frozenset[str] = frozenset({"top", "bottom", "none"})
_SELECTOR = re.compile(r"^[A-Za-z0-9_#.:>~+*\[\]()=\"'^$|,-]+$")
_SELECTOR_KEYWORDS: frozenset[str] = frozenset({"closest", "find", "next", "previous"})
_EVENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_EVERY = re.compile(r"^every\s+\d+(?:\.\d+)?(?:ms|s)$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _selector_value(value: str) -> None:
    if not value or not _SELECTOR.match(value):
        raise ValueError(f"{value!r} is not a plain CSS selector")


def _scroll_value(value: str) -> None:
    if value in _SCROLL_TARGET:
        return
    selector, _, where = value.rpartition(":")
    if where not in _SCROLL_TARGET or not selector:
        raise ValueError(f"{value!r} is not <top|bottom|none> or <selector>:<top|bottom>")
    _selector_value(selector)


def _timing_value(value: str) -> None:
    if not _TIMING.match(value):
        raise ValueError(f"{value!r} is not a duration like '200ms' or '1.5s'")


def _number_value(value: str) -> None:
    if not _NUMBER.match(value):
        raise ValueError(f"{value!r} is not a number")


def _enum_value(allowed: frozenset[str]):
    def check(value: str) -> None:
        if value not in allowed:
            raise ValueError(f"{value!r} is not one of {sorted(allowed)}")

    return check


#: modifier -> validator for its value, or ``None`` when the modifier is a flag
#: that must not carry one (``once:oops`` is a typo that htmx would ignore).
HX_SWAP_MODIFIERS: dict[str, Any] = {
    "transition": _enum_value(_BOOLEAN),
    "ignoreTitle": _enum_value(_BOOLEAN),
    "focus-scroll": _enum_value(_BOOLEAN),
    "swap": _timing_value,
    "settle": _timing_value,
    "scroll": _scroll_value,
    "show": _scroll_value,
}

HX_TRIGGER_MODIFIERS: dict[str, Any] = {
    "once": None,
    "changed": None,
    "consume": None,
    "delay": _timing_value,
    "throttle": _timing_value,
    "from": _selector_value,
    "target": _selector_value,
    "root": _selector_value,
    "threshold": _number_value,
    "queue": _enum_value(frozenset({"first", "last", "all", "none"})),
}


def _check_modifier(modifier: str, table: dict[str, Any], *, requires_value: bool) -> None:
    name, separator, value = modifier.partition(":")
    if name not in table:
        raise ValueError(f"modifier {modifier!r} is not one of {sorted(table)}")
    validator = table[name]
    match (validator, separator):
        case (None, ":"):
            raise ValueError(f"modifier {name!r} takes no value, got {value!r}")
        case (None, _):
            return
        case (_, ""):
            if requires_value:
                raise ValueError(f"modifier {name!r} needs a value")
            return
        case _:
            validator(value)


# --------------------------------------------------------------------------- #
# URLs.
# --------------------------------------------------------------------------- #


def browser_normalized_url(value: str) -> str:
    """What a browser sees after it strips the characters HTML lets you hide in.

    Leading/trailing C0 and space go, and tab/CR/LF go *anywhere* in the string,
    which is why ``"java&#9;script:alert(1)"`` is a live ``javascript:`` URL and
    must be scheme-checked in this normalised form rather than as written.
    """
    return value.strip(_URL_TRIM).replace("\t", "").replace("\n", "").replace("\r", "")


def url_scheme(value: str) -> str | None:
    """The scheme a browser would use, or ``None``; never raises."""
    match = _SCHEME.match(browser_normalized_url(value))
    return match.group(1).lower() if match else None


def _relative_same_origin(value: str) -> str:
    """A same-origin relative path, by an explicit grammar rather than by parser.

    ``urlsplit("/\\evil.example/x")`` reports no netloc, so Python calls it
    relative; a browser normalises the backslash to a slash and fetches
    ``//evil.example/x``. Rather than chase every such differential, this accepts
    only ASCII URL characters, no scheme, and no authority.
    """
    if not value:
        raise ValueError("must not be empty")
    if value != value.strip(_URL_TRIM):
        raise ValueError(f"{value!r} has leading or trailing whitespace or control characters")
    if "\\" in value:
        raise ValueError(f"{value!r} contains a backslash, which browsers read as '/'")
    illegal = sorted({character for character in value if character not in _URL_SAFE_CHARS})
    if illegal:
        raise ValueError(f"{value!r} contains illegal URL characters {illegal} (percent-encode them)")
    if (scheme := url_scheme(value)) is not None:
        raise ValueError(f"{value!r} names the {scheme!r} scheme; use a same-origin relative path")
    if value.startswith("//"):
        raise ValueError(f"{value!r} is protocol-relative (off-origin)")
    try:
        split = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{value!r} does not parse as a URL ({error})") from None
    if split.netloc:
        raise ValueError(f"{value!r} names an authority; use a same-origin relative path")
    return value


def _push_url(value: str) -> str:
    """``hx-push-url`` is either a boolean or a URL, and both are checked."""
    if value in _BOOLEAN:
        return value
    return _relative_same_origin(value)


def _valid_swap(value: str) -> str:
    parts = value.split()
    if not parts:
        raise ValueError("must not be empty")
    position, *modifiers = parts
    if position not in HX_SWAP_POSITIONS:
        raise ValueError(f"{position!r} is not one of {sorted(HX_SWAP_POSITIONS)}")
    for modifier in modifiers:
        _check_modifier(modifier, HX_SWAP_MODIFIERS, requires_value=True)
    return value


def _valid_trigger(value: str) -> str:
    """htmx trigger grammar, minus event filters.

    ``hx-trigger="click[expr]"`` hands ``expr`` to htmx's evaluator, so a filter
    is script execution in an attribute that looks structural. There is no safe
    subset worth parsing here: filters are refused outright.
    """
    specs = [spec.strip() for spec in value.split(",")]
    if not specs or any(not spec for spec in specs):
        raise ValueError(f"{value!r} has an empty trigger spec")
    for spec in specs:
        if _EVERY.match(spec):
            continue
        if "[" in spec or "]" in spec:
            raise ValueError(f"{spec!r} uses an event filter; filters are evaluated as JavaScript")
        event, *modifiers = spec.split()
        if not _EVENT_NAME.match(event):
            raise ValueError(f"{event!r} is not a valid event name")
        pending: list[str] = list(modifiers)
        while pending:
            modifier = pending.pop(0)
            name, separator, tail = modifier.partition(":")
            # htmx's extended selectors span two tokens: `from:closest form`.
            if name in {"from", "target", "root"} and separator == ":" and tail in _SELECTOR_KEYWORDS:
                if not pending:
                    raise ValueError(f"{modifier!r} needs a selector after {tail!r}")
                _selector_value(pending.pop(0))
                continue
            _check_modifier(modifier, HX_TRIGGER_MODIFIERS, requires_value=True)
    return value


def _plain_text(value: str) -> str:
    """Values that are not URLs still may not smuggle control characters."""
    if _CONTROL_CHARS.search(value):
        raise ValueError("contains control characters")
    if len(value) > 512:
        raise ValueError("is implausibly long for an attribute value")
    return value


def _selector_text(value: str) -> str:
    _plain_text(value)
    for token in value.split():
        if token in _SELECTOR_KEYWORDS:
            continue
        _selector_value(token)
    return value


type RelativeUrl = Annotated[str, AfterValidator(_relative_same_origin)]
type PushUrl = Annotated[str, AfterValidator(_push_url)]
type SwapSpec = Annotated[str, AfterValidator(_valid_swap)]
type TriggerSpec = Annotated[str, AfterValidator(_valid_trigger)]
type SelectorSpec = Annotated[str, AfterValidator(_selector_text)]
type PlainText = Annotated[str, AfterValidator(_plain_text)]


class HtmxAttributes(BaseModel):
    """The documented ``hx-*`` surface, and nothing else.

    ``extra="forbid"`` is the load-bearing line: it is what makes ``hx-swp`` (a
    typo htmx would silently ignore) and any attribute invented by a model fail
    closed rather than pass through unexamined.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    hx_get: RelativeUrl | None = Field(default=None, alias="hx-get")
    hx_post: RelativeUrl | None = Field(default=None, alias="hx-post")
    hx_put: RelativeUrl | None = Field(default=None, alias="hx-put")
    hx_patch: RelativeUrl | None = Field(default=None, alias="hx-patch")
    hx_delete: RelativeUrl | None = Field(default=None, alias="hx-delete")
    hx_push_url: PushUrl | None = Field(default=None, alias="hx-push-url")
    hx_replace_url: PushUrl | None = Field(default=None, alias="hx-replace-url")
    hx_swap: SwapSpec | None = Field(default=None, alias="hx-swap")
    hx_swap_oob: PlainText | None = Field(default=None, alias="hx-swap-oob")
    hx_target: SelectorSpec | None = Field(default=None, alias="hx-target")
    hx_trigger: TriggerSpec | None = Field(default=None, alias="hx-trigger")
    hx_select: SelectorSpec | None = Field(default=None, alias="hx-select")
    hx_select_oob: PlainText | None = Field(default=None, alias="hx-select-oob")
    hx_indicator: SelectorSpec | None = Field(default=None, alias="hx-indicator")
    hx_disabled_elt: SelectorSpec | None = Field(default=None, alias="hx-disabled-elt")
    hx_confirm: PlainText | None = Field(default=None, alias="hx-confirm")
    hx_prompt: PlainText | None = Field(default=None, alias="hx-prompt")
    hx_include: SelectorSpec | None = Field(default=None, alias="hx-include")
    hx_params: PlainText | None = Field(default=None, alias="hx-params")
    hx_encoding: PlainText | None = Field(default=None, alias="hx-encoding")
    hx_sync: PlainText | None = Field(default=None, alias="hx-sync")
    hx_preserve: PlainText | None = Field(default=None, alias="hx-preserve")
    hx_history: PlainText | None = Field(default=None, alias="hx-history")
    hx_history_elt: PlainText | None = Field(default=None, alias="hx-history-elt")
    hx_boost: PlainText | None = Field(default=None, alias="hx-boost")
    hx_ext: PlainText | None = Field(default=None, alias="hx-ext")
    hx_validate: PlainText | None = Field(default=None, alias="hx-validate")
    hx_disinherit: PlainText | None = Field(default=None, alias="hx-disinherit")
    hx_disable: PlainText | None = Field(default=None, alias="hx-disable")
    hx_headers: PlainText | None = Field(default=None, alias="hx-headers")
    hx_vals: PlainText | None = Field(default=None, alias="hx-vals")


# --------------------------------------------------------------------------- #
# CSS -> allowlist.
# --------------------------------------------------------------------------- #

_CSS_ESCAPE = re.compile(r"\\(?:([0-9A-Fa-f]{1,6})[ \t\n]?|(.))", re.DOTALL)
_MAX_CODE_POINT = 0x10FFFF
_REPLACEMENT = "�"


def normalize_css_escapes(name: str) -> ClassName:
    r"""Turn a CSS-escaped identifier into the string an HTML ``class`` holds.

    Variant utilities are written ``.hover\:text-accent`` in a stylesheet and
    ``class="hover:text-accent"`` in markup, so the allowlist must be built from
    the unescaped form. Out-of-range escapes (``\110000``), surrogates and NUL
    become U+FFFD exactly as CSS requires -- and, more to the point, never raise:
    this runs on attacker-supplied stylesheets too.

    Note the asymmetry with :meth:`Nh3Sanitizer._class_violations`: escapes are a
    *CSS* syntax. A ``class`` attribute is compared verbatim, or ``p\-lg`` would
    be a way to spell an allowlisted class that no stylesheet actually matches.
    """

    def replace(match: re.Match[str]) -> str:
        hex_digits, literal = match.groups()
        if hex_digits is None:
            return literal or ""
        code_point = int(hex_digits, 16)
        if code_point == 0 or code_point > _MAX_CODE_POINT or 0xD800 <= code_point <= 0xDFFF:
            return _REPLACEMENT
        return chr(code_point)

    return _CSS_ESCAPE.sub(replace, name)


def class_names_from_css(css_text: str) -> frozenset[ClassName]:
    """Every class selector in a stylesheet, unescaped.

    Recurses through at-rules, functional selectors (``:is()``, ``:where()``,
    ``:not()``) and CSS nesting, because a vocabulary that misses half the
    stylesheet produces false ``UNKNOWN_CLASS`` rejections, and a gate that cries
    wolf gets switched off.
    """
    rules = tinycss2.parse_stylesheet(css_text, skip_comments=True, skip_whitespace=True)
    return frozenset(_classes_in_rules(rules))


def _classes_in_rules(rules: Iterable[Any]) -> set[ClassName]:
    found: set[ClassName] = set()
    for rule in rules:
        match getattr(rule, "type", None):
            case "qualified-rule":
                found |= _classes_in_prelude(rule.prelude)
                found |= _classes_in_nested(rule.content)
            case "at-rule":
                found |= _classes_in_prelude(rule.prelude or ())
                found |= _classes_in_nested(rule.content)
            case _:
                continue
    return found


def _classes_in_nested(content: Sequence[Any] | None) -> set[ClassName]:
    """Nested rules inside a declaration block (CSS nesting, ``@media`` bodies)."""
    if not content:
        return set()
    try:
        parsed = tinycss2.parse_blocks_contents(content, skip_comments=True, skip_whitespace=True)
    except Exception:  # pragma: no cover - defensive: tinycss2 should not raise
        return set()
    return _classes_in_rules(node for node in parsed if getattr(node, "type", "") != "declaration")


def _classes_in_prelude(prelude: Sequence[Any]) -> set[ClassName]:
    found: set[ClassName] = set()
    previous_was_dot = False
    for token in prelude:
        match token.type:
            case "literal" if token.value == ".":
                previous_was_dot = True
                continue
            case "ident" if previous_was_dot:
                found.add(normalize_css_escapes(token.value))
            case "function":
                found |= _classes_in_prelude(token.arguments or ())
            case "[] block" | "() block" | "{} block":
                found |= _classes_in_prelude(token.content or ())
        previous_was_dot = False
    return found


# --------------------------------------------------------------------------- #
# Fragment walk.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Element:
    tag: str
    attributes: Attributes
    locator: Locator
    depth: int

    def get(self, name: AttrName) -> AttrValue | None:
        for key, value in self.attributes:
            if key == name:
                return value
        return None


type _Token = tuple[str, str, Attributes] | tuple[str, str]


class _FragmentWalker(HTMLParser):
    """One pass producing a comparable token stream, located elements, and defects.

    A single walk rather than three: the nh3 diff needs a canonical token stream,
    the class and attribute checks need locators, and the truncation check needs
    the open-element stack. Deriving them together is what keeps the locator in a
    violation pointing at the element the diff is complaining about.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []
        self.elements: list[_Element] = []
        self.roots: list[_Element] = []
        self.unclosed: list[str] = []
        self.stray_end_tags: list[str] = []
        self.mismatched: list[str] = []
        self.declarations: list[str] = []
        self.loose_root_text: bool = False
        self._open: list[tuple[str, Locator]] = []
        self._counts: list[dict[str, int]] = [{}]

    # -- construction ----------------------------------------------------- #

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._auto_close_for(tag)
        counts = self._counts[-1]
        counts[tag] = counts.get(tag, 0) + 1
        parent = self._open[-1][1] if self._open else ""
        step = f"{tag}:nth-of-type({counts[tag]})" if counts[tag] > 1 else tag
        locator = f"{parent} > {step}" if parent else step

        # CR is normalised to LF by every HTML parser, ours and the browser's
        # alike, so folding it here keeps a CRLF-authored fragment from looking
        # like the sanitiser rewrote an attribute.
        attributes: Attributes = tuple(
            sorted(
                (name.lower(), "" if value is None else value.replace("\r\n", "\n").replace("\r", "\n"))
                for name, value in attrs
            )
        )
        element = _Element(tag=tag, attributes=attributes, locator=locator, depth=len(self._open))
        self.elements.append(element)
        if not self._open:
            self.roots.append(element)
        self.tokens.append(("start", tag, attributes))

        if tag in VOID_ELEMENTS:
            self.tokens.append(("end", tag))
            return
        self._open.append((tag, locator))
        self._counts.append({})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self._close_top()

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS or not any(open_tag == tag for open_tag, _ in self._open):
            # Nothing to close: a browser drops this, and so does nh3, so the
            # token streams would still match. That silent repair is the bug.
            self.stray_end_tags.append(tag)
            return
        while self._open and self._open[-1][0] != tag:
            top = self._open[-1][0]
            if top not in OPTIONAL_END_TAGS:
                self.mismatched.append(top)
            self._close_top()
        self._close_top()

    def handle_data(self, data: str) -> None:
        collapsed = " ".join(data.split())
        if not collapsed:
            return
        if not self._open:
            self.loose_root_text = True
        self.tokens.append(("text", collapsed))

    def handle_decl(self, decl: str) -> None:
        self.declarations.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.declarations.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.declarations.append(f"<![{data}]>")

    def close(self) -> None:
        super().close()
        while self._open:
            tag, _ = self._open[-1]
            if tag not in OPTIONAL_END_TAGS:
                self.unclosed.append(tag)
            self._close_top()

    # -- helpers ---------------------------------------------------------- #

    def _close_top(self) -> None:
        tag, _ = self._open.pop()
        self._counts.pop()
        self.tokens.append(("end", tag))

    def _auto_close_for(self, tag: str) -> None:
        while self._open:
            top = self._open[-1][0]
            if top == "colgroup" and tag not in _COLGROUP_KEEPS:
                self._close_top()
                continue
            if tag in _CLOSED_BY_START.get(top, frozenset()):
                self._close_top()
                continue
            return


def _walk(fragment_html: FragmentHtml) -> _FragmentWalker:
    walker = _FragmentWalker()
    walker.feed(fragment_html)
    walker.close()
    return walker


# --------------------------------------------------------------------------- #
# The adapter.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Nh3Sanitizer:
    """The shipped :class:`~ui_servo.ports.sanitizer.SanitizerPort`.

    Construct it from a contract (:meth:`from_contract`) so the class vocabulary
    is derived from the same reference signal every other tier is judged against;
    ``extra_classes`` is the reviewed escape hatch, typically fed from
    :func:`class_names_from_css` over the project's token stylesheet.
    """

    class_allowlist: frozenset[ClassName]
    require_span_id: bool = True
    _cleaner: nh3.Cleaner = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # The allowlist may arrive from CSS selectors, where escapes are syntax;
        # class *attributes* are compared verbatim (see _class_violations).
        normalized = frozenset(normalize_css_escapes(name) for name in self.class_allowlist)
        object.__setattr__(self, "class_allowlist", normalized)
        object.__setattr__(
            self,
            "_cleaner",
            nh3.Cleaner(
                tags=set(ALLOWED_TAGS),
                clean_content_tags=set(CLEAN_CONTENT_TAGS),
                attributes={tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()},
                generic_attribute_prefixes=set(GENERIC_ATTRIBUTE_PREFIXES),
                url_schemes=set(URL_SCHEMES),
                link_rel=None,
                strip_comments=True,
            ),
        )

    @classmethod
    def from_contract(
        cls,
        contract: DirectionContract,
        *,
        extra_classes: Iterable[ClassName] = (),
        require_span_id: bool = True,
    ) -> Self:
        return cls(
            class_allowlist=contract.class_allowlist_seed() | frozenset(extra_classes),
            require_span_id=require_span_id,
        )

    # -- the port --------------------------------------------------------- #

    def check(self, fragment_html: FragmentHtml) -> SanitizeResult:
        """Never raises. Every path out of here is a :class:`SanitizeResult`."""
        try:
            cleaned = self._cleaner.clean(fragment_html)
            walker = _walk(fragment_html)
            violations: list[Violation] = [
                *_structure_violations(walker),
                *self._stripped_violations(cleaned, walker),
                *self._class_violations(walker),
                *_attribute_violations(walker),
                *self._span_id_violations(walker),
            ]
        except Exception as error:  # pragma: no cover - the belt for the braces
            return SanitizeResult.rejected(
                (
                    Violation(
                        kind=ViolationKind.MALFORMED_FRAGMENT,
                        locator=":root",
                        detail=f"fragment could not be analysed: {type(error).__name__}: {error}",
                    ),
                )
            )
        match violations:
            case []:
                return SanitizeResult.ok(cleaned)
            case found:
                return SanitizeResult.rejected(sorted(set(found)))

    # -- check 1: nh3 ----------------------------------------------------- #

    def _stripped_violations(self, cleaned: str, walker: _FragmentWalker) -> list[Violation]:
        """Attribute nh3's edits to individual elements, in document order.

        The alignment is positional rather than by ``(tag, attribute)`` sets: nh3
        only ever deletes, so its output is a subsequence of the input, and
        walking the two in step is what stops a surviving ``<a href="/ok">`` from
        vouching for the ``<a href="ftp://...">`` three lines below it.
        """
        after = _walk(cleaned)
        if walker.tokens == after.tokens:
            return []

        found: list[Violation] = []
        survivors = after.elements
        index = 0
        for element in walker.elements:
            if index < len(survivors) and survivors[index].tag == element.tag:
                found += _attribute_diff(element, survivors[index])
                index += 1
                continue
            found.append(
                Violation(
                    kind=ViolationKind.DISALLOWED_TAG,
                    locator=element.locator,
                    detail=f"<{element.tag}> is outside the fragment policy and was stripped",
                )
            )
        if found:
            return found
        return [
            Violation(
                kind=ViolationKind.DISALLOWED_TAG,
                locator=walker.roots[0].locator if walker.roots else ":root",
                detail="sanitised output differs structurally from the input (content was removed)",
            )
        ]

    # -- check 2: class allowlist ----------------------------------------- #

    def _class_violations(self, walker: _FragmentWalker) -> list[Violation]:
        """Compare class tokens verbatim, split the way HTML splits them.

        No unescaping (``p\\-lg`` is not ``p-lg`` to a browser reading an
        attribute) and no Unicode whitespace splitting (U+00A0 does not separate
        classes), because both would let an unreviewed token wear an approved
        token's name.
        """
        found: list[Violation] = []
        for element in walker.elements:
            raw = element.get("class")
            if raw is None:
                continue
            for name in _ASCII_WHITESPACE.split(raw):
                if not name or name in self.class_allowlist:
                    continue
                found.append(
                    Violation(
                        kind=ViolationKind.UNKNOWN_CLASS,
                        locator=element.locator,
                        detail=f"class {name!r} is not in the contract's allowlist",
                    )
                )
        return found

    # -- check 3b: the span id -------------------------------------------- #

    def _span_id_violations(self, walker: _FragmentWalker) -> list[Violation]:
        if not self.require_span_id:
            return []
        found: list[Violation] = []
        if walker.loose_root_text:
            found.append(
                Violation(
                    kind=ViolationKind.MISSING_SPAN_ID,
                    locator=":root",
                    detail="text at the fragment root belongs to no element and cannot be attributed",
                )
            )
        if not walker.roots:
            found.append(
                Violation(
                    kind=ViolationKind.MISSING_SPAN_ID,
                    locator=":root",
                    detail="fragment contains no element to carry a data-span-id",
                )
            )
        for root in walker.roots:
            match root.get(SPAN_ID_ATTRIBUTE):
                case None:
                    found.append(
                        Violation(
                            kind=ViolationKind.MISSING_SPAN_ID,
                            locator=root.locator,
                            detail=(
                                f"fragment root <{root.tag}> has no {SPAN_ID_ATTRIBUTE}; "
                                "observations could not be attributed to it"
                            ),
                        )
                    )
                case value if not _SPAN_ID.fullmatch(value):
                    found.append(
                        Violation(
                            kind=ViolationKind.INVALID_ATTRIBUTE_VALUE,
                            locator=root.locator,
                            detail=f"{SPAN_ID_ATTRIBUTE}={value!r} is not a lowercase slug",
                        )
                    )
                case _:
                    continue
        return found


def _attribute_diff(before: _Element, after: _Element) -> list[Violation]:
    surviving = dict(after.attributes)
    found: list[Violation] = []
    for name, value in before.attributes:
        if name not in surviving:
            found.append(
                Violation(
                    kind=ViolationKind.DISALLOWED_ATTRIBUTE,
                    locator=before.locator,
                    detail=f"attribute {name!r} on <{before.tag}> was stripped{_why(name)}",
                )
            )
        elif surviving[name] != value:
            found.append(
                Violation(
                    kind=ViolationKind.DISALLOWED_ATTRIBUTE,
                    locator=before.locator,
                    detail=f"attribute {name!r} on <{before.tag}> was rewritten by the sanitiser",
                )
            )
    return found


def _why(name: str) -> str:
    if name.startswith("on"):
        return " (inline event handler)"
    if name == "style":
        return " (use tokens.css, not inline style)"
    return ""


# -- structural defects ----------------------------------------------------- #


def _structure_violations(walker: _FragmentWalker) -> list[Violation]:
    """Anything a browser would silently repair is a rejection here.

    Truncated output, a stray ``</script>``, ``<span></div>``, a stowaway
    doctype: each is a place where our parse and the browser's could diverge, and
    a Tier 0 gate that guesses is a Tier 0 gate that can be tricked.
    """
    found: list[Violation] = []
    if walker.unclosed:
        found.append(
            Violation(
                kind=ViolationKind.MALFORMED_FRAGMENT,
                locator=":root",
                detail=f"unclosed elements: {_tags(walker.unclosed)}",
            )
        )
    if walker.stray_end_tags:
        found.append(
            Violation(
                kind=ViolationKind.MALFORMED_FRAGMENT,
                locator=":root",
                detail=f"end tags closing nothing: {_tags(walker.stray_end_tags)}",
            )
        )
    if walker.mismatched:
        found.append(
            Violation(
                kind=ViolationKind.MALFORMED_FRAGMENT,
                locator=":root",
                detail=f"elements closed out of order: {_tags(walker.mismatched)}",
            )
        )
    if walker.declarations:
        found.append(
            Violation(
                kind=ViolationKind.MALFORMED_FRAGMENT,
                locator=":root",
                detail=f"a fragment may not contain declarations or processing instructions: {sorted(set(walker.declarations))}",
            )
        )
    return found


def _tags(tags: Sequence[str]) -> str:
    return ", ".join(f"<{tag}>" for tag in sorted(set(tags)))


# -- check 3a: the attribute schema ---------------------------------------- #


def _attribute_violations(walker: _FragmentWalker) -> list[Violation]:
    found: list[Violation] = []
    for element in walker.elements:
        found += _url_scheme_violations(element)
        found += _htmx_violations(element)
    return found


def _htmx_violations(element: _Element) -> list[Violation]:
    """Validate ``hx-*`` and ``data-hx-*`` as the single namespace htmx treats them as."""
    found: list[Violation] = []
    canonical: dict[str, str] = {}
    spelling: dict[str, str] = {}

    for name, value in element.attributes:
        if _HX_ON.match(name):
            found.append(
                Violation(
                    kind=ViolationKind.DISALLOWED_ATTRIBUTE,
                    locator=element.locator,
                    detail=f"{name!r} is inline JavaScript (htmx evaluates it); use a reviewed script",
                )
            )
            continue
        if name.startswith(HX_DATA_PREFIX):
            key = "hx-" + name[len(HX_DATA_PREFIX) :]
        elif name.startswith("hx-"):
            key = name
        else:
            continue
        if key in canonical and canonical[key] != value:
            found.append(
                Violation(
                    kind=ViolationKind.INVALID_ATTRIBUTE_VALUE,
                    locator=element.locator,
                    detail=f"{name!r} and {spelling[key]!r} are the same htmx attribute with different values",
                )
            )
            continue
        canonical[key] = value
        spelling[key] = name

    if not canonical:
        return found
    try:
        HtmxAttributes.model_validate(canonical)
    except ValidationError as error:
        found += _from_validation_error(error, element, spelling)
    return found


def _urls_in(name: str, value: str) -> Iterator[str]:
    """Every URL hiding in one attribute value."""
    if name in _URL_ATTRIBUTES:
        yield value
    elif name in _SRCSET_ATTRIBUTES:
        for candidate in value.split(","):
            parts = candidate.split()
            if parts:
                yield parts[0]
    elif name in _URL_LIST_ATTRIBUTES:
        yield from value.split()


def _url_scheme_violations(element: _Element) -> list[Violation]:
    found: list[Violation] = []
    for name, value in element.attributes:
        for url in _urls_in(name, value):
            try:
                scheme = url_scheme(url)
            except Exception as error:
                found.append(
                    Violation(
                        kind=ViolationKind.DISALLOWED_URL,
                        locator=element.locator,
                        detail=f"{name}={url!r} could not be parsed as a URL ({type(error).__name__})",
                    )
                )
                continue
            if scheme is None:
                continue
            if scheme in _DANGEROUS_SCHEMES:
                found.append(
                    Violation(
                        kind=ViolationKind.DISALLOWED_URL,
                        locator=element.locator,
                        detail=f"{name}={url!r} uses the {scheme!r} scheme",
                    )
                )
                continue
            if scheme not in URL_SCHEMES:
                found.append(
                    Violation(
                        kind=ViolationKind.DISALLOWED_URL,
                        locator=element.locator,
                        detail=f"{name}={url!r} uses the unsupported {scheme!r} scheme",
                    )
                )
    return found


def _from_validation_error(
    error: ValidationError, element: _Element, spelling: dict[str, str]
) -> list[Violation]:
    found: list[Violation] = []
    for detail in error.errors():
        canonical = str(detail["loc"][0]) if detail["loc"] else "hx-*"
        attribute = spelling.get(canonical, canonical)
        message = detail["msg"].removeprefix("Value error, ")
        found.append(
            Violation(
                kind=_kind_for(canonical, str(detail["type"])),
                locator=element.locator,
                detail=f"{attribute}: {message}",
            )
        )
    return found


def _kind_for(attribute: str, error_type: str) -> ViolationKind:
    match (attribute, error_type):
        case (_, "extra_forbidden"):
            return ViolationKind.UNKNOWN_ATTRIBUTE
        case (name, _) if name in HX_URL_ATTRIBUTES:
            return ViolationKind.DISALLOWED_URL
        case (name, _) if name in {"hx-push-url", "hx-replace-url"}:
            return ViolationKind.DISALLOWED_URL
        case _:
            return ViolationKind.INVALID_ATTRIBUTE_VALUE


def default_sanitizer(contract: DirectionContract, *, tokens_css: str | None = None) -> Nh3Sanitizer:
    """The sanitiser the loop actually ships: contract seed plus the token sheet.

    Passing ``tokens_css`` (usually :meth:`DirectionContract.to_css_custom_properties`
    output concatenated with the project's utility sheet) is what lets a real
    stylesheet, rather than a hand-maintained list, define the vocabulary.
    """
    extra = class_names_from_css(tokens_css) if tokens_css else frozenset()
    return Nh3Sanitizer.from_contract(contract, extra_classes=extra)
