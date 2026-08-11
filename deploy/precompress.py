"""Write .br siblings next to every compressible file in an export.

Caddy's standard build can *serve* precompressed brotli but cannot *produce*
it -- `http.precompressed.br` is registered, `http.encoders.br` is not. Doing
it at deploy time is the better trade anyway: quality 11 instead of the
on-the-fly default, and zero CPU per request forever after, which is the
difference between 128 and several thousand requests per second on one vCPU.

    uv run --with brotli python precompress.py <dist>
"""

from __future__ import annotations

import sys
from pathlib import Path

import brotli

# woff2 is already a compressed container; recompressing it wastes CPU and
# usually grows the file. Everything else here is text.
SUFFIXES = {".html", ".css", ".js", ".json", ".svg", ".webmanifest", ".xml", ".txt"}
MIN_BYTES = 256


def main() -> int:
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    saved = total = 0
    made = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        raw = path.read_bytes()
        if len(raw) < MIN_BYTES:
            continue
        packed = brotli.compress(raw, quality=11)
        if len(packed) >= len(raw):
            continue
        path.with_suffix(path.suffix + ".br").write_bytes(packed)
        total += len(raw)
        saved += len(raw) - len(packed)
        made += 1
        print(f"  {path.relative_to(root)!s:<52} {len(raw):>7} -> {len(packed):>7}")

    pct = (saved / total * 100) if total else 0
    print(f"\n  {made} files precompressed, {total} -> {total - saved} bytes ({pct:.1f}% saved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
