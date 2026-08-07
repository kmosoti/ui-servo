"""ui-servo: a Gauntlet Loop for agent-generated UI.

Layered as hexagonal ports and adapters, and the layering is load-bearing rather
than decorative -- it is what keeps the regulator's model of "good" (``domain``)
independent of whichever browser, model CLI or transport happens to be observing
it this week.

    domain    the reference signal and the rules that judge against it
    ports     the interfaces the loop needs from the world
    control   the loops themselves: fast homeostatic, slow exploratory
    adapters  concrete browsers, CLIs, stores implementing the ports

Dependency rule (enforced by ``tests/test_architecture.py``): ``domain`` imports
only the standard library and pydantic; ``control`` imports ``domain`` and
``ports``; only ``adapters`` reach outward.
"""
