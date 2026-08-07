"""The dependency rule, enforced as a test rather than as a convention.

Hexagonal layering is only real if something fails when it is broken. This walks
every module in the ``ui_servo`` package with :mod:`ast` -- no imports executed,
no third-party linter -- and checks two things per layer: which ``ui_servo``
packages it may reach, and which non-stdlib distributions it may reach. The
second half matters as much as the first: a ``ports`` module that imports FastAPI
has not violated an arrow on a diagram, but it has dragged infrastructure into
the interface layer, which is the same failure by a slower route.

    ui_servo           nothing (namespace and docstring only)
    ui_servo.domain    stdlib + pydantic; ui_servo.domain only
    ui_servo.ports     stdlib + pydantic; ui_servo.domain, ui_servo.ports
    ui_servo.control   stdlib only; ui_servo.domain, ui_servo.ports, ui_servo.control
    ui_servo.adapters  any distribution -- the world is allowed in here -- but only
                       ui_servo.domain, ui_servo.ports, ui_servo.adapters internally
    ui_servo.cli       anything: the composition roots, and the only layer that may
                       name a concrete adapter

Inside the hexagon every arrow points inward: nothing imports ``control``,
nothing imports ``adapters``, and there is no edge between those two layers in
either direction. An adapter that reaches for a loop has inverted the hexagon.
``cli`` sits outside all of it and is imported by nothing, which is what makes it
safe for it to import everything.

``control`` is held to stdlib-only on purpose: the loops must be runnable against
fakes, so anything that needs a wire, a browser or a vendor SDK belongs behind a
port. If a future unit genuinely needs a library there, the allowlist below is
the place that argument gets had, in a diff, once.

The extraction and rule logic is also tested directly against synthetic sources
in :class:`TestTheGuardItself`: a guard that has never been shown to fire is not
a guard, and today most of these layers are small or empty.
"""

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "ui_servo"
ROOT_PACKAGE = "ui_servo"


@dataclass(frozen=True, slots=True)
class LayerRule:
    """What one layer is allowed to depend on.

    ``third_party = None`` means unconstrained; an empty frozenset means stdlib
    only. ``internal`` holds the ``ui_servo`` prefixes the layer may import from.
    """

    internal: frozenset[str]
    third_party: frozenset[str] | None


LAYER_RULES: dict[str, LayerRule] = {
    "ui_servo": LayerRule(internal=frozenset(), third_party=frozenset()),
    "ui_servo.domain": LayerRule(
        internal=frozenset({"ui_servo.domain"}), third_party=frozenset({"pydantic"})
    ),
    "ui_servo.ports": LayerRule(
        internal=frozenset({"ui_servo.domain", "ui_servo.ports"}),
        third_party=frozenset({"pydantic"}),
    ),
    "ui_servo.control": LayerRule(
        internal=frozenset({"ui_servo.domain", "ui_servo.ports", "ui_servo.control"}),
        third_party=frozenset(),
    ),
    "ui_servo.adapters": LayerRule(
        internal=frozenset({"ui_servo.domain", "ui_servo.ports", "ui_servo.adapters"}),
        third_party=None,
    ),
    # The composition roots. This layer exists precisely to hold the imports no
    # other layer may make, so it is unconstrained in both directions -- and
    # nothing may import *it*, which is what keeps it outermost.
    "ui_servo.cli": LayerRule(
        internal=frozenset(
            {"ui_servo.domain", "ui_servo.ports", "ui_servo.control", "ui_servo.adapters", "ui_servo.cli"}
        ),
        third_party=None,
    ),
}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _covers(prefix: str, dotted: str) -> bool:
    return dotted == prefix or dotted.startswith(f"{prefix}.")


def rule_for(module: str) -> LayerRule | None:
    """The most specific layer rule covering *module*."""
    matches = [layer for layer in LAYER_RULES if _covers(layer, module)]
    match matches:
        case []:
            return None
        case _:
            return LAYER_RULES[max(matches, key=len)]


