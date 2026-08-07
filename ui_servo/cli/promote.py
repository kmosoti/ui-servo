"""``python -m ui_servo.cli.promote`` — write a picked candidate to the site.

Argument parsing and one adapter choice (the nh3 sanitiser). The decision about
whether a pick may be promoted is :func:`ui_servo.control.promote.promote`'s, and
stays there.
"""

import argparse
import sys
from pathlib import Path

from ui_servo.adapters.nh3_sanitizer import default_sanitizer
from ui_servo.control.promote import DEFAULT_FRAGMENTS_DIR, PromotionRefused, promote
from ui_servo.domain.contract import DirectionContract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ui_servo.cli.promote",
        description="Promote a picked candidate fragment to the live site.",
    )
    parser.add_argument("--pick", type=Path, required=True, help="the chosen candidate .html")
    parser.add_argument("--part", required=True, help="which part of the site this is")
    parser.add_argument("--round", dest="round_id", required=True, help="the round it came from")
    parser.add_argument("--contract", type=Path, default=Path("direction/direction.toml"))
    parser.add_argument("--fragments-dir", type=Path, default=DEFAULT_FRAGMENTS_DIR)
    args = parser.parse_args(argv)

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
