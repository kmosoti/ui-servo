"""Run an ASGI app on a real local port, for the length of a ``with`` block.

Two callers need this and neither can use the ``granian`` command line: the
gauntlet round (:func:`ui_servo.cli.servo.previews`) and the browser tests. Both
build their app from values that exist only at runtime -- a temporary directory,
a live evidence store, the turn id of *this* round -- so there is no
``module:app`` string to point a server process at, and the app object has to be
served in the process that built it.

Granian's embedded server is exactly that: workers as asyncio tasks rather than
child processes, so the app object is passed straight in. It is driven from a
daemon thread here because every caller is synchronous -- Playwright drives a
browser, the round drives a loop -- and neither is willing to become async to
own a server they only need a URL for.

Two things are worth knowing before reading the code:

* **The port is chosen here, not by the kernel at bind time.** Granian rebuilds
  its listening socket inside the worker (``SO_REUSEPORT`` on Linux), so a
  ``port=0`` handed to it would be resolved somewhere this process cannot see
  and, with more than one worker, differently per worker. A port is picked by
  binding and releasing one instead, which leaves a window of a few microseconds
  in which something else could take it. This is a genuine regression from the
  uvicorn harness it replaces, which bound a socket and handed it straight to
  the server (``sockets=[...]``) so the port could never be stolen -- granian's
  embedded :class:`Server` accepts no equivalent, so that race cannot be closed
  the same way here. What :func:`serve` can do, and does, is not pretend the
  race away and not die to it either: an auto-selected port that loses the race
  is abandoned for a fresh one, a bounded number of times, before giving up
  loudly. A *caller-supplied* port is never retried onto a different one --
  silently moving off a port the caller asked for by name would be a worse
  surprise than failing on it.
* **Readiness is a completed TCP connection.** Granian exposes no "started"
  flag, and the alternative -- sleeping and hoping -- is how a browser test
  becomes flaky. The thread is watched at the same time, so a server that dies
  during startup raises the reason instead of running out the clock. This is a
  weaker guarantee than it looks: granian's own ``on_startup`` hook fires
  *before* it spawns the worker that actually owns the ASGI app, so it answers
  "the process began starting", not "the app is ready" -- strictly earlier,
  and no better a guarantee, than a socket that accepts. The lifespan this
  module drives is a no-op today, which is why the gap has never bitten; a
  future lifespan hook is the reason to revisit this, not to reach for
  ``on_startup`` as a substitute. It also means "accepts" is read loosely: a
  connect that succeeds because *something else* is listening on the port
  reads the same as one answered by this module's own worker. That is the
  same race the port selection above already accepts, seen from the other
  side, and is not separately guarded against here.
"""

import asyncio
import contextlib
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from granian.constants import Interfaces
from granian.server.embed import Server

LOCALHOST = "127.0.0.1"
STARTUP_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 10.0
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
PORT_RACE_RETRIES = 3
"""Extra attempts :func:`serve` makes at a fresh auto-selected port after one
is lost to the bind race described in the module docstring."""


def free_port(host: str = LOCALHOST) -> int:
    """An ephemeral port the kernel has just handed out and nobody holds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _accepting(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def _is_port_conflict(error: BaseException) -> bool:
    """Granian raises a bare ``RuntimeError`` out of its Rust bind call; the
    message text is the only thing distinguishing "this machine's port was
    taken" from every other reason a worker can fail to start."""
    return isinstance(error, RuntimeError) and "already in use" in str(error).lower()


def _build_server(app: Any, host: str, port: int, *, log_enabled: bool) -> Server:
    """The one place ``granian.server.embed.Server`` is constructed, so
    :func:`serve_forever` and :func:`serve` cannot drift on how they build it."""
    return Server(app, address=host, port=port, interface=Interfaces.ASGI, log_enabled=log_enabled)


def serve_forever(app: Any, *, host: str = LOCALHOST, port: int = 8000) -> None:
    """Serve *app* in the calling thread until interrupted, then stop cleanly.

    For a foreground command, where blocking *is* the point. ``SIGINT`` and
    ``SIGTERM`` are wired to granian's own interrupt so the workers are drained
    rather than cancelled mid-request -- which is what separates a Ctrl-C from a
    kill, and what a service manager on a droplet expects of a ``stop``.
    """
    server = _build_server(app, host, port, log_enabled=True)

    async def until_stopped() -> None:
        loop = asyncio.get_running_loop()
        for number in STOP_SIGNALS:
            loop.add_signal_handler(number, server.signal_handler_interrupt)
        await server.serve()

    asyncio.run(until_stopped())


