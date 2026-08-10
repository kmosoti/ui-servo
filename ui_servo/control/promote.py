"""Promotion: the human's pick becomes the site, with proof it was gated.

The last step of a round is not a verdict, it is a decision, and this is the only
sanctioned way that decision reaches something a visitor can load. The pick is
re-checked through the class-0 sanitiser here rather than trusted from the round,
because the file on disk is what will be served and the file on disk is what must
be clean; then it is written to ``site/promoted/<part>.html`` under a
provenance comment the Rust side verifies on every request.

    <!-- ui-servo: gated round=<n> sha256=<hash> -->

The comment is not decoration, and it is also not a signature. Be precise about
which, because earlier versions of this docstring were not.

The digest is unkeyed and lives inside the file it describes, so it establishes
*integrity*, not *authorship*. It catches: a hand edit, a half-copied fragment, a
stale promotion, a file that skipped the loop by mistake, a truncated write, a
line-ending mangling. Those are the failures that happen in a working repo, they
are all silent, and every one of them is now loud.

It does not catch a deliberate forgery. Anyone who can write to the promotion
directory can also write a fresh digest -- ``sha256`` of a preimage this file
documents in full -- and the server will serve it. That was demonstrated in
review with a `<script>` tag and a hand-computed hash. No file-local check can do
better: a hash that sits next to the thing it vouches for proves the two agree,
never that either is authorised. Closing it needs a key the writer holds and the
reader trusts, which is a deployment concern this module deliberately does not
invent.

So: the provenance comment tells you the markup on disk is the markup that was
gated. It does not tell you nobody replaced both.

Standard library, domain and ports only. The sanitiser arrives as a port, so a
promotion can be dry-run against a fake in tests without touching a real file;
the adapter that satisfies it is chosen in :mod:`ui_servo.cli.promote`.
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

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

``.`` and ``..`` match the character class and are excluded separately: they are
legal-looking tokens that name a directory rather than a file, and the Rust
reader rejects them. A writer that happily reports "promoted" for a path the
server will never load is worse than one that refuses.
"""

_RESERVED_TOKENS: Final[frozenset[str]] = frozenset({".", ".."})


def _checked(value: str, *, field: str) -> str:
    if not _SAFE_TOKEN.match(value) or value in _RESERVED_TOKENS:
        raise PromotionRefused(
            f"{field} must be 1-64 characters of [A-Za-z0-9._-], and not '.' or '..'; "
            f"got {value!r}. It becomes both a path component and part of the "
            "provenance comment."
        )
    return value

DEFAULT_FRAGMENTS_DIR: Final[Path] = Path("site/promoted")

_START_TAG: Final[re.Pattern[str]] = re.compile(
    r"""<[A-Za-z][-A-Za-z0-9]*(?:"[^"<]*"|'[^'<]*'|[^<>"'])*/?>"""
)
"""A start tag, quote-aware, so a `>` inside a quoted value does not end it.

End tags, comments, doctypes and CDATA are deliberately not matched: none of them
carry attributes, so none of them are this function's business.

Quote-awareness is not fussiness. `<[^>]*>` ends the tag at the first `>` even
inside a value, so `<section title="a > b" data-span-id="x">` would keep its
stale id -- and the failure is silent, because the result still sanitises, still
hashes and still serves.

Excluding `<` from every branch is not decoration either. With it allowed, each
`<` that begins no tag -- a truncated fragment, an unterminated quote, prose
containing `a<b` -- made the alternation rescan to end of input on failure,
quadratically: 64 kB of `x<y ` took 23.8 seconds. A start tag cannot contain a
bare `<` anyway (nh3 escapes it), so a failed match now stops at the next `<`
instead of at the end of the document.
"""

