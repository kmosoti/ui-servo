"""The slow loop, exploration half: aim the briefs, keep the elites, ask the human.

:mod:`~ui_servo.control.critique` argues about candidates that already exist. This
module decides which candidates should exist in the first place, remembers the best
of each *kind*, and puts the result in front of a person.

Four functions, one per station of the exploration cycle:

* :func:`seed_cells` -- before a round, pick K cells of the behaviour space to aim
  briefs at, as far apart as the grid allows. Asking K builders the same question
  gets K samples of one distribution; asking them for a dense-quiet hero, an airy
  expressive one and three points in between is what actually buys variety. It is
  the requisite-variety argument applied to the actuator rather than to the panel.
* :func:`admit` -- after a round, place a survivor in the archive. Gate-failed
  variants raise: the fast loop is upstream of taste, and a candidate that never
  met the bar has nothing to contribute to a discussion about which bar to move to.
* :func:`frontier_report` -- render the archive as one self-contained HTML file: a
  card per occupied cell with its screenshot, its coordinates, what the gates said,
  what the panel found, and how bland it was. Self-contained because it has to
  survive being emailed, opened from a checkout, or read six months later with the
  screenshots long since deleted.
* :func:`record_pick` -- write the human's choice into the exemplar store.

**The exemplar store has exactly one writer, and it is this function.** Under
Conant-Ashby the regulator's model of taste is the direction contract plus the
exemplars, so an exemplar written by anything other than a person is the loop
grading its own taste and then learning from the grade. Every other station here
reads, measures and reports; only :func:`record_pick` writes, it is only ever
called from a human-initiated path, and it takes an :class:`Elite` -- something
that already passed the gates and won its cell -- rather than a bare variant.

Standard library plus :mod:`ui_servo.domain` and :mod:`ui_servo.ports`. This module
does touch the filesystem, unlike :mod:`~ui_servo.control.critique`: a report that
is not a file is not a report, and screenshots are on disk because that is where
the sensor put them. It reads and it writes what it is pointed at, and resolves
nothing it was not given.
"""

from base64 import b64encode
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Final

from ui_servo.domain.contract import DirectionContract
from ui_servo.domain.variant import (
    AXIS_NAMES,
    DEFAULT_RESOLUTION,
    STILL,
    CellCoord,
    Coverage,
    Elite,
    EliteArchive,
    GateFailure,
    MotionEvidence,
    StyleAxes,
    Variant,
    fitness_of,
)
from ui_servo.control.regulator import RegulatorReport
from ui_servo.ports.store import Exemplar, ExemplarStorePort, FileName

FRAGMENT_FILENAME: Final[FileName] = "fragment.html"
"""What the picked variant's markup is saved as inside an exemplar."""

SCREENSHOT_STEM: Final[str] = "screenshot"
"""Stem of the picked variant's image inside an exemplar; the suffix is kept."""

DEFAULT_REPORT_TITLE: Final[str] = "ui-servo frontier"

