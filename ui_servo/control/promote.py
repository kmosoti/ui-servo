"""Promotion: the human's pick becomes the site, with proof it was gated.

The last step of a round is not a verdict, it is a decision, and this is the only
sanctioned way that decision reaches something a visitor can load. The pick is
re-checked through the class-0 sanitiser here rather than trusted from the round,
because the file on disk is what will be served and the file on disk is what must
be clean; then it is written to ``site/assets/fragments/<part>.html`` under a
provenance comment the Rust side verifies on every request.

    <!-- ui-servo: gated round=<n> sha256=<hash> -->

The comment is not decoration. The sanitiser is Python and runs here, once; the
server is Rust and runs continuously; the comment plus the hash is the only
evidence the running site has that the markup it is about to serve ever passed a
gate at all. Editing a promoted file by hand breaks the hash, which is the
intended outcome -- an ungated edit and a hand-written fragment are the same
thing wearing different clothes.

Standard library, domain and ports only. The sanitiser arrives as a port, so a
promotion can be dry-run against a fake in tests without touching a real file.
"""

import argparse
import importlib
import re
import sys
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

DEFAULT_FRAGMENTS_DIR: Final[Path] = Path("site/assets/fragments")


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
    body = (result.cleaned_html or markup).strip()
    path.write_text(render_promoted_file(body, round_id=round_id), encoding="utf-8")
    return Promotion(part=part, round_id=round_id, path=path, digest=body_digest(body))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ui_servo.control.promote",
        description="Promote a picked candidate fragment to the live site.",
    )
    parser.add_argument("--pick", type=Path, required=True, help="the chosen candidate .html")
    parser.add_argument("--part", required=True, help="which part of the site this is")
    parser.add_argument("--round", dest="round_id", required=True, help="the round it came from")
    parser.add_argument("--contract", type=Path, default=Path("direction/direction.toml"))
    parser.add_argument("--fragments-dir", type=Path, default=DEFAULT_FRAGMENTS_DIR)
    args = parser.parse_args(argv)

    # The composition root has to name an adapter, and reaches it the way
    # servo.main does: through importlib inside the function body, so that
    # importing this module never drags nh3 in and the dependency rule in
    # tests/test_architecture.py still describes this file truthfully.
    nh3_sanitizer = importlib.import_module("ui_servo.adapters.nh3_sanitizer")
    default_sanitizer = nh3_sanitizer.default_sanitizer

    contract = DirectionContract.from_toml(args.contract.read_text(encoding="utf-8"))
    try:
        promotion = promote(
            args.pick.read_text(encoding="utf-8"),
            part=args.part,
            round_id=args.round_id,
            sanitizer=default_sanitizer(contract),
            fragments_dir=args.fragments_dir,
        )
    except PromotionRefused as refusal:
        print(refusal, file=sys.stderr)
        return 1

    print(f"promoted {promotion.part} from round {promotion.round_id} -> {promotion.path}")
    print(f"  sha256 {promotion.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