def imported_modules(source: str, module: str, *, is_package: bool) -> frozenset[str]:
    """Absolute dotted names imported by *module*, relative imports resolved.

    ``from x import y`` yields both ``x`` and ``x.y``: whether ``y`` is a
    submodule or an attribute is unknowable without importing, and a layering
    check must not be the thing that decides to execute application code.

    ``importlib.import_module("x.y")`` counts too, and that is not pedantry. An
    earlier version of this repo had ``ui_servo.control.servo`` reach its
    adapters that way, with a docstring explaining that it kept the import graph
    honest. It did the opposite: the dependency was real, this guard read only
    ``import`` statements, and the rule passed while being broken. A layering
    check that any developer can step around by spelling the import differently
    is decoration. Only literal arguments are resolvable -- a computed module
    name is caught by :func:`test_no_module_name_is_computed` instead.
    """
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Import(names=names):
                found.update(alias.name for alias in names)
            case ast.ImportFrom(module=target, names=names, level=level):
                match level:
                    case 0:
                        base = target or ""
                    case _:
                        anchor = package.split(".") if package else []
                        trimmed = anchor[: len(anchor) - (level - 1)] if level > 1 else anchor
                        base = ".".join([*trimmed, *([target] if target else [])])
                if not base:
                    continue
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in names)
            case ast.Call(func=func, args=[ast.Constant(value=str(target)), *_]):
                if _is_import_module(func):
                    found.add(target)
    return frozenset(found)


def _is_import_module(func: ast.expr) -> bool:
    """``import_module(...)`` or ``importlib.import_module(...)``, either spelling."""
    match func:
        case ast.Name(id="import_module"):
            return True
        case ast.Attribute(attr="import_module"):
            return True
        case _:
            return False


def dynamic_import_targets(source: str) -> tuple[ast.Call, ...]:
    """``import_module`` calls whose argument this guard cannot read."""
    return tuple(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _is_import_module(node.func)
        and not (node.args and isinstance(node.args[0], ast.Constant))
    )


def violations(module: str, imports: frozenset[str]) -> tuple[str, ...]:
    """Every import in *module* that its layer is not allowed to make."""
    rule = rule_for(module)
    if rule is None:
        return ()
    found: list[str] = []
    for imported in sorted(imports):
        top = imported.partition(".")[0]
        if top == ROOT_PACKAGE:
            if imported == ROOT_PACKAGE:
                continue
            if not any(_covers(prefix, imported) for prefix in rule.internal):
                found.append(
                    f"{module} imports {imported}; its layer may reach "
                    f"{sorted(rule.internal) or 'no ui_servo package'}"
                )
        elif top in sys.stdlib_module_names:
            continue
        elif rule.third_party is None or top in rule.third_party:
            continue
        else:
            found.append(
                f"{module} imports third-party {top!r}; its layer allows "
                f"{sorted(rule.third_party) or 'stdlib only'}"
            )
    return tuple(found)


def python_modules() -> Iterator[tuple[str, Path]]:
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield module_name(path), path


def _cases() -> list[tuple[str, Path]]:
    return list(python_modules())


def test_package_is_actually_scanned() -> None:
    modules = dict(_cases())
    assert ROOT_PACKAGE in modules
    assert "ui_servo.domain.contract" in modules
    assert all(rule_for(module) is not None for module in modules), (
        "every shipped module must fall under a layer rule; add new layers to LAYER_RULES"
    )


@pytest.mark.parametrize(("module", "path"), _cases(), ids=lambda value: str(value))
def test_no_module_name_is_computed(module: str, path: Path) -> None:
    """A module name this guard cannot read is a hole in it.

    Literal ``import_module("ui_servo.adapters.x")`` is resolved and checked like
    any other import. ``import_module(name)`` cannot be, so it is refused
    outright rather than silently skipped -- the failure mode this whole file
    exists to prevent is a dependency that is real and invisible.
    """
    computed = dynamic_import_targets(path.read_text(encoding="utf-8"))
    assert not computed, (
        f"{module} computes a module name at line(s) "
        f"{sorted({node.lineno for node in computed})}; the layer guard cannot see through it. "
        "Import it normally, or move the composition into ui_servo.cli."
    )


