"""Smoke test: the daemon boots, a frontend attaches, the shell capability runs,
and two frontends attached to one session see the same stream.

Deliberately protocol-level rather than unit-level -- the contract that matters
is the socket, because every frontend afos will ever grow talks through it.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from afos.daemon import Daemon
from afos.protocol import decode, encode


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = str(Path(self.tmp.name) / "afosd.sock")

    async def __aenter__(self) -> "Harness":
        self.daemon = Daemon(self.socket)
        await self.daemon.start()
        self.serving = asyncio.create_task(self.daemon.serve_forever())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.serving.cancel()
        await self.daemon.stop()
        self.tmp.cleanup()

    async def connect(self, frontend: str = "test", session: str | None = None):
        reader, writer = await asyncio.open_unix_connection(self.socket)
        writer.write(encode({"t": "hello", "frontend": frontend, "session": session}))
        await writer.drain()
        welcome = decode(await reader.readline())
        return reader, writer, welcome


async def collect(reader: asyncio.StreamReader, until: str, timeout: float = 10.0) -> list[dict]:
    """Read frames until one whose text contains `until`."""
    frames: list[dict] = []

    async def loop() -> None:
        async for raw in reader:
            frame = decode(raw)
            frames.append(frame)
            if until in str(frame.get("text", "")) or until == frame.get("status"):
                return

    await asyncio.wait_for(loop(), timeout)
    return frames


class SmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_welcome(self) -> None:
        async with Harness() as h:
            _, writer, welcome = await h.connect()
            self.assertEqual(welcome["t"], "welcome")
            self.assertEqual(welcome["name"], "system")
            writer.close()

    async def test_exec_capability(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec echo hello-from-afos"}))
            await writer.drain()
            frames = await collect(reader, "exit 0")
            texts = [str(f.get("text", "")) for f in frames]
            self.assertIn("hello-from-afos", texts)
            writer.close()

    async def test_exec_reports_failure(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec exit 3"}))
            await writer.drain()
            await collect(reader, "exit 3")
            writer.close()

    async def test_two_frontends_share_a_session(self) -> None:
        async with Harness() as h:
            r1, w1, hello1 = await h.connect("console")
            r2, w2, hello2 = await h.connect("ssh")
            self.assertEqual(hello1["session"], hello2["session"])

            # Input from the console frontend must surface on the ssh one.
            w1.write(encode({"t": "input", "text": ":exec echo shared-stream"}))
            await w1.drain()
            seen = await collect(r2, "shared-stream")
            self.assertTrue(any("shared-stream" in str(f.get("text", "")) for f in seen))
            w1.close()
            w2.close()

    async def test_unknown_session_is_rejected(self) -> None:
        async with Harness() as h:
            _, _, reply = await h.connect(session="s999")
            self.assertEqual(reply["t"], "error")


if __name__ == "__main__":
    unittest.main()


class QueueingTest(unittest.IsolatedAsyncioTestCase):
    """Turns must queue, not race -- a piped script is a first-class frontend."""

    async def test_input_is_serialised_in_order(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            for n in (1, 2, 3):
                writer.write(encode({"t": "input", "text": f":exec echo turn-{n}"}))
            await writer.drain()

            frames = await collect(reader, "turn-3")
            echoed = [t for t in (str(f.get("text", "")) for f in frames)
                      if t.startswith("turn-")]
            self.assertEqual(echoed, ["turn-1", "turn-2", "turn-3"])
            writer.close()

    async def test_bye_waits_for_accepted_work(self) -> None:
        """The bug this pins: `:quit` used to drop the output of the turn
        before it, which made every scripted run of the console look empty."""
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec sleep 0.4; echo slow-turn-done"}))
            writer.write(encode({"t": "bye"}))
            await writer.drain()

            seen: list[str] = []
            async for raw in reader:  # daemon closes the socket after draining
                seen.append(str(decode(raw).get("text", "")))
            self.assertIn("slow-turn-done", seen)


class InterruptTest(unittest.IsolatedAsyncioTestCase):
    """Ctrl-C has to kill the turn and leave the session usable.

    On tty1 there is no shell to fall back to, so a session that cannot recover
    from an interrupt is a machine that needs rebooting.
    """

    async def test_interrupt_kills_the_turn_not_the_session(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec sleep 30"}))
            await writer.drain()
            await collect(reader, "busy", timeout=5)  # state frame carries no text
            await asyncio.sleep(0.3)

            writer.write(encode({"t": "interrupt"}))
            await writer.drain()
            await collect(reader, "interrupted", timeout=5)

            # Still alive: the next turn must run normally.
            writer.write(encode({"t": "input", "text": ":exec echo still-here"}))
            await writer.drain()
            seen = await collect(reader, "still-here", timeout=10)
            self.assertTrue(any("still-here" in str(f.get("text", "")) for f in seen))
            writer.close()

    async def test_interrupt_with_nothing_running_is_harmless(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "interrupt"}))
            await writer.drain()
            await collect(reader, "nothing to interrupt", timeout=5)
            writer.close()


class ConsoleWaitTest(unittest.IsolatedAsyncioTestCase):
    """The console must win a race it is guaranteed to sometimes lose.

    systemd starts afos-console alongside afosd, so the socket is often not
    there yet. Exiting turns that into a restart loop that churns the tty; on a
    machine with no getty it once turned into no way in at all.
    """

    async def test_wait_blocks_until_the_daemon_appears(self) -> None:
        from afos.client import Console

        tmp = tempfile.TemporaryDirectory()
        socket = str(Path(tmp.name) / "late.sock")
        try:
            console = Console(socket, None, color=False, wait=True)
            connecting = asyncio.create_task(console._connect())

            # Nothing to connect to yet: it must still be trying.
            await asyncio.sleep(0.3)
            self.assertFalse(connecting.done(), "console gave up before afosd existed")

            daemon = Daemon(socket)
            await daemon.start()
            serving = asyncio.create_task(daemon.serve_forever())
            try:
                _, writer = await asyncio.wait_for(connecting, timeout=10)
                writer.close()
            finally:
                serving.cancel()
                await daemon.stop()
        finally:
            tmp.cleanup()

    async def test_without_wait_it_fails_fast(self) -> None:
        from afos.client import Console

        tmp = tempfile.TemporaryDirectory()
        try:
            console = Console(str(Path(tmp.name) / "absent.sock"), None, color=False)
            with self.assertRaises(OSError):
                await asyncio.wait_for(console._connect(), timeout=5)
        finally:
            tmp.cleanup()