_IMAGE_MEDIA_TYPES: Final[Mapping[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

_SAFE_NAME_EXTRA: Final[frozenset[str]] = frozenset("._-")
"""Characters an exemplar name may contain besides letters and digits.

Matches the store adapter's own rule (``[A-Za-z0-9][A-Za-z0-9._-]*``). Enforced
here as well so a variant id full of slashes fails at the call site with a message
about the variant, rather than deep inside a store with a message about a path.
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Aiming the briefs                                                            #
# --------------------------------------------------------------------------- #


def _squared(a: CellCoord, b: CellCoord) -> int:
    return sum((first - second) ** 2 for first, second in zip(a, b, strict=True))


def _spread_profile(chosen: Sequence[CellCoord]) -> tuple[int, ...]:
    """Every pairwise squared distance in the set, smallest first.

    Compared lexicographically, this is the spread objective: the set whose *closest
    pair* is furthest apart wins, and if two sets tie on that, the one whose second
    closest pair is furthest apart wins, and so on. Comparing sorted profiles rather
    than a sum is what stops one enormous distance from paying for two cells sitting
    on top of each other -- and two cells on top of each other is exactly the
    degenerate brief set this function exists to avoid.
    """
    return tuple(
        sorted(
            _squared(chosen[i], chosen[j])
            for i in range(len(chosen))
            for j in range(i + 1, len(chosen))
        )
    )


def _farthest_first(pool: Sequence[CellCoord], k: int, centre: tuple[float, ...]) -> list[CellCoord]:
    """Greedy seed: furthest from the centre, then furthest from what is chosen."""
    chosen = [
        min(
            pool,
            key=lambda cell: (
                -sum((axis - middle) ** 2 for axis, middle in zip(cell, centre, strict=True)),
                cell,
            ),
        )
    ]
    remaining = [cell for cell in pool if cell != chosen[0]]
    while len(chosen) < k and remaining:
        best = min(
            remaining,
            key=lambda cell: (-min(_squared(cell, taken) for taken in chosen), cell),
        )
        chosen.append(best)
        remaining.remove(best)
    return chosen


def _improved(chosen: list[CellCoord], pool: Sequence[CellCoord]) -> list[CellCoord]:
    """Steepest-ascent swaps until no single substitution spreads the set further.

    Farthest-first alone is a 2-approximation and its failure mode is systematic:
    having taken one corner it takes the opposite corner, and every later pick is
    squeezed between them. On a 3x3x3 grid that costs real spread, so the greedy
    result is polished by exchanging one chosen cell at a time for an unchosen one
    whenever :func:`_spread_profile` improves.

    Deterministic: the best improving swap is taken each round, ties broken by the
    resulting sorted cell list, and the loop ends when nothing improves. Bounded by
    construction -- the profile strictly increases every round and there are finitely
    many subsets.
    """
    current = sorted(chosen)
    best_profile = _spread_profile(current)
    while True:
        candidates: list[tuple[tuple[int, ...], list[CellCoord]]] = []
        occupied = set(current)
        for index in range(len(current)):
            for replacement in pool:
                if replacement in occupied:
                    continue
                swapped = sorted(current[:index] + current[index + 1 :] + [replacement])
                profile = _spread_profile(swapped)
                if profile > best_profile:
                    candidates.append((profile, swapped))
        if not candidates:
            return current
        best_profile, current = max(candidates, key=lambda item: (item[0], item[1]))


def seed_cells(
    k: int,
    *,
    resolution: int = DEFAULT_RESOLUTION,
    avoid: Collection[CellCoord] = (),
) -> list[CellCoord]:
    """*k* distinct target cells, spread as far apart as the grid allows.

    Farthest-first traversal (:func:`_farthest_first`) followed by swap refinement
    (:func:`_improved`), maximising the sorted vector of pairwise distances. The
    result is deterministic -- same *k*, same *resolution*, same list, on any machine
    -- because a round nobody can replay is a round nobody can compare against.

    Returned in coordinate order. The order carries no priority: these are K
    simultaneous briefs, not a ranked list, and a caller that truncated the list
    would be choosing which regions of the space to abandon by accident.

    ``avoid`` is normally ``archive.occupied_cells()``: a brief aimed at a cell that
    already has an elite is a brief spent re-deriving something we have. Cells in it
    are skipped entirely while there are enough others; when *k* exceeds the number
    of free cells the whole grid is used instead, because "aim somewhere" beats
    "return fewer briefs than were asked for" -- a caller that wanted K variants and
    got three would have silently narrowed its own round.
    """
    if resolution < 1:
        raise ValueError(f"a grid needs at least one bin per axis, got {resolution}")
    grid = [
        (first, second, third)
        for first in range(resolution)
        for second in range(resolution)
        for third in range(resolution)
    ]
    if k < 1:
        raise ValueError(f"a round needs at least one brief, got {k}")
    if k > len(grid):
        raise ValueError(
            f"cannot seed {k} distinct cells: the {resolution}^{len(AXIS_NAMES)} grid "
            f"has only {len(grid)}"
        )

    excluded = set(avoid)
    free = [cell for cell in grid if cell not in excluded]
    pool = free if len(free) >= k else grid
    if k == len(pool):
        return sorted(pool)

    centre = tuple((resolution - 1) / 2 for _ in AXIS_NAMES)
    return _improved(_farthest_first(pool, k, centre), pool)


# --------------------------------------------------------------------------- #
# Admitting survivors                                                          #
# --------------------------------------------------------------------------- #


def gate_summary(report: RegulatorReport) -> str:
    """One line a human can read: how many gates were met, and which were not.

    Written from the report rather than from :attr:`RegulatorReport.passed` alone so
    the card in the frontier report says *what* was checked. "7/7 gates passed" and
    "passed" are the same verdict and very different evidence.
    """
    total = len(report.gates)
    failed = report.failed_gate_names
    if not failed:
        return f"{total}/{total} gates passed"
    return (
        f"{total - len(failed)}/{total} gates passed; "
        f"failed: {', '.join(failed)}"
    )


def admit(
    regulator_report: RegulatorReport,
    panel_rank: int,
    *,
    archive: EliteArchive,
    variant: Variant,
    panel_size: int,
    motion: MotionEvidence = STILL,
    contract: DirectionContract | None = None,
    findings: Sequence[str] = (),
    screenshot: str | Path = "",
) -> Elite | None:
    """Place one ranked survivor in *archive*. Returns the elite, or ``None``.

    ``None`` means the cell's incumbent was fitter and held. A gate-failed report
    does *not* mean ``None``: it raises :class:`~ui_servo.domain.variant.GateFailure`,
    because a gate failure arriving here is a pipeline bug -- the fast loop is meant
    to have rejected it before any judge was asked -- and a quiet return would let
    broken UI vanish into the same silence as an ordinary loss.

    ``panel_rank`` is 1-based within ``panel_size``, which is the field the variant
    actually competed in. Normalising by the field size is what lets fitness mean the
    same thing in a cell filled from a two-candidate round and one filled from six.

    The distinctiveness bonus is measured against what the archive already holds,
    before this variant enters it. The blandness score rides along on the elite as
    taste for a human to read and is deliberately not summed into fitness -- one idea,
    measured once.
    """
    if not regulator_report.passed:
        raise GateFailure(
            f"variant {regulator_report.variant_id!r} failed "
            f"{list(regulator_report.failed_gate_names)}; gate-failed candidates never "
            "reach the archive, and should never have reached a judge either"
        )
    vector = regulator_report.style_vector
    if vector is None:
        raise ValueError(
            f"variant {regulator_report.variant_id!r} has no style vector; the archive "
            "locates candidates by what they rendered, so a report taken without a "
            "style sample cannot be placed"
        )
    axes = StyleAxes.of(vector, motion=motion, contract=contract)
    return archive.place(
        variant,
        axes,
        fitness_of(
            panel_rank=panel_rank,
            panel_size=panel_size,
            distinctiveness=archive.distinctiveness(vector),
        ),
        passed=True,
        style_vector=vector,
        blandness_score=regulator_report.blandness_score,
        panel_rank=panel_rank,
        gate_summary=gate_summary(regulator_report),
        findings=tuple(findings),
        screenshot=str(screenshot),
    )


# --------------------------------------------------------------------------- #
# The frontier report                                                          #
# --------------------------------------------------------------------------- #

_STYLE: Final[str] = """
:root { color-scheme: dark light; --bg:#141210; --card:#1c1917; --line:#332e2a;
        --ink:#f0ece6; --dim:#a8a09a; --accent:#f0a259; --accent-2:#5fb6d4; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem clamp(1rem,4vw,3rem); background:var(--bg); color:var(--ink);
       font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
h1 { font-size:1.75rem; margin:0 0 .25rem; letter-spacing:-.01em; }
.meta { color:var(--dim); margin:0 0 1.5rem; font-size:.9rem; }
.coverage { border:1px solid var(--line); border-radius:6px; padding:1rem 1.25rem;
            margin-bottom:2rem; background:var(--card); }
.coverage dl { display:grid; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));
               gap:.75rem 1.5rem; margin:.75rem 0 0; }
.coverage dt { color:var(--dim); font-size:.75rem; text-transform:uppercase;
               letter-spacing:.06em; }
.coverage dd { margin:.15rem 0 0; font-size:1.1rem; font-variant-numeric:tabular-nums; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(20rem,1fr)); gap:1.25rem; }
.card { border:1px solid var(--line); border-radius:6px; background:var(--card);
        overflow:hidden; display:flex; flex-direction:column; }
