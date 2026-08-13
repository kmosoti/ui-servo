"""Generate the kmosoti/kmosoti profile README from ui-servo's own data.

The profile README went stale the way the deployment runbook went stale: the
facts were prose, so nothing could notice when they stopped being true. It
pointed at kmosoti.github.io months after the site moved, and listed projects
that had been superseded. `deploy/gen-reference.py` already solved this shape
of problem here -- generate the factual parts, check them in CI -- and this is
the same trick pointed at a different repo.

What it does NOT do is generate prose. The voice in that README is the owner's
and regenerating it from data would flatten it into a changelog. Only regions
between markers are replaced:

    <!-- ui-servo:begin:projects -->   ... generated ...   <!-- ui-servo:end:projects -->

Everything outside the markers survives untouched, so the narrative sections
can be edited freely in the profile repo without this tool ever fighting them.

    python tools/profile_readme.py --out build/profile      # write
    python tools/profile_readme.py --check --against <dir>  # CI drift gate

The project list is read from the `RESUME` object in site/assets/portfolio.js,
which is the same data the site's "download resume.json" button serves -- so
the profile cannot disagree with the site about what is pre-alpha. That exact
disagreement happened three times in one evening (card badge, print resume,
and this object) before it was worth automating.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_JS = REPO_ROOT / "site" / "assets" / "portfolio.js"

SITE_URL = "https://kennedy.mosoti.dev"
GITHUB_USER = "kmosoti"

# The site's palette, so the card reads as the same system as the thing it
# announces. Values are portfolio.css's own.
EMBER = "#ff7a45"
INK = "#08090b"
CARD = "#101215"
BORDER = "#24282e"
TEXT = "#ece7dd"
DIM = "#9aa0a7"
FAINT = "#6e747b"

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

# A status that is not in this table is rendered plainly rather than guessed at.
STATUS_COLOR = {"pre-alpha": "#f2b134", "early bootstrap": FAINT, "active": FAINT}


@dataclass(frozen=True)
class Project:
    name: str
    status: str
    summary: str
    url: str | None


def read_projects(source: Path = PORTFOLIO_JS) -> list[Project]:
    """Pull the `projects` array out of the RESUME object.

    Deliberately a targeted extraction rather than a JS-to-JSON conversion of
    the whole object: RESUME contains `ITEM_TAGS.foo` identifier references
    that no JSON parser will accept, and a general converter that strips them
    is a parser I would have to trust without being able to test its failure
    modes. Matching the four fields this tool actually uses is small enough to
    verify by reading, and `--check` plus the callers' assertions catch a shape
    change loudly instead of silently emitting an empty list.
    """
    text = source.read_text(encoding="utf-8")
    start = text.find("projects: [")
    if start == -1:
        raise SystemExit(f"no `projects: [` array in {source}")
    end = text.find("\n    ],", start)
    if end == -1:
        raise SystemExit(f"unterminated `projects` array in {source}")
    block = text[start:end]

    entry = re.compile(
        r"\{\s*name:\s*'([^']+)',\s*"
        r"status:\s*'([^']+)',\s*"
        r"summary:\s*'([^']*)',\s*"
        r"url:\s*(?:'([^']+)'|null)"
    )
    projects = [Project(m[1], m[2], m[3], m[4]) for m in entry.finditer(block)]
    if not projects:
        raise SystemExit(
            f"matched no projects in {source}; the RESUME shape changed and "
            "tools/profile_readme.py needs updating"
        )
    return projects


# --------------------------------------------------------------------------- #
# the animated card
# --------------------------------------------------------------------------- #

def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(version: str, deployed: str, projects: list[Project]) -> str:
    """An animated terminal card in the site's own language.

    SMIL (`<animate>`), not CSS keyframes and not JS: GitHub serves README
    images through its camo proxy into an `<img>`, and an `<img>` renders SVG
    in an isolated context with scripting disabled. SMIL animates there; a
    `<script>` never runs, and inline `<svg>` in Markdown is stripped by
    GitHub's sanitizer altogether, so linking a file is the only route.

    Sizes are in user units against a fixed viewBox so the card scales to
    whatever column width GitHub gives it.
    """
    pre_alpha = sum(1 for p in projects if p.status == "pre-alpha")
    cmd = " whoami"
    CH = 8.4          # advance width of the 13.5px monospace face, measured
    TEXT_X = 46
    # Two different animations, and conflating them put the caret inside the
    # word: the clip rect grows from zero, while the caret's x is that width
    # offset by where the text starts.
    widths = [round((i + 1) * CH, 1) for i in range(len(cmd))]
    clip_steps = ";".join(str(w) for w in widths)
    caret_steps = ";".join(str(round(TEXT_X + w, 1)) for w in widths)

    CX, CY = 780, 122
    disk = []
    # The accretion disk from the BlackCell page, reduced to what SMIL can do:
    # motes on tilted circular paths, each with its own period so the ring
    # shears instead of turning like a wheel.
    #
    # The path is in absolute user coordinates, not centred on the origin:
    # animateMotion has no `transform` attribute, so an origin-centred path
    # leaves the mote orbiting (0,0), and the group's rotate() then flings it
    # to roughly (0,247) -- a stray dot in the corner, with the disk itself
    # left as static rings.
    for i, (rx, ry, dur, op) in enumerate(
        [(58, 17, 7.5, 0.85), (48, 14, 5.5, 0.7), (68, 20, 10.0, 0.5), (38, 11, 4.0, 0.6)]
    ):
        orbit = (
            f"M {CX + rx},{CY} A {rx},{ry} 0 1,1 {CX - rx},{CY} "
            f"A {rx},{ry} 0 1,1 {CX + rx},{CY}"
        )
        disk.append(
            f'<g transform="rotate(-18 {CX} {CY})" opacity="{op}">'
            f'<ellipse cx="{CX}" cy="{CY}" rx="{rx}" ry="{ry}" fill="none" '
            f'stroke="{EMBER}" stroke-width="0.6" opacity="0.28"/>'
            f'<circle r="{1.9 - i * 0.25:.2f}" fill="{EMBER}">'
            f'<animateMotion dur="{dur}s" repeatCount="indefinite" path="{orbit}"/>'
            f"</circle></g>"
        )
    disk_svg = "".join(disk)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 880 260" width="880" height="260" role="img" aria-label="ui-servo console: Kennedy Mosoti, observability platform engineer">
  <title>kennedy@observability — {_esc(version)}</title>
  <defs>
    <clipPath id="type"><rect x="46" y="86" width="0" height="22">
      <animate attributeName="width" values="0;{clip_steps}" dur="1.1s" begin="0.35s" fill="freeze" calcMode="discrete"/>
    </rect></clipPath>
    <radialGradient id="core" cx="50%" cy="50%">
      <stop offset="55%" stop-color="{INK}"/>
      <stop offset="100%" stop-color="{EMBER}" stop-opacity="0.30"/>
    </radialGradient>
    <clipPath id="card"><rect x="1" y="1" width="878" height="258" rx="10"/></clipPath>
  </defs>

  <rect width="880" height="260" rx="10" fill="{CARD}" stroke="{BORDER}"/>
  <g clip-path="url(#card)">
    {disk_svg}
    <circle cx="780" cy="122" r="26" fill="url(#core)"/>
    <circle cx="780" cy="122" r="26" fill="none" stroke="{EMBER}" stroke-width="1.1" opacity="0.55"/>
  </g>

  <!-- title bar -->
  <rect x="1" y="1" width="878" height="38" rx="10" fill="#0c0e11"/>
  <rect x="1" y="30" width="878" height="9" fill="#0c0e11"/>
  <line x1="1" y1="39" x2="879" y2="39" stroke="#1b1f24"/>
  <circle cx="22" cy="20" r="4" fill="{EMBER}">
    <animate attributeName="opacity" values="1;0.35;1" dur="3.2s" repeatCount="indefinite"/>
  </circle>
  <text x="38" y="24" font-family="{MONO}" font-size="11.5" fill="{FAINT}">kennedy@observability — zsh</text>
  <text x="859" y="24" font-family="{MONO}" font-size="11.5" fill="{FAINT}" text-anchor="end">{_esc(version)}</text>

  <!-- typed command -->
  <text x="30" y="103" font-family="{MONO}" font-size="13.5" fill="{EMBER}">$</text>
  <g clip-path="url(#type)">
    <text x="46" y="103" font-family="{MONO}" font-size="13.5" fill="{DIM}">{_esc(cmd)}</text>
  </g>
  <rect x="{TEXT_X}" y="90" width="7.5" height="15" fill="{EMBER}">
    <animate attributeName="x" values="{TEXT_X};{caret_steps}" dur="1.1s" begin="0.35s" fill="freeze" calcMode="discrete"/>
    <animate attributeName="opacity" values="1;1;0;0" dur="1s" begin="1.45s" repeatCount="indefinite"/>
  </rect>

  <text x="30" y="152" font-family="{MONO}" font-size="30" font-weight="700" fill="{TEXT}" letter-spacing="-0.5">Kennedy Mosoti</text>
  <text x="30" y="176" font-family="{MONO}" font-size="12" fill="{EMBER}" letter-spacing="1.1">OBSERVABILITY PLATFORM ENGINEER</text>
  <text x="30" y="196" font-family="{MONO}" font-size="12" fill="{FAINT}" letter-spacing="1.1">BRANCHING INTO AGENTIC ENGINEERING</text>

  <line x1="30" y1="216" x2="850" y2="216" stroke="{BORDER}"/>
  <text x="30" y="238" font-family="{MONO}" font-size="11" fill="{FAINT}"><tspan fill="{DIM}">{len(projects)}</tspan> projects · <tspan fill="{DIM}">{pre_alpha}</tspan> pre-alpha · deployed <tspan fill="{DIM}">{_esc(deployed)}</tspan> · <tspan fill="{EMBER}">{_esc(SITE_URL.replace("https://", ""))}</tspan></text>
</svg>
"""


