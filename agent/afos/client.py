"""afos-console -- a thin frontend.

On tty1 it is what a getty would have been; over ssh it is what a login shell
would have been. Both are the same file, because the session lives in the
daemon and this only renders it. Line editing comes from the kernel's canonical
mode -- one more thing not worth reimplementing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from typing import Any, AsyncIterator

from .protocol import READ_LIMIT, SOCKET_PATH, ProtocolError, decode, encode

RESET = "\033[0m"
STYLE = {
    "agent": "",
    "exec": "\033[2m",
    "system": "\033[2;36m",
    "error": "\033[31m",
}
PROMPT = "\033[1;32mafos>\033[0m "


class Console:
    def __init__(
        self,
        socket_path: str,
        session: str | None,
        color: bool,
        # Must exceed the daemon's bye-drain (30s), or the console gives up
        # on exactly the output the daemon stayed open to deliver -- and
        # exits 0, so the truncation looks like the command's real output.
        linger: float = 35.0,
        wait: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.session = session
        self.color = color
        self.linger = linger
        self.wait = wait
        self.busy = False

    def _write(self, text: str) -> None:
        # Belt and braces: if anything ever leaves this fd non-blocking, a
        # burst of output must not be fatal to the only console on the box.
        for _ in range(200):
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
                return
            except BlockingIOError:
                time.sleep(0.01)
            except (BrokenPipeError, ValueError):
                return

    def _render(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "output":
            stream = str(frame.get("stream", "agent"))
            body = str(frame.get("text", ""))
            if self.color:
                body = f"{STYLE.get(stream, '')}{body}{RESET}"
            self._write(body + "\n")
        elif kind == "state":
            was, self.busy = self.busy, frame.get("status") == "busy"
            if was and not self.busy:
                self._prompt()
        elif kind == "error":
            self._write(f"{STYLE['error'] if self.color else ''}{frame.get('text')}{RESET}\n")

    def _prompt(self) -> None:
        self._write(PROMPT if self.color else "afos> ")

    async def run(self) -> int:
        try:
            reader, writer = await self._connect()
        except OSError as e:
            print(
                f"afos-console: cannot reach afosd at {self.socket_path}: {e}",
                file=sys.stderr,
            )
            return 69  # EX_UNAVAILABLE

        writer.write(encode({"t": "hello", "frontend": _frontend_name(), "session": self.session}))
        await writer.drain()

        # Ctrl-C interrupts the turn instead of killing the frontend -- on tty1
        # there is nothing to fall back to.
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(
            signal.SIGINT,
            lambda: (writer.write(encode({"t": "interrupt"})), self._write("^C\n")),
        )
        # There is nothing to suspend to. A getty could be stopped and the
        # kernel would still hand the tty to a shell; this console being
        # stopped leaves the machine with no interactive entry at all, and
        # systemd's Restart= cannot see a stopped process -- the unit still
        # reports active. So the terminal reflex must be inert here.
        for sig in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU, signal.SIGQUIT):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, signal.SIG_IGN)

        incoming = asyncio.create_task(self._drain(reader))
        outgoing = asyncio.create_task(self._forward_stdin(writer))
        await asyncio.wait({incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED)

        if not incoming.done():
            # stdin ended first. The daemon closes the connection in response to
            # our `bye`, which is what finishes `incoming` -- but a turn may
            # still be producing output, so let it land instead of truncating it.
            try:
                await asyncio.wait_for(incoming, timeout=self.linger)
            except asyncio.TimeoutError:
                incoming.cancel()
        outgoing.cancel()
        writer.close()
        return 0

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the socket, optionally waiting for afosd to appear.

        On tty1 the console is started by systemd alongside afosd, so losing the
        race is normal and exiting would just churn the tty through a restart
        loop. Waiting is also what makes the console survive an afosd restart
        without a gap a human would notice.
        """
        delay = 0.1
        while True:
            try:
                # Same limit as the daemon: the console must be able to read
                # back any frame afosd is willing to send, or a large but
                # perfectly legal output kills the only console on the box.
                return await asyncio.open_unix_connection(
                    self.socket_path, limit=READ_LIMIT
                )
            except (FileNotFoundError, ConnectionRefusedError, PermissionError):
                if not self.wait:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        async for raw in reader:
            try:
                frame = decode(raw)
            except ProtocolError as e:
                print(f"afos-console: {e}", file=sys.stderr)
                continue
            if frame.get("t") == "welcome":
                self._write(
                    f"afos {frame.get('version')} -- session {frame.get('session')}"
                    f" ({frame.get('name')})\n"
                )
                self._prompt()
            else:
                self._render(frame)

    async def _forward_stdin(self, writer: asyncio.StreamWriter) -> None:
        async for text in _stdin_lines():
            if text.strip() == ":quit":
                break
            writer.write(encode({"t": "input", "text": text}))
            await writer.drain()
        # Either :quit or EOF. Say goodbye so the daemon detaches cleanly rather
        # than logging a lost connection.
        writer.write(encode({"t": "bye"}))
        await writer.drain()


async def _stdin_lines() -> AsyncIterator[str]:
    """Yield lines from stdin.

    Deliberately a thread rather than loop.connect_read_pipe(). On a tty,
    systemd hands the unit the same open file description for stdin and stdout,
    and connect_read_pipe sets O_NONBLOCK on it -- so the first burst of output
    that fills the tty buffer makes sys.stdout.write raise BlockingIOError and
    the machine's only console exits. A daemon thread parked in read() costs
    nothing and mutates no flags.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def pump() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\n"))
        except (ValueError, OSError):
            pass  # stdin closed under us during shutdown
        loop.call_soon_threadsafe(queue.put_nowait, None)

    # daemon=True: a thread parked in read() must never hold up process exit.
    threading.Thread(target=pump, daemon=True, name="afos-stdin").start()
    while (line := await queue.get()) is not None:
        yield line


def _frontend_name() -> str:
    if os.environ.get("SSH_CONNECTION"):
        return "ssh"
    try:
        return f"console:{os.ttyname(0).rsplit('/', 1)[-1]}"
    except OSError:
        return "pipe"


def main() -> int:
    ap = argparse.ArgumentParser(prog="afos-console", description="afos frontend")
    ap.add_argument("--socket", default=SOCKET_PATH)
    ap.add_argument("--session", default=None, help="session id, 'new', or 'system'")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument(
        "--wait",
        action="store_true",
        help="block until afosd is listening instead of failing (used on tty units)",
    )
    args = ap.parse_args()

    color = not args.no_color and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    try:
        return asyncio.run(
            Console(args.socket, args.session, color, wait=args.wait).run()
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