def _spawn(
    app: Any, host: str, port: int, *, log_enabled: bool
) -> tuple[Server, asyncio.AbstractEventLoop, threading.Thread, list[BaseException]]:
    """One attempt at standing the server up: build it, start its worker thread,
    and hand back everything needed to await startup or tear it back down.

    *failure* is a list rather than a plain variable so the closure below can
    append to it from the worker thread and the caller can read it from its
    own -- there is no ``nonlocal`` that reaches across a thread boundary.
    """
    server = _build_server(app, host, port, log_enabled=log_enabled)
    loop = asyncio.new_event_loop()
    failure: list[BaseException] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except BaseException as error:  # noqa: BLE001 — see below; the catch is the point
            # Kept, not raised: this is a thread nobody is joining yet, and a
            # traceback printed here would be all the caller ever learned about
            # a port that was taken or an app that could not be built. Narrowing
            # this would let exactly the failures worth reporting escape into a
            # thread with no listener, which is the bug the `failure` list exists
            # to prevent.
            failure.append(error)
        finally:
            loop.close()

    thread = threading.Thread(target=run, name=f"granian-{port}", daemon=True)
    thread.start()
    return server, loop, thread, failure


@contextlib.contextmanager
def serve(
    app: Any,
    *,
    host: str = LOCALHOST,
    port: int | None = None,
    startup_timeout: float = STARTUP_TIMEOUT,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT,
) -> Iterator[str]:
    """Serve *app* on ``http://host:port`` and yield its base URL.

    *port* defaults to a free one (see the module docstring for the bind race
    that entails, and the retries this takes for it). The server is stopped
    and joined on the way out, including when the body raises: a leaked
    server holds the port and the evidence store's lock, and the next round
    would then fail for a reason that has nothing to do with the next round.

    Two failures the old uvicorn-based harness could not hide either, but this
    one used to swallow, are now raised out of the ``with`` block -- only when
    the block's own body exited cleanly, so a real error in the caller's code
    is never masked by a secondary one about the server around it: a worker
    that crashes *after* startup succeeded, and a worker that is still alive
    ``shutdown_timeout`` seconds after being asked to stop.
    """
    auto_port = port is None
    retries_left = PORT_RACE_RETRIES if auto_port else 0
    while True:
        bind = free_port(host) if auto_port else port
        assert bind is not None
        server, loop, thread, failure = _spawn(app, host, bind, log_enabled=False)
        try:
            _await_startup(thread, failure, host, bind, startup_timeout)
            break
        except RuntimeError:
            # A losing attempt is joined, not abandoned: its closure still
            # names this iteration's `server`/`loop`, and starting the next
            # attempt before this thread has actually exited would let the
            # two race over which one they close.
            thread.join(timeout=shutdown_timeout)
            if retries_left > 0 and failure and _is_port_conflict(failure[0]):
                retries_left -= 1
                continue
            raise

    try:
        yield f"http://{host}:{bind}"
    finally:
        # The thread may finish between the liveness check and the wakeup, which
        # closes the loop underneath it; a server that has already stopped needs
        # no stopping.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(server.signal_handler_interrupt)
        thread.join(timeout=shutdown_timeout)
        if sys.exc_info()[0] is None:
            if failure:
                raise RuntimeError(f"the granian worker on {host}:{bind} failed") from failure[0]
            if thread.is_alive():
                raise RuntimeError(
                    f"the granian worker on {host}:{bind} did not stop within "
                    f"{shutdown_timeout}s; its socket may still be held"
                )


def _await_startup(
    thread: threading.Thread,
    failure: list[BaseException],
    host: str,
    port: int,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if failure:
            raise RuntimeError(f"the server on {host}:{port} failed to start") from failure[0]
        if not thread.is_alive():
            raise RuntimeError(f"the server thread for {host}:{port} exited during startup")
        if _accepting(host, port):
            return
        time.sleep(0.02)
    raise RuntimeError(f"the server on {host}:{port} did not accept a connection in {timeout}s")