# --------------------------------------------------------------------------- #
# the generated README regions
# --------------------------------------------------------------------------- #

def block_header(svg_path: str) -> str:
    return (
        f'<img src="./{svg_path}" alt="ui-servo console card: Kennedy Mosoti, '
        f'observability platform engineer" width="100%">'
    )


def block_projects(projects: list[Project]) -> str:
    rows = ["| Project | Status | What it is |", "| --- | --- | --- |"]
    for p in projects:
        name = f"[{p.name}]({p.url})" if p.url else p.name
        rows.append(f"| **{name}** | `{p.status}` | {p.summary} |")
    rows.append("")
    rows.append(
        "_Statuses are read from the site's own resume data, not retyped here. "
        "Nothing is past pre-alpha; when that changes this table changes with it._"
    )
    return "\n".join(rows)


def block_site(version: str, deployed: str, sha: str) -> str:
    short = sha[:7] if sha else "unknown"
    return "\n".join(
        [
            f"**[{SITE_URL.replace('https://', '')}]({SITE_URL})** — "
            "a Rust/axum site exported to static files, served by Caddy on a "
            "512MB droplet. Offline-capable, precompressed, and deployed by CI.",
            "",
            "| | |",
            "| --- | --- |",
            f"| Live release | [`{short}`](https://github.com/{GITHUB_USER}/ui-servo/commit/{sha}) |",
            f"| Service worker | `{version}` |",
            f"| Deployed | {deployed} |",
        ]
    )