@pytest.mark.parametrize(("module", "path"), _cases(), ids=lambda value: str(value))
def test_layer_dependency_rule(module: str, path: Path) -> None:
    imports = imported_modules(
        path.read_text(encoding="utf-8"), module, is_package=path.name == "__init__.py"
    )
    assert violations(module, imports) == ()


class TestTheGuardItself:
    """Prove the checker fires, so the passing suite above means something."""

    def _imports(self, source: str, module: str, *, is_package: bool = False) -> frozenset[str]:
        return imported_modules(source, module, is_package=is_package)

    def test_detects_absolute_violation(self) -> None:
        imports = self._imports(
            "import ui_servo.adapters.playwright_browser\n", "ui_servo.domain.contract"
        )
        assert "ui_servo.adapters.playwright_browser" in imports
        assert violations("ui_servo.domain.contract", imports)

    def test_detects_the_importlib_dodge_that_used_to_pass(self) -> None:
        """The exact code this guard once let through, in both spellings.

        ``ui_servo.control.servo`` reached its adapters this way and the suite
        stayed green. That is the regression: not a wrong rule, an unenforced one.
        """
        for source in (
            'import importlib\n\ndef main():\n'
            '    importlib.import_module("ui_servo.adapters.nh3_sanitizer")\n',
            'from importlib import import_module\n\ndef main():\n'
            '    import_module("ui_servo.adapters.nh3_sanitizer")\n',
        ):
            imports = self._imports(source, "ui_servo.control.servo")
            assert "ui_servo.adapters.nh3_sanitizer" in imports, source
            assert violations("ui_servo.control.servo", imports), source

    def test_the_same_dodge_is_allowed_in_the_composition_root(self) -> None:
        """`cli` may name adapters however it likes; that is what it is for."""
        imports = self._imports(
            'import importlib\nimportlib.import_module("ui_servo.adapters.nh3_sanitizer")\n',
            "ui_servo.cli.servo",
        )
        assert not violations("ui_servo.cli.servo", imports)

    def test_a_computed_module_name_is_refused_rather_than_skipped(self) -> None:
        computed = dynamic_import_targets(
            'import importlib\n'
            'def load(name):\n'
            '    return importlib.import_module("ui_servo.adapters." + name)\n'
        )
        assert len(computed) == 1
        # A literal one is readable, so it is checked rather than refused.
        assert not dynamic_import_targets('import_module("ui_servo.adapters.x")\n')

    def test_no_layer_may_import_the_composition_root(self) -> None:
        """`cli` is outermost: importing it would drag every adapter inward."""
        for layer in ("ui_servo.domain", "ui_servo.ports", "ui_servo.control", "ui_servo.adapters"):
            imports = self._imports("from ui_servo.cli import servo\n", f"{layer}.thing")
            assert violations(f"{layer}.thing", imports), layer

    def test_detects_from_import_of_sibling_package(self) -> None:
        imports = self._imports("from ui_servo import control\n", "ui_servo.domain.contract")
        assert "ui_servo.control" in imports
        assert violations("ui_servo.domain.contract", imports)

    def test_domain_may_not_reach_any_non_domain_ui_servo_package(self) -> None:
        for source in (
            "from ui_servo.ports.store import EvidenceStore\n",
            "import ui_servo.evidence_dashboard\n",
            "from ui_servo import taste\n",
        ):
            imports = self._imports(source, "ui_servo.domain.rules")
            assert violations("ui_servo.domain.rules", imports), source

    def test_domain_third_party_limited_to_pydantic(self) -> None:
        assert violations(
            "ui_servo.domain.contract", self._imports("import httpx\n", "ui_servo.domain.contract")
        )
        assert (
            violations(
                "ui_servo.domain.contract",
                self._imports(
                    "import re\nimport tomllib\nfrom pydantic import BaseModel\n",
                    "ui_servo.domain.contract",
                ),
            )
            == ()
        )

    def test_ports_may_not_import_infrastructure(self) -> None:
        for source in ("import fastapi\n", "import playwright\n", "import nh3\n"):
            imports = self._imports(source, "ui_servo.ports.browser")
            assert violations("ui_servo.ports.browser", imports), source

    def test_ports_may_import_domain_and_pydantic(self) -> None:
        imports = self._imports(
            "from ui_servo.domain.contract import DirectionContract\nfrom pydantic import BaseModel\n",
            "ui_servo.ports.browser",
        )
        assert violations("ui_servo.ports.browser", imports) == ()

    def test_ports_may_not_import_adapters_or_control(self) -> None:
        for source in (
            "from ui_servo.adapters.chromium import Chromium\n",
            "from ui_servo.control.regulator import Regulator\n",
        ):
            imports = self._imports(source, "ui_servo.ports.browser")
            assert violations("ui_servo.ports.browser", imports), source

    def test_control_is_stdlib_plus_domain_and_ports(self) -> None:
        allowed = self._imports(
            "import asyncio\n"
            "from ui_servo.domain.contract import DirectionContract\n"
            "from ui_servo.ports.browser import Browser\n",
            "ui_servo.control.regulator",
        )
        assert violations("ui_servo.control.regulator", allowed) == ()
        for source in ("import playwright\n", "import httpx\n", "import pydantic\n"):
            denied = self._imports(source, "ui_servo.control.regulator")
            assert violations("ui_servo.control.regulator", denied), source

    def test_control_may_not_import_adapters(self) -> None:
        imports = self._imports(
            "from ui_servo.adapters.cli_panel import ClaudeCli\n", "ui_servo.control.critique"
        )
        assert violations("ui_servo.control.critique", imports)

    def test_adapters_may_import_any_distribution(self) -> None:
        imports = self._imports(
            "import playwright\n"
            "import nh3\n"
            "from ui_servo.domain.contract import DirectionContract\n"
            "from ui_servo.ports.browser import Browser\n",
            "ui_servo.adapters.chromium",
        )
        assert violations("ui_servo.adapters.chromium", imports) == ()

    def test_adapters_may_not_import_control(self) -> None:
        imports = self._imports(
            "from ui_servo.control.regulator import Regulator\n", "ui_servo.adapters.chromium"
        )
        assert violations("ui_servo.adapters.chromium", imports)

    def test_root_package_stays_a_namespace(self) -> None:
        assert violations(ROOT_PACKAGE, self._imports("import fastapi\n", ROOT_PACKAGE, is_package=True))
        assert violations(ROOT_PACKAGE, self._imports("from ui_servo import adapters\n", ROOT_PACKAGE, is_package=True))

    def test_resolves_relative_imports(self) -> None:
        imports = self._imports(
            "from ..adapters import chromium\nfrom . import contract\n", "ui_servo.domain.rules"
        )
        assert {
            "ui_servo.adapters",
            "ui_servo.adapters.chromium",
            "ui_servo.domain.contract",
        } <= imports
        assert violations("ui_servo.domain.rules", imports)

    def test_resolves_relative_imports_inside_a_package_init(self) -> None:
        imports = self._imports(
            "from .contract import DirectionContract\n", "ui_servo.domain", is_package=True
        )
        assert "ui_servo.domain.contract" in imports
        assert violations("ui_servo.domain", imports) == ()

    def test_similar_prefix_is_not_a_violation(self) -> None:
        imports = self._imports("import ui_servo_extras.thing\n", "ui_servo.adapters.chromium")
        assert violations("ui_servo.adapters.chromium", imports) == ()
        domain_imports = self._imports("import ui_servo.domain_notes\n", "ui_servo.domain.contract")
        assert violations("ui_servo.domain.contract", domain_imports)

    def test_rule_selection_prefers_the_most_specific_layer(self) -> None:
        assert rule_for("ui_servo.domain.contract") is LAYER_RULES["ui_servo.domain"]
        assert rule_for("ui_servo.adapters.chromium") is LAYER_RULES["ui_servo.adapters"]
        assert rule_for(ROOT_PACKAGE) is LAYER_RULES[ROOT_PACKAGE]
        assert rule_for("something_else") is None
