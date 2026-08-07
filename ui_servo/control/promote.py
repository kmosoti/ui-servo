"""Promotion: the human's pick becomes the site, with proof it was gated.

The last step of a round is not a verdict, it is a decision, and this is the only
sanctioned way that decision reaches something a visitor can load. The pick is
re-checked through the class-0 sanitiser here rather than trusted from the round,
because the file on disk is what will be served and the file on disk is what must
be clean; then it is written to ``site/promoted/<part>.html`` under a
provenance comment the Rust side verifies on every request.

    <!-- ui-servo: gated round=<n> sha256=<hash> -->

The comment is not decoration. The sanitiser is Python and runs here, once; the
server is Rust and runs continuously; the comment plus the hash is the only
evidence the running site has that the markup it is about to serve ever passed a
gate at all. Editing a promoted file by hand breaks the hash, which is the
intended outcome -- an ungated edit and a hand-written fragment are the same
thing wearing different clothes.

Standard library, domain and ports only. The sanitiser arrives as a port, so a
promotion can be dry-run against a fake in tests without touching a real file;
the adapter that satisfies it is chosen in :mod:`ui_servo.cli.promote`.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from ui_servo.domain.contract import DirectionContract
from ui_servo.ports.sanitizer import SanitizerPort

PROVENANCE_TEMPLATE: Final[str] = "<!-- ui-servo: gated round={round} sha256={digest} -->"

_SAFE_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
"""What may appear in a path component or inside the provenance comment.

Both `part` and `round_id` reach a filesystem path *and* the first line of a
served file, so both are slugs or nothing. Unvalidated they are two separate
holes: a `round_id` containing a newline closes the comment and appends markup
that the recorded hash still vouches for -- the sanitiser ran before that text
existed -- and a `part` containing `..` or a slash escapes the fragments
directory in both the writer and the reader.
"""


def _checked(value: str, *, field: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise PromotionRefused(
            f"{field} must be 1-64 characters of [A-Za-z0-9._-]; got {value!r}. "
            "It becomes both a path component and part of the provenance comment."
        )
    return value

DEFAULT_FRAGMENTS_DIR: Final[Path] = Path("site/promoted")

_START_TAG: Final[re.Pattern[str]] = re.compile(
    r"""<[A-Za-z][-A-Za-z0-9]*(?:"[^"]*"|'[^']*'|[^>"'])*/?>"""
)
"""A start tag, quote-aware, so a `>` inside a quoted value does not end it.

End tags, comments, doctypes and CDATA are deliberately not matched: none of them
carry attributes, so none of them are this function's business.

Quote-awareness is not fussiness. `<[^>]*>` ends the tag at the first `>` even
inside a value, so `<section title="a > b" data-span-id="x">` would keep its
stale id -- and the failure is silent, because the result still sanitises, still
hashes and still serves.
"""

_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(
    r"""\s+(?P<name>[^\s=/>]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?""",
    re.IGNORECASE,
)
"""One attribute, name captured, value consumed whole whatever its quoting.

Walking a tag attribute by attribute is what makes the difference between
removing an attribute and corrupting a value. A pattern that simply searches the
tag text for `data-span-id=...` also matches *inside* another attribute's value:
`<p title="a data-span-id=&quot;fake&quot; b">` would be rewritten to
`<p title="a b">`, silently editing markup a critic already approved and then
hashing the corruption as though it had been judged.
"""

_SPAN_ID: Final[str] = "data-span-id"
"""The join key. During a round the probe files every reading under it and the
panel cites it in verdicts; after promotion it is a lie, because the server's
`frame` stamps a fresh one on the element it serves. Two ids on one element means
the probe -- which reads the nearest -- attributes live evidence to a candidate
that stopped existing when the round ended. Not an error: a mis-filed reading,
which is quieter and worse."""


def _strip_tag_attribute(tag: str) -> str:
    """Rebuild one start tag without its span id, leaving every value intact."""
    name_end = tag.find(" ") if " " in tag else len(tag) - 1
    out = [tag[:name_end]]
    position = name_end
    while (attribute := _ATTRIBUTE.match(tag, position)) is not None:
        if attribute.group("name").lower() != _SPAN_ID:
            out.append(attribute.group(0))
        position = attribute.end()
    out.append(tag[position:])
    return "".join(out)


def strip_span_ids(markup: str) -> str:
    """Remove candidate span ids so the server's fresh one is the only one.

    Only attribute *names* are matched. Text that talks about the attribute is
    left exactly as written -- a fragment documenting the probe is a fragment,
    not a mistake -- and so is any other attribute's value.
    """
    return _START_TAG.sub(lambda tag: _strip_tag_attribute(tag.group(0)), markup)


class PromotionRefused(Exception):
    """The pick did not pass the gate, so it does not become the site."""


@dataclass(frozen=True, slots=True)
class Promotion:
    part: str
    round_id: str
    path: Path
    digest: str


def body_digest(markup: str) -> str:
    """Hash of the markup the comment will vouch for.

    Trimmed before hashing so that the Rust reader, which trims what it reads,
    computes the same number. A whitespace-only difference between writer and
    reader would present as a forged file.
    """
    return sha256(markup.strip().encode("utf-8")).hexdigest()


def render_promoted_file(markup: str, *, round_id: str) -> str:
    round_id = _checked(round_id, field="round")
    body = markup.strip()
    header = PROVENANCE_TEMPLATE.format(round=round_id, digest=body_digest(body))
    return f"{header}\n{body}\n"


def promote(
    markup: str,
    *,
    part: str,
    round_id: str,
    sanitizer: SanitizerPort,
    fragments_dir: Path = DEFAULT_FRAGMENTS_DIR,
) -> Promotion:
    """Gate the pick, then write it where the site will serve it."""
    part = _checked(part, field="part")
    round_id = _checked(round_id, field="round")
    result = sanitizer.check(markup)
    if not result.accepted:
        raise PromotionRefused(
            f"{part}: the pick did not survive the class-0 gate, so it is not promoted:\n  "
            + "\n  ".join(violation.detail for violation in result.violations)
        )

    fragments_dir.mkdir(parents=True, exist_ok=True)
    path = fragments_dir / f"{part}.html"
    # Strip after the gate, so what the sanitiser approved is what gets hashed.
    body = strip_span_ids(result.cleaned_html or markup).strip()
    path.write_text(render_promoted_file(body, round_id=round_id), encoding="utf-8")
    return Promotion(part=part, round_id=round_id, path=path, digest=body_digest(body))
