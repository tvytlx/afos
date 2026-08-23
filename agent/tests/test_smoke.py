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
from afos.protocol import MAX_FRAME, READ_LIMIT, decode, encode
from afos.session import SCROLLBACK

MAX_FRAME_CAP = MAX_FRAME
SCROLLBACK_CAP = SCROLLBACK


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
        reader, writer = await asyncio.open_unix_connection(
            self.socket, limit=READ_LIMIT
        )
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


class ResourceLimitTest(unittest.IsolatedAsyncioTestCase):
    """afosd runs for the life of the machine, and is the only way into it.

    "Small leak" and "eventually a brick" are the same sentence here, so the
    limits are tested rather than assumed.
    """

    async def test_oversize_frame_does_not_kill_the_frontend(self) -> None:
        """A 200KB paste used to raise ValueError out of asyncio's readline and
        take the connection down with an unhandled traceback. On tty1 that is
        the machine's only console."""
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": "A" * 200_000}))
            await writer.drain()
            await collect(reader, "no model wired up", timeout=10)

            # Still usable afterwards -- that is the property that matters.
            writer.write(encode({"t": "input", "text": ":exec echo survived-the-paste"}))
            await writer.drain()
            seen = await collect(reader, "survived-the-paste", timeout=10)
            self.assertTrue(
                any("survived-the-paste" in str(f.get("text", "")) for f in seen)
            )
            writer.close()

    async def test_unreachable_sessions_are_reaped(self) -> None:
        async with Harness() as h:
            for _ in range(20):
                r, w, _ = await h.connect(session="new")
                w.close()
                await w.wait_closed()
            await asyncio.sleep(0.5)
            live = h.daemon.registry.sessions
            self.assertEqual(
                len(live), 1, f"expected only the default session, got {list(live)}"
            )

    async def test_a_session_still_working_is_not_reaped(self) -> None:
        """Reaping must not race a turn that is still producing output for a
        frontend that will reattach."""
        async with Harness() as h:
            r1, w1, hello = await h.connect(session="new")
            w1.write(encode({"t": "input", "text": ":exec sleep 1; echo late"}))
            await w1.drain()
            await collect(r1, "busy", timeout=5)
            w1.close()
            await w1.wait_closed()
            await asyncio.sleep(0.3)
            self.assertIn(
                hello["session"],
                h.daemon.registry.sessions,
                "a session with work in flight was reaped",
            )

    async def test_outbox_is_bounded_in_bytes_not_frames(self) -> None:
        """The frame cap alone did not close the hole it looked like it closed.

        Frames are capped at 2MB each, so 4096 of them is still 8GB --
        `:exec cat /var/log/syslog` on a box with long JSON lines gets there in
        seconds. This asserts the byte budget directly rather than asserting a
        constant against a constant, which is what the previous version of this
        test did: it passed with the fix reverted.
        """
        from afos.daemon import OUTBOX_BYTES, Outbox

        outbox = Outbox()
        big = "x" * 100_000
        for _ in range(500):  # 50MB offered, into a 4MB budget
            outbox.put({"t": "output", "stream": "exec", "text": big})
        self.assertLessEqual(
            outbox._bytes, OUTBOX_BYTES, "outbox grew past its byte budget"
        )
        self.assertGreater(outbox.dropped, 0)

    async def test_the_gap_is_announced_in_band(self) -> None:
        """Reporting lost output only to journald reports it only to a place
        you need this console to read."""
        from afos.daemon import Outbox

        outbox = Outbox()
        for _ in range(500):
            outbox.put({"t": "output", "stream": "exec", "text": "y" * 100_000})
        first = await outbox.get()
        self.assertEqual(first.get("stream"), "error")
        self.assertIn("output lost", str(first.get("text", "")))

    async def test_state_frames_are_never_evicted(self) -> None:
        """busy/idle is what draws the prompt, and it sits at the head of
        exactly the flood that would evict it -- leaving a console that has
        finished working but never says so."""
        from afos.daemon import Outbox

        outbox = Outbox()
        outbox.put({"t": "state", "status": "busy"})
        for _ in range(500):
            outbox.put({"t": "output", "stream": "exec", "text": "z" * 100_000})
        outbox.put({"t": "state", "status": "idle"})

        states = [f for f, _ in outbox._frames if f.get("t") == "state"]
        self.assertEqual(
            [f["status"] for f in states],
            ["busy", "idle"],
            "a state frame was evicted; the console would never redraw its prompt",
        )

    async def test_a_frontend_that_never_reads_cannot_grow_the_daemon(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect(session="new")
            writer.write(encode({"t": "input", "text": ":exec seq 1 60000"}))
            await writer.drain()
            await asyncio.sleep(3)  # deliberately never read from `reader`

            session = [s for s in h.daemon.registry.sessions.values()][-1]
            self.assertLessEqual(len(session.history), SCROLLBACK_CAP)
            writer.close()

    async def test_a_huge_single_output_is_truncated(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec head -c 6000000 /dev/zero | tr '\\0' 'x'"}))
            await writer.drain()
            frames = await collect(reader, "exit 0", timeout=30)
            for f in frames:
                self.assertLessEqual(
                    len(encode(f)), MAX_FRAME_CAP,
                    "afosd emitted a frame no peer could read back",
                )
            writer.close()


class ShutdownTest(unittest.IsolatedAsyncioTestCase):
    """afosd must stop when told to, with a console still attached.

    Python 3.12 -- the version the shipped image runs -- changed
    Server.wait_closed() to wait for outstanding connection handlers. A console
    is attached for the life of the machine, so before the fix every
    `systemctl restart afosd` hung until TimeoutStopSec expired and systemd
    SIGKILLed the daemon. On 3.11 it passes trivially, which is the point of
    running this suite inside the container too.
    """

    async def test_stop_returns_with_a_frontend_attached(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            daemon = Daemon(str(Path(tmp.name) / "d.sock"))
            await daemon.start()
            serving = asyncio.create_task(daemon.serve_forever())

            reader, writer = await asyncio.open_unix_connection(
                daemon.socket_path, limit=READ_LIMIT
            )
            writer.write(encode({"t": "hello", "frontend": "held", "session": None}))
            await writer.drain()
            await reader.readline()

            serving.cancel()
            await asyncio.wait_for(daemon.stop(), timeout=10)
            self.assertFalse(Path(daemon.socket_path).exists())
            writer.close()
        finally:
            tmp.cleanup()

    async def test_stop_returns_mid_turn(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            daemon = Daemon(str(Path(tmp.name) / "d.sock"))
            await daemon.start()
            serving = asyncio.create_task(daemon.serve_forever())

            reader, writer = await asyncio.open_unix_connection(
                daemon.socket_path, limit=READ_LIMIT
            )
            writer.write(encode({"t": "hello", "frontend": "busy", "session": None}))
            writer.write(encode({"t": "input", "text": ":exec sleep 30"}))
            await writer.drain()
            await asyncio.sleep(0.5)

            serving.cancel()
            await asyncio.wait_for(daemon.stop(), timeout=10)
            writer.close()
        finally:
            tmp.cleanup()


class InterruptQueueTest(unittest.IsolatedAsyncioTestCase):
    """Ctrl-C must stop the batch, not just the command in flight.

    Cancelling only the running turn meant the next queued command started
    immediately, so an operator on the only console was interrupting one
    command at a time while the queue kept feeding.
    """

    async def test_interrupt_discards_the_queue(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":exec sleep 30"}))
            for n in range(3):
                writer.write(encode({"t": "input", "text": f":exec echo queued-{n}"}))
            await writer.drain()
            await collect(reader, "busy", timeout=5)
            await asyncio.sleep(0.3)

            writer.write(encode({"t": "interrupt"}))
            await writer.drain()
            await collect(reader, "discarded", timeout=10)

            writer.write(encode({"t": "input", "text": ":exec echo after-interrupt"}))
            await writer.drain()
            seen = await collect(reader, "after-interrupt", timeout=15)
            texts = [str(f.get("text", "")) for f in seen]
            self.assertFalse(
                [t for t in texts if t.startswith("queued-")],
                f"queued commands survived the interrupt: {texts}",
            )
            writer.close()


class SessionIdTest(unittest.IsolatedAsyncioTestCase):
    async def test_ids_do_not_repeat_across_daemon_restarts(self) -> None:
        """Ids restarted at s1 every time, so a frontend reconnecting with a
        remembered id landed silently in a different conversation."""
        seen = []
        for _ in range(2):
            async with Harness() as h:
                _, writer, hello = await h.connect(session="new")
                seen.append(hello["session"])
                writer.close()
        self.assertNotEqual(seen[0], seen[1])


class TimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_exec_timeout_can_be_raised(self) -> None:
        """`apt upgrade` outlives 120s, and it is an ordinary thing to ask this
        agent to do."""
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":timeout 600"}))
            await writer.drain()
            await collect(reader, "timeout set to 600s", timeout=10)

            session = h.daemon.registry.default
            self.assertEqual(session.exec_timeout, 600.0)
            writer.close()

    async def test_a_command_that_overruns_is_killed_and_reported(self) -> None:
        async with Harness() as h:
            reader, writer, _ = await h.connect()
            writer.write(encode({"t": "input", "text": ":timeout 1"}))
            await writer.drain()
            await collect(reader, "timeout set to 1s", timeout=10)

            writer.write(encode({"t": "input", "text": ":exec sleep 20"}))
            await writer.drain()
            frames = await collect(reader, "exit 124", timeout=20)
            self.assertTrue(
                any("killed after" in str(f.get("text", "")) for f in frames),
                "a killed command must say so, not just return a code",
            )
            writer.close()


if __name__ == "__main__":
    unittest.main()
