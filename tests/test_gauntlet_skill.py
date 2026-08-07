"""Guards on the Gauntlet orchestration prompts.

The skill and the three agent definitions are *configuration of the control loop*, not
prose: they encode the command contract the servo CLI exposes, the candidate filename
grammar the self-preference guard parses, the blind-staging layout the panel's fairness
rests on, and the promotion provenance the site refuses to serve without. Drift in any of
them silently weakens a guarantee no runtime check would catch, so the invariants are
asserted here as text facts.

The frontmatter is parsed with ``yaml.safe_load`` because the harness that loads these files
parses YAML -- a hand-rolled ``key: value`` splitter would accept blocks the real loader
rejects, which is the wrong direction for a guard to be wrong in.

NOTE: ``pyyaml`` is currently present only transitively (via ``uvicorn[standard]``). It
belongs in the ``dev`` dependency group in ``pyproject.toml``, which this unit does not own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

type Frontmatter = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parent.parent

SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "gauntlet" / "SKILL.md"
AGENT_PATHS = {
    "builder": REPO_ROOT / ".claude" / "agents" / "builder.md",
    "critic": REPO_ROOT / ".claude" / "agents" / "critic.md",
    "integrator": REPO_ROOT / ".claude" / "agents" / "integrator.md",
}
ALL_PATHS = {"gauntlet": SKILL_PATH, **AGENT_PATHS}

SERVO_INVOCATION = (
    "uv run python -m ui_servo.cli.servo \\\n"
    "  --candidates evidence/rounds/<round>/candidates \\\n"
    "  --part <part> \\\n"
    "  --round <round> \\\n"
    "  --out evidence/rounds/<round>/"
)

CANDIDATE_GRAMMAR = "<part>.<family>.<k>.html"
LEGACY_GRAMMAR = "<part>.<family>.html"

BLIND_DIR = "evidence/rounds/<round>/blind/<comparison-id>/"
BLIND_GRAMMAR = "<part>.<A|B>.<hash>"

PROMOTED_PATH = "site/promoted/<part>.html"
PROMOTED_ROUTE = "/fragments/promoted/{part}"
PROVENANCE = "<!-- ui-servo: gated round=<n> sha256=<hash> -->"

DISPATCH_FIELDS = (
    "part_spec",
    "contract_path",
    "exemplars_path",
    "round_id",
    "target_cell",
    "variant_index",
    "output_path",
)


def split_frontmatter(path: Path) -> tuple[Frontmatter, str]:
    """Return the YAML frontmatter block and the body beneath it."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        pytest.fail(f"{path}: no opening frontmatter fence")
    try:
        end = text.index("\n---\n", 3)
    except ValueError:
        pytest.fail(f"{path}: unterminated frontmatter block")

    try:
        parsed = yaml.safe_load(text[4 : end + 1])
    except yaml.YAMLError as exc:
        pytest.fail(f"{path}: frontmatter is not valid YAML -- {exc}")

    if not isinstance(parsed, dict):
        pytest.fail(f"{path}: frontmatter parsed as {type(parsed).__name__}, not a mapping")
    return parsed, text[end + 5 :]


@pytest.fixture(scope="module")
def bodies() -> dict[str, str]:
    return {name: split_frontmatter(path)[1] for name, path in ALL_PATHS.items()}


@pytest.mark.parametrize("name", sorted(ALL_PATHS))
def test_frontmatter_parses_as_yaml_with_string_scalars(name: str) -> None:
    parsed, body = split_frontmatter(ALL_PATHS[name])

    for key, value in parsed.items():
        assert isinstance(key, str), f"{name}: non-string key {key!r}"
        assert isinstance(value, str), (
            f"{name}: {key} parsed as {type(value).__name__};"
            " frontmatter values must be plain strings"
        )

    assert parsed.get("name") == name
    assert parsed.get("description", "").strip(), "description is required for dispatch"
    assert body.strip(), f"{name}: empty body"


@pytest.mark.parametrize("name", sorted(AGENT_PATHS))
def test_agents_declare_restricted_tools(name: str) -> None:
    parsed, _ = split_frontmatter(AGENT_PATHS[name])

    tools = parsed.get("tools")
    assert isinstance(tools, str) and tools.strip(), (
        f"{name}: agents must declare their tool surface explicitly, as a string"
    )
    assert "*" not in tools


def test_critic_tools_are_read_only() -> None:
    """A comparator that can write is a comparator that can become an author."""
    parsed, _ = split_frontmatter(AGENT_PATHS["critic"])

    granted = {tool.strip() for tool in parsed["tools"].split(",")}
    assert not granted & {"Write", "Edit", "Bash", "NotebookEdit"}


def test_skill_documents_the_exact_servo_invocation(bodies: dict[str, str]) -> None:
    """The CLI contract is U13's; the skill must quote it, not paraphrase it."""
    assert SERVO_INVOCATION in bodies["gauntlet"]
    assert "--dry-judges" in bodies["gauntlet"]


def test_skill_does_not_use_the_stale_parts_flag(bodies: dict[str, str]) -> None:
    assert "--parts" not in bodies["gauntlet"]


@pytest.mark.parametrize("name", ["gauntlet", "builder"])
def test_three_token_candidate_grammar_is_documented(
    bodies: dict[str, str], name: str
) -> None:
    body = bodies[name]

    assert CANDIDATE_GRAMMAR in body
    assert "hero.claude.2.html" in body, f"{name}: grammar needs a worked example"


