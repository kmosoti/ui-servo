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

    Static imports only. Dynamic ones are not resolved here because they are not
    permitted at all inside the hexagon -- see :data:`RUNTIME_IMPORT_POWERS`.
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
    return frozenset(found)


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


RUNTIME_IMPORT_POWERS: frozenset[str] = frozenset(
    {"import_module", "__import__", "exec", "eval", "importlib"}
)
"""Identifiers that turn a string into an imported module at runtime.

Referencing any of these inside the hexagon is a violation *in itself*, which is
a deliberate retreat from the previous design. That one tried to resolve dynamic
imports -- follow the alias, resolve the relative target, decide what was really
being imported -- and it lost, repeatedly. Three rounds of fixes, and a review
still produced sixteen one-line bypasses: ``__import__``,
``getattr(importlib, "import_module")``, ``importlib.__dict__[...]``,
``functools.partial``, ``staticmethod``, walrus and annotated and tuple and
for-target and default-argument bindings, ``exec`` of a literal import, and an
alias chain longer than the resolver's hard-coded bound. One of them defeated the
computed-name test as well, so a fully dynamic import was silently skipped by the
check written to catch exactly that.

The lesson is that recognising every spelling of a capability is a losing game,
because the language keeps offering more. So the rule is now about the capability
rather than its spelling: no layer inside the hexagon may hold a runtime importer
at all. That is checkable in a page of code and does not acquire a new case every
time somebody is cleverer than the last reviewer.

``importlib`` itself is on the list because holding the module is holding the
function: ``getattr(importlib, "import_module")`` needs no other name.
``from importlib.resources import files`` stays legal -- it binds neither
``importlib`` nor an importer, and reading a packaged data file is not a
dependency on another layer.

**Threat model, stated plainly.** This stops a shortcut, not an adversary.
Anyone able to commit to this repo can also edit this file; the guard exists so
that quietly breaking the layering takes a deliberate, visible act rather than a
convenient one-liner. `# arch: allow` is that visible act.
"""

ARCH_ALLOW: str = "# arch: allow"
"""Escape hatch. On the same line, with a reason after it, and it shows up in a
diff -- which is the whole point: the rule can be broken, but not silently."""


