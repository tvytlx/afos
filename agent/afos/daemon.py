"""afosd -- the agent daemon.

systemd is PID 1. afosd is the only interactive surface above it. Frontends are
thin clients over a Unix socket and hold no state, so any of them can die
without ending a conversation, and several can watch the same one.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import logging
import os
import signal
import sys
from collections import deque
from pathlib import Path
from typing import Any

from . import __version__
from . import identity
from .brain import BuiltinBrain
from .protocol import READ_LIMIT, SOCKET_PATH, ProtocolError, decode, encode
from .session import Session

log = logging.getLogger("afosd")

SOCKET_GROUP = "afos"  # non-root frontends (sshd ForceCommand) join via this group
SOCKET_MODE = 0o660
BYE_DRAIN_SECONDS = 30.0
FLUSH_SECONDS = 5.0

# A frontend that stops reading must not be able to grow afosd without bound.
# `:exec seq 1 400000` against a non-reading frontend took the daemon from 24MB
# to 126MB and climbing; on a machine whose only entry point is this process,
# the OOM killer is indistinguishable from a brick.
#
# The budget is in BYTES, not frames. A frame cap alone does not close the hole
# it looks like it closes: frames are capped at 2MB each, so 4096 of them is
# still 8GB. `:exec cat /var/log/syslog` on a box with long JSON lines gets
# there in seconds.
OUTBOX_BYTES = 4 * 1024 * 1024
OUTBOX_HIGH_WATER = OUTBOX_BYTES // 2
CLOSE_TIMEOUT = 5.0


class _Goodbye(Exception):
    """A frontend detaching on purpose -- not a fault, so not logged as one."""


class Outbox:
    """Frames waiting to reach one frontend, bounded by total encoded size.

    Two rules that a plain Queue does not give you, and that matter because the
    frontend on the other end may be the machine's only console:

    - `state` frames are never evicted. The busy/idle pair is what draws the
      prompt, and it is the frame most likely to be at the head of a flood --
      losing it leaves a console that has finished working but never says so.
    - A gap is announced in band. Reporting dropped output only to journald
      means reporting it only to a place you need this console to read.
    """

    def __init__(self, max_bytes: int = OUTBOX_BYTES) -> None:
        self.max_bytes = max_bytes
        self._frames: deque[tuple[dict[str, Any], int]] = deque()
        self._bytes = 0
        self._wake = asyncio.Event()
        self.dropped = 0
        self._announced = 0

    def put(self, frame: dict[str, Any]) -> None:
        size = len(encode(frame))
        self._frames.append((frame, size))
        self._bytes += size
        while self._bytes > self.max_bytes and len(self._frames) > 1:
            self._evict_oldest()
        self._wake.set()

    def _evict_oldest(self) -> None:
        for i, (frame, size) in enumerate(self._frames):
            if frame.get("t") == "state":
                continue
            del self._frames[i]
            self._bytes -= size
            self.dropped += 1
            return
        frame, size = self._frames.popleft()  # all state frames: shed anyway
        self._bytes -= size
        self.dropped += 1

    @property
    def pending(self) -> bool:
        return bool(self._frames) or self.dropped > self._announced

    @property
    def pressured(self) -> bool:
        return self._bytes > OUTBOX_HIGH_WATER

    async def get(self) -> dict[str, Any]:
        while True:
            if self.dropped > self._announced:
                lost = self.dropped - self._announced
                self._announced = self.dropped
                return {
                    "t": "output",
                    "stream": "error",
                    "text": f"[afos] output lost -- {lost} frame(s) dropped here",
                }
            if self._frames:
                frame, size = self._frames.popleft()
                self._bytes -= size
                return frame
            self._wake.clear()
            await self._wake.wait()


class Registry:
    def __init__(self, brain: Any) -> None:
        self.brain = brain
        self.sessions: dict[str, Session] = {}
        self.default = self.create(name="system")

    def create(self, name: str | None = None) -> Session:
        s = Session(self.brain, name=name)
        s.registry = self
        self.sessions[s.id] = s
        return s

    def reap(self) -> int:
        """Drop sessions with no frontends and no history worth keeping.

        Sessions are created on demand and were never removed: fifty frontends
        that each asked for `new` and disconnected left fifty unreachable
        sessions, each holding a pump task and up to 500 frames of scrollback.
        afosd is meant to run for the life of the machine, so "small leak" and
        "eventually fatal" are the same sentence.

        The default session is never reaped -- it is the one a frontend reaches
        by asking for nothing.
        """
        dead = [
            s
            for s in self.sessions.values()
            if s is not self.default and s.frontends == 0 and s.idle()
        ]
        for s in dead:
            s.close()
            del self.sessions[s.id]
        return len(dead)

    def resolve(self, ident: str | None) -> Session:
        if ident in (None, "", "system", "default"):
            return self.default
        if ident == "new":
            return self.create()
        s = self.sessions.get(ident or "")
        if s is None:
            raise KeyError(ident)
        return s


class Daemon:
    def __init__(self, socket_path: str = SOCKET_PATH) -> None:
        self.socket_path = socket_path
        self.registry = Registry(BuiltinBrain())
        self.policy = identity.Policy()
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from an unclean exit would make bind() fail, and the
        # unit restarts on crash -- so this has to be survivable.
        if path.is_socket():
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(path), limit=READ_LIMIT
        )
        _harden_socket(path)
        log.info("afosd %s listening on %s", __version__, path)

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Python 3.12 changed Server.wait_closed() to wait for outstanding
            # connection handlers before returning. On this machine a console is
            # attached for the life of the box, so the wait never ends: every
            # `systemctl restart afosd` would hang until TimeoutStopSec expired
            # and systemd SIGKILLed us. On 3.11 wait_closed() returned at once,
            # which is why it took running the tests on the Python the image
            # actually ships (3.12) to see it at all.
            for task in list(self._handlers):
                task.cancel()
            try:
                await asyncio.wait_for(self._server.wait_closed(), CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("connection handlers did not settle; closing anyway")
        Path(self.socket_path).unlink(missing_ok=True)

    # -- connection handling -------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)

        outbox = Outbox()
        session: Session | None = None
        pump: asyncio.Task[None] | None = None
        frontend = "unknown"

        async def sink(frame: dict[str, Any]) -> None:
            before = outbox.dropped
            outbox.put(frame)
            if outbox.dropped and not before:
                log.warning("frontend %s is not keeping up; dropping frames", frontend)
            if outbox.pressured:
                # Cooperative backpressure: yield so the writer task gets a
                # chance to drain the socket before the producer runs again.
                await asyncio.sleep(0)

        try:
            line = await reader.readline()
            if not line:
                return
            hello = decode(line)
            if hello.get("t") != "hello":
                writer.write(encode({"t": "error", "text": "expected hello"}))
                await writer.drain()
                return

            # The kernel's answer, not the client's. `hello.frontend` is a
            # label the client chose; it is fine for a log line and worthless
            # for a decision, so it is clamped and clearly subordinate.
            peer = identity.of(writer)
            claimed = str(hello.get("frontend", "unknown"))[:64]
            frontend = f"{claimed}[{self.policy.describe(peer)}]"

            allowed, why = self.policy.admits(peer)
            if not allowed:
                writer.write(encode({"t": "error", "text": f"refused: {why}"}))
                await writer.drain()
                return
            try:
                session = self.registry.resolve(hello.get("session"))
            except KeyError as e:
                writer.write(
                    encode({"t": "error", "text": f"no such session: {e.args[0]}"})
                )
                await writer.drain()
                return

            writer.write(
                encode(
                    {
                        "t": "welcome",
                        "version": __version__,
                        "session": session.id,
                        "name": session.name,
                    }
                )
            )
            await writer.drain()

            session.peer = str(peer)
            pump = asyncio.create_task(self._pump(outbox, writer))
            await session.attach(sink)
            log.info("%s attached to %s", frontend, session.id)
            await session.emit("system", f"{frontend} attached")

            while True:
                try:
                    raw = await reader.readline()
                except ValueError as e:
                    # asyncio's own limit, hit before ours. Recoverable in
                    # principle but the stream is now mid-frame, so the honest
                    # move is to say why and drop the connection -- the unit
                    # restarts the console immediately.
                    await session.emit("error", f"frame too large: {e}")
                    raise _Goodbye
                if not raw:
                    break
                try:
                    await self._dispatch(session, decode(raw))
                except ProtocolError as e:
                    # Recoverable: report and keep the frontend alive. This may
                    # be the machine's only console.
                    await session.emit("error", str(e))

        except _Goodbye:
            pass
        except (ProtocolError, ConnectionResetError) as e:
            log.warning("frontend %s: %s", frontend, e)
        except asyncio.CancelledError:
            pass  # afosd is shutting down; teardown below still runs
        finally:
            if handler is not None:
                self._handlers.discard(handler)
            if session is not None:
                session.detach(sink)
                try:
                    await session.emit("system", f"{frontend} detached")
                except asyncio.CancelledError:
                    pass
                reaped = self.registry.reap()
                if reaped:
                    log.info("reaped %d unreachable session(s)", reaped)
            if outbox.dropped:
                # One line per connection, not one per burst: journald is read
                # through the agent on this machine, and a flood of warnings
                # about a flood is its own denial of service.
                log.warning(
                    "frontend %s lost %d frame(s) in total", frontend, outbox.dropped
                )
            if pump is not None:
                await self._flush(outbox, pump, writer)
            writer.close()

    async def _dispatch(self, session: Session, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "input":
            await session.submit(str(msg.get("text", "")))
        elif kind == "interrupt":
            if not session.interrupt():
                await session.emit("system", "nothing to interrupt")
        elif kind == "bye":
            # Graceful: let accepted work finish and reach this frontend before
            # the socket goes away. Draining the session is only half of it --
            # the frames are then sitting in this connection's outbox, and
            # cancelling the writer task would throw away exactly the output
            # drain() just waited for.
            await session.drain(timeout=BYE_DRAIN_SECONDS)
            raise _Goodbye
        else:
            await session.emit("error", f"unknown frame type: {kind!r}")

    @staticmethod
    async def _flush(
        outbox: "Outbox", pump: asyncio.Task[None], writer: asyncio.StreamWriter
    ) -> None:
        """Give the writer a bounded chance to empty the outbox before closing.

        Without this, `:quit` after a slow command produced no output at all --
        the session drained, and then the frames died in the queue.
        """
        deadline = asyncio.get_running_loop().time() + FLUSH_SECONDS
        while outbox.pending and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        pump.cancel()
        with contextlib.suppress(Exception):
            await writer.drain()

    @staticmethod
    async def _pump(outbox: "Outbox", writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                writer.write(encode(await outbox.get()))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass


def _harden_socket(path: Path) -> None:
    """0660 root:afos -- a frontend needs group membership, not root."""
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
        os.chown(path, -1, gid)
    except (KeyError, PermissionError, OSError):
        pass  # group absent in dev containers; the mode below still applies
    os.chmod(path, SOCKET_MODE)


async def _amain(socket_path: str) -> int:
    daemon = Daemon(socket_path)
    await daemon.start()

    loop = asyncio.get_running_loop()
    stopping: asyncio.Future[int] = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda s=sig: stopping.done() or stopping.set_result(s)
        )

    serve = asyncio.create_task(daemon.serve_forever())
    await asyncio.wait({serve, stopping}, return_when=asyncio.FIRST_COMPLETED)
    serve.cancel()
    await daemon.stop()
    log.info("afosd stopped")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="afosd", description="afos agent daemon")
    ap.add_argument("--socket", default=SOCKET_PATH)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(_amain(args.socket))


if __name__ == "__main__":
    raise SystemExit(main())