def render_blocks(version: str, deployed: str, sha: str, projects: list[Project],
                  svg_path: str) -> dict[str, str]:
    return {
        "header": block_header(svg_path),
        "site": block_site(version, deployed, sha),
        "projects": block_projects(projects),
    }


MARKER = re.compile(
    r"(?P<open><!--\s*ui-servo:begin:(?P<name>[\w-]+)\s*-->)"
    r".*?"
    r"(?P<close><!--\s*ui-servo:end:(?P=name)\s*-->)",
    re.DOTALL,
)


def apply_blocks(readme: str, blocks: dict[str, str]) -> tuple[str, set[str]]:
    """Replace only the marked regions. Returns the text and the names seen.

    An unknown marker is left exactly as-is rather than emptied: the profile
    repo is allowed to carry regions this tool does not know about, and
    deleting someone else's content because we did not recognise the name is
    the worst possible failure for a bot with push access.
    """
    seen: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        name = m["name"]
        seen.add(name)
        if name not in blocks:
            return m[0]
        return f"{m['open']}\n{blocks[name]}\n{m['close']}"

    return MARKER.sub(sub, readme), seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--readme", type=Path, help="existing profile README to update")
    ap.add_argument("--out", type=Path, help="directory to write README.md and the SVG into")
    ap.add_argument("--svg-path", default="assets/ui-servo-console.svg")
    ap.add_argument("--version", default="dev", help="service-worker version being shipped")
    ap.add_argument("--sha", default="", help="commit sha of the live release")
    ap.add_argument("--deployed", default="", help="ISO date of the deploy (defaults to today, UTC)")
    ap.add_argument("--check", action="store_true", help="exit 1 if --out is not current")
    args = ap.parse_args(argv)

    deployed = args.deployed or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    projects = read_projects()
    svg = render_svg(args.version, deployed, projects)
    blocks = render_blocks(args.version, deployed, args.sha, projects, args.svg_path)

    if not args.out:
        ap.error("--out is required")
    readme_out = args.out / "README.md"
    svg_out = args.out / args.svg_path

    source = args.readme or readme_out
    if not source.is_file():
        ap.error(f"no README to update at {source}; pass --readme")
    updated, seen = apply_blocks(source.read_text(encoding="utf-8"), blocks)

    missing = set(blocks) - seen
    if missing:
        raise SystemExit(
            "the profile README is missing marker pairs for: "
            + ", ".join(sorted(missing))
            + "\nAdd <!-- ui-servo:begin:NAME --><!-- ui-servo:end:NAME --> where "
            "each generated region belongs."
        )

    if args.check:
        stale = [
            str(p)
            for p, want in ((readme_out, updated), (svg_out, svg))
            if not p.is_file() or p.read_text(encoding="utf-8") != want
        ]
        if stale:
            print("stale: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"{readme_out} and {svg_out} are current")
        return 0

    svg_out.parent.mkdir(parents=True, exist_ok=True)
    readme_out.parent.mkdir(parents=True, exist_ok=True)
    svg_out.write_text(svg, encoding="utf-8")
    readme_out.write_text(updated, encoding="utf-8")
    print(f"wrote {readme_out} ({len(updated)}b) and {svg_out} ({len(svg)}b)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
