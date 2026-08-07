"""Composition roots: the one layer allowed to know that adapters exist.

Every other layer is constrained. ``domain`` is pure, ``ports`` is interfaces,
``control`` holds the loops and is deliberately stdlib-only so they can be run
against fakes, and ``adapters`` implement ports without knowing what drives them.
None of them may name a concrete adapter. But *something* has to: a hexagon with
no composition root is a diagram, not a program.

This package is that something. It is the outermost ring — it may import any
layer and any distribution — and it contains only entry points: parse arguments,
choose the concrete store, sanitiser, sensor and judges, hand them to a control
loop, print what came back. No policy lives here. If a decision in one of these
modules could change an outcome, it is in the wrong file.

The history is worth recording, because the earlier arrangement looked fine and
was not. ``main()`` used to live inside ``ui_servo.control`` and reach its
adapters through :func:`importlib.import_module`, on the argument that a dynamic
import keeps the module's import graph cheap. That much was true. What it also
did was make the dependency rule in ``tests/test_architecture.py`` pass while the
dependency was real — the guard read ``import`` statements, and a string passed
to ``import_module`` is not one. The rule was not being satisfied, it was being
evaded, and a guard that can be stepped around by spelling the import
differently protects nothing. So the imports here are ordinary and visible, the
layer that performs them is allowed to, and the guard now resolves literal
``import_module`` arguments too — see ``TestTheGuardItself``.
"""