_HTML_SPACE: Final[str] = r"[ \t\n\f\r]"
r"""HTML5 whitespace, spelled out.

Python's ``\s`` is Unicode-aware and matches U+00A0, U+000B and U+001C-U+001F,
none of which separate attributes in HTML -- a parser reads them as part of the
attribute *name*. Using ``\s`` here meant a non-breaking space between two
attributes made this function delete the wrong one, absorb an attribute into the
element name, or invent an element that had not existed. Three separate
corruptions from one character class."""

_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(
    rf"""{_HTML_SPACE}+(?P<name>[^ \t\n\f\r=/>]+)"""
    rf"""(?:{_HTML_SPACE}*={_HTML_SPACE}*(?:"[^"]*"|'[^']*'|[^ \t\n\f\r>]*))?""",
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


_TAG_NAME: Final[re.Pattern[str]] = re.compile(r"<[A-Za-z][-A-Za-z0-9]*")
"""Just the `<` and the element name, so the attribute walk starts in the right
place.

This replaced `tag.find(" ")`, which was wrong in a way worth keeping a note
about: HTML separates attributes with *any* whitespace, so a tag written across
several lines has no space until whichever quoted value happens to contain one.
`<img\\ndata-span-id="x"\\ntitle="a b">` therefore began its walk in the middle of
`title`, and the rebuilt tag was garbage. Differential-fuzzing the rewrite
against :mod:`html.parser` found it in 4000 cases; reading the code did not.
"""


_TAG_END: Final[re.Pattern[str]] = re.compile(rf"""{_HTML_SPACE}*/?>\Z""")
"""What a start tag must end with once every attribute has been consumed."""


def _strip_tag_attribute(tag: str) -> str:
    """Rebuild one start tag without its span id, or return it untouched.

    **Fails closed.** If the attribute walk cannot account for every byte between
    the element name and the closing `>`, the tag is returned exactly as it came
    in. The alternative -- splice together the part that parsed and append the
    rest -- is what produced the worst class of bug in this function's history:
    `<p data-span-id="y"title="x">` became `<ptitle="x">`, an element that does
    not exist, and `<p = data-span-id="x">` silently kept its id. Both look like
    ordinary output, both still hash, both still serve.

    A no-op leaves a stale span id in place, which `promote` then refuses
    outright. That is a worse-looking failure and a much better one: it is
    visible.
    """
    name = _TAG_NAME.match(tag)
    if name is None:
        return tag
    out = [name.group(0)]
    position = name.end()
    dropped = False
    while (attribute := _ATTRIBUTE.match(tag, position)) is not None:
        if attribute.group("name").lower() == _SPAN_ID:
            dropped = True
        else:
            out.append(attribute.group(0))
        position = attribute.end()

    tail = tag[position:]
    if not _TAG_END.match(tail):
        # Unconsumable input: something in this tag is not an attribute as far as
        # this scanner is concerned, so it does not get to guess.
        return tag
    if not dropped:
        return tag
    # Removing an attribute can change the meaning of the one before it: an
    # unquoted value ends only at whitespace or `>`, so closing up the gap can
    # extend the previous value or the element name. Keep one separator.
    if tail.startswith("/") and out[-1] and out[-1][-1] not in " \t\n\f\r":
        out.append(" ")
    out.append(tail)
    return "".join(out)


def strip_span_ids(markup: str) -> str:
    """Remove candidate span ids so the server's fresh one is the only one.

    Only attribute *names* are matched. Text that talks about the attribute is
    left exactly as written -- a fragment documenting the probe is a fragment,
    not a mistake -- and so is any other attribute's value.
    """
    return _START_TAG.sub(lambda tag: _strip_tag_attribute(tag.group(0)), markup)


_SPAN_ID_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(
    rf"""{_HTML_SPACE}{_SPAN_ID}{_HTML_SPACE}*=""", re.IGNORECASE
)
"""Used to check the strip's own work, not to do it.

:func:`_strip_tag_attribute` fails closed: a tag it cannot fully account for is
returned untouched, which leaves the id in place. That is the right call at the
tag level -- a visible leftover beats a silent splice -- but it must not then be
promoted, so :func:`promote` refuses. The two halves together turn every parsing
gap in this module from "corrupt the markup quietly" into "refuse the promotion
loudly", which is the only trade this file should ever make.
"""


class PromotionRefused(Exception):
    """The pick did not pass the gate, so it does not become the site."""


@dataclass(frozen=True, slots=True)
class Promotion:
    part: str
    round_id: str
    path: Path
    digest: str


DIGEST_VERSION: Final[str] = "ui-servo/1"
"""Format tag in the hash preimage, so a future change to what is covered cannot
be mistaken for tampering (or, worse, tampering mistaken for an old format)."""


def body_digest(markup: str, *, part: str, round_id: str) -> str:
    """Hash of everything the provenance comment asserts, not just the markup.

    Covering the body alone was not enough, and the gap was not theoretical: the
    comment says *which round produced this*, and with a body-only digest that
    claim was freely editable. Changing `round=4` to `round=99` left the body
    hash valid, so the file loaded, cached at boot, and served a page attributing
    itself to a round that never made it. Verified against the running binary.

    A comment cannot hash itself, so the fix is not to cover the comment but to
    bind its values into the preimage: part and round now change the digest.
    Editing either produces a mismatch, which is the same refusal an edited body
    already got.

    Newline-separated with a version tag, and the fields are `_checked` *here*
    rather than only in the callers: with `part` free-form, ``("hero\n4", "x")``
    and ``("hero", "4")`` produce the same preimage, so a third caller would
    inherit an ambiguous digest silently. The check is cheap; the docstring
    claiming an invariant the function did not enforce was not.

    Line endings are normalised first. Python writes with ``newline=None``, which
    emits CRLF on Windows, and git with ``core.autocrlf=true`` rewrites the
    committed file on checkout -- so without this, the promoter's own output is
    refused by its own reader as "edited after promotion", which is both wrong
    and an alarming thing to tell somebody.
    """
    part = _checked(part, field="part")
    round_id = _checked(round_id, field="round")
    preimage = "\n".join((DIGEST_VERSION, part, round_id, _normalise_newlines(markup).strip()))
    return sha256(preimage.encode("utf-8")).hexdigest()


def _normalise_newlines(markup: str) -> str:
    """CRLF and CR both become LF, so the hash is about content, not checkout."""
    return markup.replace("\r\n", "\n").replace("\r", "\n")


def render_promoted_file(markup: str, *, part: str, round_id: str) -> str:
    part = _checked(part, field="part")
    round_id = _checked(round_id, field="round")
    body = _normalise_newlines(markup).strip()
    digest = body_digest(body, part=part, round_id=round_id)
    header = PROVENANCE_TEMPLATE.format(round=round_id, digest=digest)
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

    # Strip after the gate, so what the sanitiser approved is what gets hashed.
    body = strip_span_ids(result.cleaned_html or markup).strip()
    if _SPAN_ID_ATTRIBUTE.search(body):
        raise PromotionRefused(
            f"{part}: a data-span-id survived the strip, so this markup would be served "
            "carrying two join keys and the probe would file live readings under a "
            "candidate that no longer exists. The strip fails closed on tags it cannot "
            "fully parse, so this means the markup contains something it does not "
            "understand -- report the fragment rather than hand-editing it."
        )

    fragments_dir.mkdir(parents=True, exist_ok=True)
    path = fragments_dir / f"{part}.html"
    # newline="" so the bytes written are exactly the bytes hashed, on every
    # platform. The default translates "\n" to os.linesep.
    path.write_text(
        render_promoted_file(body, part=part, round_id=round_id),
        encoding="utf-8",
        newline="",
    )
    return Promotion(
        part=part,
        round_id=round_id,
        path=path,
        digest=body_digest(body, part=part, round_id=round_id),
    )