.card header { padding:.75rem 1rem; border-bottom:1px solid var(--line);
               display:flex; justify-content:space-between; gap:1rem; align-items:baseline; }
.cell { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--accent); }
.fitness { font-variant-numeric:tabular-nums; color:var(--accent-2); }
.shot { display:block; width:100%; height:auto; background:#0b0a09; }
.no-shot { padding:2.5rem 1rem; text-align:center; color:var(--dim); font-size:.85rem;
           background:#0b0a09; }
.body { padding:.85rem 1rem 1rem; display:flex; flex-direction:column; gap:.6rem; }
.axes { display:grid; gap:.35rem; margin:0; }
.axis { display:grid; grid-template-columns:9rem 1fr 3rem; gap:.5rem; align-items:center;
        font-size:.8rem; color:var(--dim); }
.bar { height:.45rem; border-radius:3px; background:#2a2522; overflow:hidden; }
.bar span { display:block; height:100%; background:var(--accent); }
.axis .num { text-align:right; font-variant-numeric:tabular-nums; color:var(--ink); }
.facts { margin:0; font-size:.85rem; color:var(--dim); }
.facts b { color:var(--ink); font-weight:600; }
.findings { margin:.1rem 0 0; padding-left:1.1rem; font-size:.85rem; }
.findings li { margin:.15rem 0; }
.empty { margin-top:2.5rem; }
.empty ul { list-style:none; padding:0; display:flex; flex-wrap:wrap; gap:.4rem; }
.empty li { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem;
            color:var(--dim); border:1px dashed var(--line); border-radius:4px;
            padding:.15rem .5rem; }
"""


def _data_uri(path: Path) -> str | None:
    """*path* inlined as a ``data:`` URI, or ``None`` if it cannot be read.

    Inlined rather than linked because the report is meant to outlive the run
    directory it was written in. A screenshot that has since been swept is a fact
    worth showing, so an unreadable file becomes a visible placeholder rather than a
    broken image or an exception -- the report's job is to get a human to a
    decision, and one missing image must not cost the other twenty-six cards.
    """
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    media = _IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{media};base64,{b64encode(blob).decode('ascii')}"


def _axis_rows(elite: Elite) -> str:
    rows = []
    for name, value in elite.axes.as_mapping().items():
        percent = f"{value * 100:.1f}"
        rows.append(
            f'<div class="axis"><span>{escape(name)}</span>'
            f'<span class="bar"><span style="width:{percent}%"></span></span>'
            f'<span class="num">{value:.2f}</span></div>'
        )
    return "".join(rows)


def _screenshot_block(elite: Elite) -> str:
    if not elite.screenshot:
        return '<p class="no-shot">no screenshot was captured for this variant</p>'
    uri = _data_uri(Path(elite.screenshot))
    if uri is None:
        return (
            '<p class="no-shot">screenshot unavailable: '
            f"{escape(Path(elite.screenshot).name)}</p>"
        )
    return (
        f'<img class="shot" alt="rendered {escape(elite.variant.part)}, cell '
        f'{escape(str(elite.cell))}" src="{uri}">'
    )


def _card(elite: Elite) -> str:
    bland = (
        "not measured"
        if elite.blandness_score is None
        else f"{elite.blandness_score:.3f} (low = bland)"
    )
    rank = "unranked" if elite.panel_rank is None else str(elite.panel_rank)
    aimed = (
        "no target cell"
        if elite.aimed_at is None
        else f"aimed at {elite.aimed_at}" + ("" if elite.on_target else " — landed elsewhere")
    )
    findings = (
        '<ul class="findings">'
        + "".join(f"<li>{escape(finding)}</li>" for finding in elite.findings)
        + "</ul>"
        if elite.findings
        else '<p class="facts">the panel recorded no findings against this variant.</p>'
    )
    return (
        '<article class="card">'
        f'<header><span class="cell">{escape(str(elite.cell))}</span>'
        f'<span class="fitness">fitness {elite.fitness:.3f}</span></header>'
        f"{_screenshot_block(elite)}"
        '<div class="body">'
        f'<p class="facts"><b>{escape(elite.variant.part)}</b> · '
        f"{escape(elite.variant.variant_id)} · {escape(aimed)}</p>"
        f'<div class="axes">{_axis_rows(elite)}</div>'
        f'<p class="facts">gates: <b>{escape(elite.gate_summary or "not recorded")}</b></p>'
        f'<p class="facts">panel rank <b>{escape(rank)}</b> · blandness '
        f"<b>{escape(bland)}</b></p>"
        f"{findings}"
        "</div></article>"
    )


def _coverage_block(coverage: Coverage) -> str:
    best = "n/a" if coverage.best_fitness is None else f"{coverage.best_fitness:.3f}"
    mean = "n/a" if coverage.mean_fitness is None else f"{coverage.mean_fitness:.3f}"
    return (
        '<section class="coverage"><strong>Coverage</strong>'
        f"<p class=\"facts\">{escape(coverage.summary())}</p><dl>"
        f"<div><dt>cells occupied</dt><dd>{coverage.occupied} / {coverage.total}</dd></div>"
        f"<div><dt>coverage</dt><dd>{coverage.ratio:.0%}</dd></div>"
        f"<div><dt>QD score</dt><dd>{coverage.qd_score:.3f}</dd></div>"
        f"<div><dt>best fitness</dt><dd>{best}</dd></div>"
        f"<div><dt>mean fitness</dt><dd>{mean}</dd></div>"
        "</dl></section>"
    )


def _empty_block(coverage: Coverage) -> str:
    if not coverage.empty_cells:
        return (
            '<section class="empty"><h2>Unreached cells</h2>'
            "<p class=\"facts\">none — every cell of the grid has an elite.</p></section>"
        )
    listed = "".join(f"<li>{escape(str(cell))}</li>" for cell in coverage.empty_cells)
    return (
        '<section class="empty"><h2>Unreached cells</h2>'
        f'<p class="facts">{len(coverage.empty_cells)} corners of the behaviour space '
        f"nothing has landed in yet — in axis order ({escape(', '.join(AXIS_NAMES))}). "
        "These are the briefs still worth writing.</p>"
        f"<ul>{listed}</ul></section>"
    )


def frontier_report(
    archive: EliteArchive,
    out_html: str | Path,
    *,
    title: str = DEFAULT_REPORT_TITLE,
    now: Callable[[], str] = _utc_now,
) -> Path:
    """Render *archive* to a single self-contained HTML file and return its path.

    One card per occupied cell -- screenshot, coordinates, gate summary, panel
    findings, blandness -- plus the coverage stats and the list of cells nothing has
    reached. That last list is the point of the whole exercise: a leaderboard tells
    you which candidate won, and only a grid tells you which questions were never
    asked.

    Self-contained: images are inlined as ``data:`` URIs and the stylesheet is
    embedded, so the file works from a checkout, an email attachment or a directory
    whose screenshots were swept months ago. Nothing is fetched at view time, which
    also means the report cannot quietly change after the human read it.

    Everything a critic wrote is escaped on the way in. Findings are model output
    and model output is untrusted text; a finding containing ``<script>`` is a
    finding about a markup bug, not a licence to run it in the reviewer's browser.
    """
    destination = Path(out_html)
    coverage = archive.coverage()
    elites = archive.frontier()
    cards = (
        "".join(_card(elite) for elite in elites)
        if elites
        else '<p class="facts">the archive is empty: no variant has passed its gates '
        "and been ranked yet.</p>"
    )
    document = (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>"
        f"<h1>{escape(title)}</h1>"
        f'<p class="meta">{len(elites)} elite(s) across a {archive.resolution}'
        f"<sup>{len(AXIS_NAMES)}</sup> behaviour grid · generated {escape(now())} · "
        "pick one; the pick is the only thing that writes an exemplar.</p>"
        f"{_coverage_block(coverage)}"
        f'<section class="grid">{cards}</section>'
        f"{_empty_block(coverage)}"
        "</body></html>"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


# --------------------------------------------------------------------------- #
# The human's pick                                                             #
# --------------------------------------------------------------------------- #


def exemplar_name(elite: Elite) -> str:
    """The default store key for a picked elite: ``<part>-<variant_id>``."""
    raw = f"{elite.variant.part}-{elite.variant.variant_id}"
    cleaned = "".join(
        char if char.isalnum() or char in _SAFE_NAME_EXTRA else "-" for char in raw
    ).strip("-")
    if not cleaned or not cleaned[0].isalnum():
        raise ValueError(
            f"cannot derive an exemplar name from part {elite.variant.part!r} and "
            f"variant {elite.variant.variant_id!r}; pass name= explicitly"
        )
    return cleaned


def pick_meta(elite: Elite, *, note: str = "", picked_by: str = "", picked_at: str) -> dict[str, Any]:
    """The provenance recorded beside a picked exemplar.

    Deliberately verbose. An exemplar is a point of the regulator's taste model, and
    a taste model you cannot interrogate later -- what was it beating, where did it
    sit, what did the panel dislike about it anyway -- is a preference nobody can
    argue with. ``picked_by`` and ``note`` are the human's own words, and they are
    the only fields here no measurement produced.
    """
    return {
        "variant_id": elite.variant.variant_id,
        "part": elite.variant.part,
        "builder_family": elite.variant.builder_family,
        "cell": list(elite.cell),
        "cell_hint": None if elite.aimed_at is None else list(elite.aimed_at),
        "axes": elite.axes.as_mapping(),
        "fitness": elite.fitness,
        "panel_rank": elite.panel_rank,
        "blandness_score": elite.blandness_score,
        "gate_summary": elite.gate_summary,
        "findings": list(elite.findings),
        "picked_at": picked_at,
        "picked_by": picked_by,
        "note": note,
    }


def record_pick(
    elite: Elite,
    exemplar_store: ExemplarStorePort,
    *,
    name: str | None = None,
    note: str = "",
    picked_by: str = "",
    now: Callable[[], str] = _utc_now,
) -> Exemplar:
    """Save the human's chosen elite as an exemplar. **The only writer of taste.**

    Nothing else in ``ui_servo`` calls
    :meth:`~ui_servo.ports.store.ExemplarStorePort.save_exemplar`, and nothing else
    should. The exemplars plus the direction contract *are* the regulator's model of
    taste (Conant-Ashby), so a loop that could write them would be closing the
    circuit on its own preferences: it would rank candidates, learn from the winner,
    and drift wherever its own bias pointed with nobody in the path. Keeping the one
    write behind a function that takes an already-gated, already-ranked
    :class:`~ui_servo.domain.variant.Elite` and is only ever invoked from a
    human-initiated command is how "the human is the brake" stays structural rather
    than aspirational.

    Saves the fragment markup, the screenshot if one was captured and can be read,
    and the provenance from :func:`pick_meta`. A screenshot the elite names but that
    cannot be read raises: an exemplar is evidence, and half of it is worse than a
    clear failure at pick time when the file is still recoverable.
    """
    files: dict[FileName, bytes] = {
        FRAGMENT_FILENAME: elite.variant.html.encode("utf-8")
    }
    if elite.screenshot:
        source = Path(elite.screenshot)
        try:
            files[f"{SCREENSHOT_STEM}{source.suffix or '.png'}"] = source.read_bytes()
        except OSError as error:
            raise ValueError(
                f"elite {elite.variant.variant_id!r} names a screenshot that cannot be "
                f"read ({source}): {error}"
            ) from error
    return exemplar_store.save_exemplar(
        name or exemplar_name(elite),
        files,
        pick_meta(elite, note=note, picked_by=picked_by, picked_at=now()),
    )


def picks_from(archive: EliteArchive, cells: Iterable[CellCoord]) -> list[Elite]:
    """The elites a human named by cell, in the order they named them.

    A convenience for the command that reads a pick off the frontier report, and a
    guard: an unknown cell raises rather than being skipped, so a typo in a pick
    cannot silently record a different exemplar than the one that was chosen.
    """
    picked: list[Elite] = []
    for cell in cells:
        elite = archive.get(cell)
        if elite is None:
            raise KeyError(f"no elite occupies cell {cell}; occupied: {archive.occupied_cells()}")
        picked.append(elite)
    return picked