@pytest.mark.parametrize("name", ["gauntlet", "builder"])
def test_legacy_two_token_form_is_marked_k_zero(bodies: dict[str, str], name: str) -> None:
    body = bodies[name]

    assert LEGACY_GRAMMAR in body or "hero.claude.html" in body
    assert "k = 0" in body, f"{name}: legacy form must be pinned to k = 0"


@pytest.mark.parametrize("name", ["gauntlet", "builder"])
def test_variant_index_is_never_folded_into_the_family_token(
    bodies: dict[str, str], name: str
) -> None:
    """Encoding k into the family breaks eligible_judges' self-preference guard."""
    body = bodies[name]

    assert "hero.claude2.html" in body, f"{name}: name the anti-pattern to forbid it"
    assert "family" in body.lower()


class TestBlindStaging:
    """Per-comparison isolation: a critic must not be able to survey the round."""

    def test_skill_mandates_per_comparison_directories(self, bodies: dict[str, str]) -> None:
        body = bodies["gauntlet"]

        assert BLIND_DIR in body, "blind staging must be nested per comparison"
        assert "comparison" in body.lower()

    def test_skill_uses_u13s_authoritative_blind_grammar(
        self, bodies: dict[str, str]
    ) -> None:
        body = bodies["gauntlet"]

        assert BLIND_GRAMMAR in body
        assert "authoritative" in body.lower(), (
            "the skill must record that servo owns this grammar, so U13 matches it"
        )

    def test_skill_forbids_handing_over_the_parent_directory(
        self, bodies: dict[str, str]
    ) -> None:
        body = bodies["gauntlet"]

        assert "Not its\n  parent" in body or "not its parent" in body.lower()
        assert "sibling" in body.lower()

    def test_critic_is_confined_to_one_comparison_directory(
        self, bodies: dict[str, str]
    ) -> None:
        body = bodies["critic"].lower()

        assert "comparison directory" in body or "comparison dir" in body
        assert "sibling" in body, "sibling comparisons must be named as off-limits"
        assert "parent" in body, "the parent blind/ dir must be named as off-limits"
        assert "candidates/" in body, "the candidates dir must be named as off-limits"
        assert "glob" in body, "the prohibition must cover listing, not just reading"

    def test_critic_is_warned_about_correlation_not_just_filenames(
        self, bodies: dict[str, str]
    ) -> None:
        """Enumerating siblings reconstructs authorship without ever reading a filename."""
        body = bodies["critic"].lower()

        assert "correlat" in body


class TestPromotion:
    """U14 owns the serving side; the skill must cite it accurately."""

    def test_skill_names_the_promoted_path_and_route(self, bodies: dict[str, str]) -> None:
        body = bodies["gauntlet"]

        assert PROMOTED_PATH in body
        assert PROMOTED_ROUTE in body
        assert "fragments::promoted::render" in body

    def test_skill_requires_the_provenance_comment_at_promotion(
        self, bodies: dict[str, str]
    ) -> None:
        body = bodies["gauntlet"]

        assert PROVENANCE in body
        assert "verifies the" in body.lower() or "verified" in body.lower()

    def test_skill_no_longer_carries_the_unbacked_schedule_marker(
        self, bodies: dict[str, str]
    ) -> None:
        """U14 exists now; 'scheduled-U14' was a promise standing in for a spec."""
        assert "scheduled-U14" not in bodies["gauntlet"]

    def test_integrator_reviews_the_promoted_assembly(self, bodies: dict[str, str]) -> None:
        body = bodies["integrator"]

        assert PROMOTED_PATH in body
        assert "gate" in body.lower(), "integration fixes must re-enter through the gates"


class TestBuilderDispatch:
    """A builder cannot write a file it was never told the path of."""

    @pytest.mark.parametrize("field", DISPATCH_FIELDS)
    def test_every_payload_field_is_specified(
        self, bodies: dict[str, str], field: str
    ) -> None:
        assert field in bodies["gauntlet"], f"dispatch payload is missing {field}"

    def test_payload_is_distinguished_from_critique_context(
        self, bodies: dict[str, str]
    ) -> None:
        body = bodies["gauntlet"]

        assert "not critique context" in body.lower()
        assert "isolation" in body.lower()

    def test_the_part_spec_is_no_longer_called_the_entire_brief(
        self, bodies: dict[str, str]
    ) -> None:
        """It is the design brief; the payload around it is operational."""
        assert "entire brief a builder will ever see" not in bodies["gauntlet"]


def test_builder_never_treats_an_island_as_fragment_content(
    bodies: dict[str, str],
) -> None:
    """Islands mount in the site shell; the class-0 sanitizer rejects them in fragments."""
    body = bodies["builder"]

    assert "htmx-only" in body or "only htmx" in body
    assert "site shell" in body or "site-shell" in body

    for banned in ("or a\n  WASM island", "or a WASM island", "custom element or"):
        assert banned not in body, (
            f"builder.md still offers islands as fragment content: {banned!r}"
        )


def test_builder_inputs_do_not_defer_to_site_readme(bodies: dict[str, str]) -> None:
    """The authoring contract lives in the agent file; a second copy is a drift source."""
    assert "site/README.md" not in bodies["builder"]
    assert "site/README.md" not in bodies["gauntlet"]


def test_integration_findings_re_enter_through_the_gates(bodies: dict[str, str]) -> None:
    body = bodies["gauntlet"]

    assert "re-run the deterministic gauntlet" in body
    assert "assembled page" in body


def test_bar_passed_requires_panel_integrator_and_final_gates(
    bodies: dict[str, str],
) -> None:
    body = bodies["gauntlet"]
    _, _, termination = body.partition("## 9. Termination")

    assert termination, "termination section is missing"
    assert "no major gap" in termination
    assert "no major findings" in termination
    assert "final assembly" in termination
