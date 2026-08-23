"""afosd -- the agent daemon.

systemd is PID 1. afosd is the only interactive surface above it. Frontends are
thin clients over a Unix socket and hold no state, so any of them can die
without ending a conversation, and several can watch the same one.
"""

from __future__ import annotations

import argparse
import asyncio
import grp
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .brain import BuiltinBrain
from .protocol import SOCKET_PATH, ProtocolError, decode, encode
from .session import Session

log = logging.getLogger("afosd")

SOCKET_GROUP = "afos"  # non-root frontends (sshd ForceCommand) join via this group
SOCKET_MODE = 0o660
BYE_DRAIN_SECONDS = 30.0


class _Goodbye(Exception):
    """A frontend detaching on purpose -- not a fault, so not logged as one."""


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
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from an unclean exit would make bind() fail, and the
        # unit restarts on crash -- so this has to be survivable.
        if path.is_socket():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        _harden_socket(path)
        log.info("afosd %s listening on %s", __version__, path)

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        Path(self.socket_path).unlink(missing_ok=True)

    # -- connection handling -------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def sink(frame: dict[str, Any]) -> None:
            await outbox.put(frame)

        session: Session | None = None
        pump: asyncio.Task[None] | None = None
        frontend = "unknown"

        try:
            line = await reader.readline()
            if not line:
                return
            hello = decode(line)
            if hello.get("t") != "hello":
                writer.write(encode({"t": "error", "text": "expected hello"}))
                await writer.drain()
                return

            frontend = str(hello.get("frontend", "unknown"))
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

            pump = asyncio.create_task(self._pump(outbox, writer))
            await session.attach(sink)
            log.info("%s attached to %s", frontend, session.id)
            await session.emit("system", f"{frontend} attached")

            async for raw in reader:
                await self._dispatch(session, decode(raw))

        except _Goodbye:
            pass
        except (ProtocolError, ConnectionResetError) as e:
            log.warning("frontend %s: %s", frontend, e)
        finally:
            if session is not None:
                session.detach(sink)
                await session.emit("system", f"{frontend} detached")
            if pump is not None:
                pump.cancel()
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
            # the socket goes away.
            await session.drain(timeout=BYE_DRAIN_SECONDS)
            raise _Goodbye
        else:
            await session.emit("error", f"unknown frame type: {kind!r}")

    @staticmethod
    async def _pump(
        outbox: asyncio.Queue[dict[str, Any]], writer: asyncio.StreamWriter
    ) -> None:
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
