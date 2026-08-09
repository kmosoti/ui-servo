"""Pytest wiring shared by the whole suite.

The `playwright` marker means "this test drives a real headless browser", and
both the plain run (`-m "not live and not playwright"`) and CI depend on it
being accurate: a runner installs the playwright *package* along with the rest
of the dependencies but not the browser binaries, so an unmarked browser test
fails there at launch instead of being deselected.

Remembering to type the marker is a rule; these tests earn it instead by asking
for a browser-backed fixture. `tests/test_playwright_sensor.py` is why the
distinction has to be per-test rather than per-file: it mixes real captures
with pure contract and port tests that want to keep running in the plain suite.
"""

from __future__ import annotations

import pytest

# Fixtures that launch a real browser. Anything downstream of one is covered
# too — `item.fixturenames` carries the whole dependency closure, not just the
# fixtures named in the test signature.
BROWSER_FIXTURES = frozenset({"sensor"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if BROWSER_FIXTURES.intersection(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.playwright)
