"""A session is the unit of interaction, and it lives in the daemon.

That inversion is the point of the whole architecture: a console on tty1, an
ssh client and an API caller can attach to the same session and watch the same
stream. A frontend dying does not end the conversation -- frontends are views.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import secrets
import shlex
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from . import exec as shell
from .protocol import truncate

if TYPE_CHECKING:  # pragma: no cover
    from .daemon import Registry

Sink = Callable[[dict[str, Any]], Awaitable[None]]

# afos removes every audit surface Linux had -- there is no login, no sudo,
# no shell history -- so this is the only record that the machine did anything.
# It goes to stderr, which systemd routes to journald; deliberately not the
# python3-systemd binding, because adding a dependency is a decision this
# project has not made yet.
audit = logging.getLogger("afos.audit")
log = logging.getLogger("afos.session")

_ids = itertools.count(1)
# Ids restarted at s1 on every afosd restart, so a frontend reconnecting with a
# remembered id landed silently in a different conversation. The epoch makes a
# stale id a rejection instead of a wrong answer.
_EPOCH = secrets.token_hex(2)
SCROLLBACK = 500
REPLAY = 50


class Session:
    def __init__(self, brain: Any, name: str | None = None) -> None:
        self.id = f"s{next(_ids)}.{_EPOCH}"
        self.name = name or self.id
        self.created = time.time()
        self.brain = brain
        self.registry: Registry | None = None
        # Set by the daemon from the kernel's view of the connection, not
        # from anything a client claimed about itself.
        self.peer: str = "unattributed"
        # `apt upgrade`, a filesystem check, a large copy: all of them run
        # longer than the default and all of them are ordinary things to
        # ask this agent to do. Adjustable per session via `:timeout`.
        self.exec_timeout: float = shell.DEFAULT_TIMEOUT
        self.history: list[dict[str, Any]] = []
        self._sinks: set[Sink] = set()
        self._turn = asyncio.Lock()
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None

    # -- frontends -----------------------------------------------------------

    async def attach(self, sink: Sink) -> None:
        self._sinks.add(sink)
        for frame in self.history[-REPLAY:]:
            await sink(frame)

    def detach(self, sink: Sink) -> None:
        self._sinks.discard(sink)

    def idle(self) -> bool:
        """No turn running and nothing queued -- safe to discard."""
        return not self._turn.locked() and self._inbox.empty()

    @property
    def frontends(self) -> int:
        return len(self._sinks)

    # -- output --------------------------------------------------------------

    async def emit(self, stream: str, text: str) -> None:
        await self._fanout(
            {"t": "output", "stream": stream, "text": truncate(text)}, record=True
        )

    async def _fanout(self, frame: dict[str, Any], record: bool = False) -> None:
        if record:
            self.history.append(frame)
            del self.history[:-SCROLLBACK]
        # Iterate a copy: one wedged frontend must not abort the broadcast.
        for sink in list(self._sinks):
            try:
                await sink(frame)
            except Exception:
                self._sinks.discard(sink)

    async def run_shell(self, cmd: str, timeout: float | None = None) -> int:
        timeout = self.exec_timeout if timeout is None else timeout
        started = time.monotonic()
        audit.info(
            "exec start session=%s peer=%s cmd=%s",
            self.id,
            self.peer,
            shlex.quote(cmd),
        )
        try:
            rc = await shell.run(
                cmd, lambda line: self.emit("exec", line), timeout=timeout
            )
        except asyncio.CancelledError:
            audit.info(
                "exec cancelled session=%s peer=%s after=%.1fs cmd=%s",
                self.id, self.peer, time.monotonic() - started, shlex.quote(cmd),
            )
            raise
        audit.info(
            "exec end session=%s peer=%s rc=%d after=%.1fs cmd=%s",
            self.id, self.peer, rc, time.monotonic() - started, shlex.quote(cmd),
        )
        return rc

    # -- input ---------------------------------------------------------------
    #
    # Input is queued and turns run strictly one at a time. The alternative --
    # rejecting input while busy -- reads fine on a tty where a human waits for
    # the prompt, and falls apart everywhere else: a script piping three lines,
    # or two frontends typing at once, would silently lose turns.

    async def submit(self, text: str) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._pump())
        await self._inbox.put(text)

    async def _pump(self) -> None:
        while True:
            text = await self._inbox.get()
            try:
                await self._run_turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The pump must outlive anything a turn can do to it. Letting
                # the exception end this task left the session accepting input
                # and answering nothing -- deaf, with no error, on a machine
                # where this may be the only session there is.
                log.exception("turn failed in session %s", self.id)
                with contextlib.suppress(Exception):
                    await self.emit("error", f"turn failed: {type(e).__name__}: {e}")
            finally:
                self._inbox.task_done()

    async def _run_turn(self, text: str) -> None:
        async with self._turn:
            await self._fanout({"t": "state", "status": "busy"})
            # The turn is its own task so interrupt() can cancel it without
            # taking the pump down with it.
            turn = asyncio.create_task(self.brain.respond(self, text))
            self._task = turn
            try:
                # wait(), not await turn: awaiting the task directly makes an
                # interrupt indistinguishable from the pump itself being
                # cancelled, and swallowing the latter leaves a task that can
                # never be shut down.
                await asyncio.wait({turn})
            except asyncio.CancelledError:
                turn.cancel()
                raise
            finally:
                self._task = None

            if turn.cancelled():
                await self.emit("system", "interrupted")
            elif turn.exception() is not None:
                e = turn.exception()
                await self.emit("error", f"{type(e).__name__}: {e}")
            await self._fanout({"t": "state", "status": "idle"})

    async def drain(self, timeout: float = 30.0) -> bool:
        """Wait for queued input to finish. Returns False if it did not.

        A frontend saying goodbye is not a reason to throw away the output of
        work already accepted -- especially when it is the only frontend.
        """

        async def settled() -> None:
            await self._inbox.join()
            async with self._turn:
                pass

        try:
            await asyncio.wait_for(settled(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def interrupt(self) -> bool:
        """Cancel the running turn AND discard whatever is queued behind it.

        Cancelling only the current turn meant Ctrl-C on the machine's only
        console could not stop a batch: the next queued command started
        immediately, so the operator was interrupting one command at a time
        while the queue kept feeding.
        """
        dropped = 0
        while True:
            try:
                self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._inbox.task_done()
            dropped += 1

        running = self._task is not None and not self._task.done()
        if running:
            self._task.cancel()
        if dropped:
            asyncio.create_task(
                self.emit("system", f"discarded {dropped} queued input(s)")
            )
        return running or bool(dropped)

    def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
