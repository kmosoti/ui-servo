"""The ASGI application that serves ``POST /beacon`` in production.

``router.py`` deliberately takes its store and its turn id as arguments so that
the preview server, a test's temporary directory and a deployed daemon can all
mount the same handlers without any of them knowing where the stock lives. That
left one thing unwritten: the object Granian actually imports. This module is
it, and it is almost entirely configuration -- reading the environment,
refusing to start on a bad value, and handing the result to
:func:`create_beacon_router`.

It is run as::

    granian --interface asgi --factory \\
        ui_servo.adapters.beacon_ingest.app:create_app \\
        --host 127.0.0.1 --port 8111

``--factory`` matters: the app is built when Granian calls it, not when the
module is imported, so importing this module in a test never touches the
environment and never creates a directory.

**Configuration refuses rather than guesses.** Both the Rust site and this
service take the same line -- a misconfigured server that starts is worse than
one that does not, because the failure surfaces later, as evidence quietly
going missing, instead of immediately, in the unit's status. The one required
variable has no default at all.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from litestar import Litestar

from ui_servo.adapters.beacon_ingest.router import (
    IngestHealth,
    create_beacon_router,
    validate_turn_id,
)
from ui_servo.adapters.jsonl_store import JsonlEvidenceStore

ROOT_ENV: Final[str] = "UI_SERVO_INGEST_ROOT"
TURN_ENV: Final[str] = "UI_SERVO_INGEST_TURN"
FSYNC_ENV: Final[str] = "UI_SERVO_INGEST_FSYNC"

TURN_PREFIX: Final[str] = "prod-"


class ConfigError(RuntimeError):
    """The environment does not describe a service that can safely start."""


def _root_from_env(environ: os._Environ[str] | dict[str, str]) -> Path:
    """The directory that *contains* ``evidence/``, never that directory itself.

    :class:`JsonlEvidenceStore` appends ``evidence/`` to whatever root it is
    given, so pointing this at an existing ``.../evidence`` yields
    ``.../evidence/evidence`` -- the same shape of mistake
    ``UI_SERVO_PROMOTED_ROOT`` invites on the Rust side, and worth refusing
    outright rather than discovering in a directory listing a week later.
    """
    raw = environ.get(ROOT_ENV, "").strip()
    if not raw:
        raise ConfigError(
            f"{ROOT_ENV} is required: the directory that will contain evidence/ "
            "(for example /var/lib/ui-servo)"
        )
    root = Path(raw)
    if not root.is_absolute():
        raise ConfigError(f"{ROOT_ENV} must be an absolute path, got {raw!r}")
    if root.name == "evidence":
        raise ConfigError(
            f"{ROOT_ENV}={raw!r} ends in 'evidence': the store appends that itself, "
            f"so this would write to {root / 'evidence'}. Give it the parent."
        )
    if not root.is_dir():
        raise ConfigError(f"{ROOT_ENV}={raw!r} is not an existing directory")
    return root


def _turn_from_env(environ: os._Environ[str] | dict[str, str], *, today: str) -> str:
    """A turn id is a filename, so a bad one is a path-traversal attempt.

    The default is date-stamped because the turn id names the JSONL file every
    accepted beacon is appended to: dating it means a restart opens a new file
    instead of growing one forever, and it gives the retention timer something
    with an age to prune. Files rotate on restart, not at midnight -- state that
    plainly rather than pretend a cron-shaped guarantee.
    """
    raw = environ.get(TURN_ENV, "").strip()
    candidate = raw or f"{TURN_PREFIX}{today}"
    try:
        return validate_turn_id(candidate)
    except ValueError as error:
        # ValueError, not BeaconError: validate_turn_id raises the base class,
        # and BeaconError is a *subclass* of it -- catching the subclass here
        # would let "../../etc/passwd" through this handler and out of the
        # factory as an unhandled crash instead of a named configuration error.
        raise ConfigError(f"{TURN_ENV}={candidate!r} is not a usable turn id: {error}") from error


def _fsync_from_env(environ: os._Environ[str] | dict[str, str]) -> bool:
    raw = environ.get(FSYNC_ENV, "").strip().lower()
    if raw in {"", "0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    raise ConfigError(f"{FSYNC_ENV}={raw!r} is not a boolean (use 1/0, true/false, yes/no)")


def create_app(environ: os._Environ[str] | dict[str, str] | None = None) -> Litestar:
    """Build the ingest app from the environment, or refuse to build one.

    *environ* is injectable so a test can exercise the refusals without
    mutating the process it runs in.
    """
    env = os.environ if environ is None else environ
    root = _root_from_env(env)
    turn_id = _turn_from_env(env, today=datetime.now(UTC).strftime("%Y-%m-%d"))
    store = JsonlEvidenceStore(root, fsync=_fsync_from_env(env))

    return Litestar(
        route_handlers=[
            create_beacon_router(store, turn_id=turn_id, health=IngestHealth()),
        ],
        # The probe is the only client and it reads no schema. Omitting the
        # config also takes /schema off the routing table, which keeps the
        # public surface of a write endpoint down to the two routes it needs.
        openapi_config=None,
        # Explicit, not merely default: debug mode renders tracebacks into
        # responses, and this is the one process on the droplet that a stranger
        # can POST to.
        debug=False,
        # Same reasoning as the preview server: an adapter does not get to
        # reconfigure the logging of the process hosting it. Under systemd,
        # stderr is journald's, and that is where these lines belong.
        logging_config=None,
    )
