"""Control: the loops that close between what was observed and what was wanted.

Two loops with deliberately different periods live here. The fast homeostatic
loop (``regulator.py``) compares deterministic observations to the direction
contract and acts without asking anyone; the slow exploratory loop
(``critique.py`` and ``explore.py``) spends model latency on the question no gate
can answer -- whether the thing is any good -- and feeds gaps back as findings.

The layer is held to the standard library plus :mod:`ui_servo.domain` and
:mod:`ui_servo.ports` by ``tests/test_architecture.py``. That is what makes the
loops runnable against fakes: a browser, a CLI or a wire is always on the far
side of a port, so a whole round can be exercised in a unit test without a
process, a network or a screenshot.

Submodules are imported explicitly rather than re-exported here, so importing one
loop never drags the other's dependencies in behind it.
"""