def runtime_import_powers(source: str) -> tuple[tuple[int, str], ...]:
    """Every reference to runtime import machinery, with its line number.

    Matches on identifier alone -- any ``Name`` or ``Attribute`` -- so it does not
    matter how the reference was obtained, only that the module has one.
    """
    lines = source.splitlines()
    allowed = {
        number for number, text in enumerate(lines, start=1) if ARCH_ALLOW in text
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        identifier: str | None = None
        match node:
            case ast.Name(id=name) if name in RUNTIME_IMPORT_POWERS:
                identifier = name
            case ast.Attribute(attr=attr) if attr in RUNTIME_IMPORT_POWERS:
                identifier = attr
            # `import importlib` and `import importlib.resources` both bind the
            # name `importlib`, and holding the module is holding the importer.
            # The `from importlib.resources import ...` form binds neither.
            case ast.Import(names=names) if any(
                alias.name == "importlib" or alias.name.startswith("importlib.")
                for alias in names
            ):
                identifier = "importlib"
            case ast.ImportFrom(module="importlib", names=names) if any(
                alias.name in RUNTIME_IMPORT_POWERS for alias in names
            ):
                identifier = "import_module"
            case _:
                continue
        if node.lineno not in allowed:
            found.append((node.lineno, identifier))
    return tuple(sorted(set(found)))


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
def test_no_runtime_import_machinery(module: str, path: Path) -> None:
    """No layer inside the hexagon may hold a runtime importer.

    The previous rule tried to work out *what* a dynamic import imported. This
    one refuses the capability, because sixteen demonstrated bypasses established
    that the resolver could not be made complete: every fix taught the next
    reviewer a new spelling.

    ``ui_servo.cli`` is exempt. It is the composition root, it is allowed to name
    anything, and it is outside the hexagon.
    """
    if _covers("ui_servo.cli", module):
        return
    powers = runtime_import_powers(path.read_text(encoding="utf-8"))
    assert not powers, (
        f"{module} references runtime import machinery at "
        + ", ".join(f"line {line} ({name})" for line, name in powers)
        + ". Inside the hexagon a module's dependencies must be readable from its "
        f"import statements. If this one is genuinely necessary, put `{ARCH_ALLOW} "
        "<reason>` on the line so the exception is a visible act rather than a "
        "convenient one."
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

    # ---------------------------------------------------------------- powers ---
    #
    # Every case below was demonstrated as a working bypass of the previous
    # resolve-the-dynamic-import design, by a reviewer, after three rounds of
    # fixing that design. They are kept as a set because the point is not any one
    # of them: it is that the set kept growing, which is what moved the rule from
    # "work out what this imports" to "you may not hold an importer".

    @pytest.mark.parametrize(
        ("label", "source"),
        [
            ("builtin", '__import__("ui_servo.adapters.nh3_sanitizer")\n'),
            ("builtin rebound", 'imp = __import__\nimp("ui_servo.adapters.nh3_sanitizer")\n'),
            ("builtins attr", 'import builtins\nbuiltins.__import__("x")\n'),
            ("plain", 'import importlib\nimportlib.import_module("x")\n'),
            ("from-import", 'from importlib import import_module\nimport_module("x")\n'),
            ("import alias", 'from importlib import import_module as load\nload("x")\n'),
            ("assign alias", 'from importlib import import_module\nload = import_module\nload("x")\n'),
            ("getattr", 'import importlib\ngetattr(importlib, "import_module")("x")\n'),
            ("__dict__", 'import importlib\nimportlib.__dict__["import_module"]("x")\n'),
            ("partial", 'import functools, importlib\nfunctools.partial(importlib.import_module)("x")\n'),
            ("staticmethod", 'from importlib import import_module\nclass L:\n    load = staticmethod(import_module)\n'),
            ("annassign", 'from importlib import import_module\nload: object = import_module\n'),
            ("walrus", 'from importlib import import_module\n(load := import_module)\n'),
            ("tuple target", 'from importlib import import_module\nload, other = import_module, None\n'),
            ("for target", 'from importlib import import_module\nfor load in (import_module,):\n    pass\n'),
            ("default arg", 'from importlib import import_module\ndef go(load=import_module):\n    pass\n'),
            ("boolop", 'from importlib import import_module\nload = None\nload = load or import_module\n'),
            ("submodule import", 'import importlib.resources\ngetattr(importlib, "import_module")("x")\n'),
            ("exec", 'exec("from ui_servo.adapters import nh3_sanitizer")\n'),
            ("eval", 'eval("__import__(\'ui_servo.adapters.nh3_sanitizer\')")\n'),
            ("computed name", 'import importlib\nimportlib.import_module("ui_servo.adapters." + n)\n'),
        ],
    )
    def test_every_route_to_a_runtime_importer_is_refused(self, label: str, source: str) -> None:
        assert runtime_import_powers(source), f"{label} passed: {source!r}"

    def test_reading_a_packaged_file_is_still_allowed(self) -> None:
        """`from importlib.resources import files` binds no importer.

        The rule has to leave this alone or it stops being about layering and
        starts being about the word "importlib" — three modules in this repo read
        their packaged contract this way.
        """
        assert not runtime_import_powers("from importlib.resources import as_file, files\n")
        assert not runtime_import_powers("from importlib.metadata import version\n")

    def test_the_escape_hatch_works_and_is_line_scoped(self) -> None:
        """The rule can be broken — visibly, in a diff, with a reason."""
        allowed = 'import importlib  # arch: allow -- plugin loader, see ADR-4\n'
        assert not runtime_import_powers(allowed)
        # Only the annotated line is exempt. Line 2 trips twice — once for the
        # `importlib` reference and once for the `.import_module` attribute —
        # which is the belt-and-braces the capability rule is supposed to have.
        mixed = allowed + 'importlib.import_module("ui_servo.adapters.x")\n'
        assert {line for line, _ in runtime_import_powers(mixed)} == {2}
        assert {name for _, name in runtime_import_powers(mixed)} == {
            "importlib",
            "import_module",
        }

    def test_the_composition_root_is_exempt_from_the_powers_rule(self) -> None:
        """`ui_servo.cli` is outside the hexagon; naming things is its job."""
        assert _covers("ui_servo.cli", "ui_servo.cli.servo")
        assert not _covers("ui_servo.cli", "ui_servo.control.servo")

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
