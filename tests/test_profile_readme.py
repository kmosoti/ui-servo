"""Tests for the profile-README generator.

The interesting risk is not "does it render" -- it is that this tool has push
access to another repository. Two failure modes matter more than the output
looking right:

1. It silently produces nothing (the RESUME shape changed, the regex stops
   matching, and an empty project table gets pushed over a good one).
2. It destroys hand-written prose it was never meant to touch.

Both are asserted here directly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import profile_readme as pr  # noqa: E402

SEED = Path(__file__).resolve().parents[1] / "tools" / "profile" / "README.seed.md"


# --------------------------------------------------------------------------- #
# reading the site's own data
# --------------------------------------------------------------------------- #

def test_projects_come_from_the_sites_resume_object() -> None:
    projects = pr.read_projects()
    assert len(projects) == 6
    names = [p.name for p in projects]
    assert "BlackCell" in names and "splunk-dashboard-studio" in names
    assert all(p.summary for p in projects), "a project lost its summary"


def test_no_project_claims_to_be_past_pre_alpha() -> None:
    """The owner's standing statement, asserted rather than remembered.

    It was contradicted in three separate places on one evening -- the card
    badge, the print resume, and the RESUME object this reads -- so it is
    cheaper to fail a test than to notice it on a live profile.
    """
    overstated = {"shipped", "beta", "stable", "ga", "1.0", "alpha"}
    bad = [p for p in pr.read_projects() if p.status.lower() in overstated]
    assert not bad, f"these claim more than pre-alpha: {[(p.name, p.status) for p in bad]}"


def test_a_changed_resume_shape_fails_loudly(tmp_path: Path) -> None:
    """An empty match must never be mistaken for "no projects"."""
    decoy = tmp_path / "portfolio.js"
    decoy.write_text("var RESUME = {\n    projects: [\n      { nom: 'x' }\n    ],\n  };\n")
    with pytest.raises(SystemExit, match="matched no projects"):
        pr.read_projects(decoy)


def test_a_missing_projects_array_fails_loudly(tmp_path: Path) -> None:
    decoy = tmp_path / "portfolio.js"
    decoy.write_text("var RESUME = { name: 'x' };\n")
    with pytest.raises(SystemExit, match="no `projects: \\[` array"):
        pr.read_projects(decoy)


# --------------------------------------------------------------------------- #
# not destroying the owner's writing
# --------------------------------------------------------------------------- #

def test_only_marked_regions_are_replaced() -> None:
    original = (
        "# Title\n\nProse the owner wrote.\n\n"
        "<!-- ui-servo:begin:projects -->\nOLD\n<!-- ui-servo:end:projects -->\n\n"
        "More prose, with a > quote and a [link](x).\n"
    )
    updated, seen = pr.apply_blocks(original, {"projects": "NEW"})
    assert "Prose the owner wrote." in updated
    assert "More prose, with a > quote and a [link](x)." in updated
    assert "OLD" not in updated and "NEW" in updated
    assert seen == {"projects"}


def test_an_unknown_marker_is_left_alone_not_emptied() -> None:
    """A bot with push access must not delete a region it fails to recognise."""
    original = "<!-- ui-servo:begin:someday -->keep me<!-- ui-servo:end:someday -->"
    updated, seen = pr.apply_blocks(original, {"projects": "NEW"})
    assert updated == original
    assert seen == {"someday"}


def test_repeated_runs_are_idempotent() -> None:
    text = "<!-- ui-servo:begin:projects -->\nx\n<!-- ui-servo:end:projects -->\n"
    once, _ = pr.apply_blocks(text, {"projects": "BODY"})
    twice, _ = pr.apply_blocks(once, {"projects": "BODY"})
    assert once == twice


def test_a_readme_missing_its_markers_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no markers here\n")
    with pytest.raises(SystemExit, match="missing marker pairs"):
        pr.main(["--readme", str(tmp_path / "README.md"), "--out", str(tmp_path)])


# --------------------------------------------------------------------------- #
# the artefacts themselves
# --------------------------------------------------------------------------- #

def test_the_seed_carries_every_marker_the_generator_emits() -> None:
    seed = SEED.read_text(encoding="utf-8")
    for name in pr.render_blocks("v", "2026-01-01", "abc", pr.read_projects(), "a.svg"):
        assert f"ui-servo:begin:{name}" in seed, f"seed has no {name} marker"
        assert f"ui-servo:end:{name}" in seed


def test_the_seed_points_at_the_live_site_not_the_retired_pages_one() -> None:
    assert "kmosoti.github.io" not in SEED.read_text(encoding="utf-8")


def test_the_card_animates_without_scripting() -> None:
    """GitHub renders README images in an <img>, where script never runs and
    inline <svg> is stripped by the sanitizer. SMIL is the only option left."""
    svg = pr.render_svg("abc123", "2026-01-01", pr.read_projects())
    assert "<script" not in svg.lower()
    assert "<animate" in svg, "nothing in the card moves"
    assert re.search(r"<animateMotion[^>]+path=\"M \d", svg), "the disk motes lost their orbit"
    assert svg.lstrip().startswith("<svg"), "must be a standalone file, not a fragment"


def test_the_card_orbits_are_centred_on_the_disk() -> None:
    """Regression: an origin-centred animateMotion path put a stray mote in the
    corner, because animateMotion has no transform attribute to offset it."""
    svg = pr.render_svg("v", "2026-01-01", pr.read_projects())
    for path in re.findall(r'<animateMotion[^>]+path="M ([0-9.]+),([0-9.]+)', svg):
        x, y = float(path[0]), float(path[1])
        assert 700 < x < 860 and y == 122, f"orbit starts at ({x},{y}), off the disk"


def test_the_generated_markdown_uses_only_what_github_renders() -> None:
    blocks = pr.render_blocks("v1", "2026-01-01", "a" * 40, pr.read_projects(), "assets/c.svg")
    body = "\n".join(blocks.values())
    assert "<svg" not in body, "inline svg is stripped by GitHub's sanitizer"
    assert "<script" not in body and "onerror=" not in body
    assert body.count("|") > 10, "the project table did not render as a table"


def test_the_release_block_links_the_real_commit() -> None:
    sha = "a5a3e71b5cf4f2769f80b4a1a80eb82d1c898c14"
    block = pr.block_site("ver", "2026-08-11", sha)
    assert f"https://github.com/kmosoti/ui-servo/commit/{sha}" in block
    assert sha[:7] in block


def test_writes_then_check_passes_and_a_stale_copy_fails(tmp_path: Path) -> None:
    args = ["--readme", str(SEED), "--out", str(tmp_path), "--version", "v9",
            "--sha", "b" * 40, "--deployed", "2026-08-11"]
    assert pr.main(args) == 0
    assert pr.main([*args, "--check"]) == 0

    (tmp_path / "README.md").write_text("tampered\n<!-- ui-servo:begin:projects -->\n"
                                        "<!-- ui-servo:end:projects -->\n")
    assert pr.main([*args, "--check"]) == 1
