"""Shell as a capability, not an entry point.

afos gives no human a login shell. The agent gets one the way it gets any other
tool: a scoped call with streamed output, a timeout, and a process group that
can actually be killed -- including the grandchildren a naive kill would leak.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Awaitable, Callable

DEFAULT_TIMEOUT = 120.0
TIMEOUT_RC = 124  # matches coreutils timeout(1)

CHUNK = 64 * 1024
# Longest run of bytes emitted as one line. Reading with StreamReader.readline()
# instead would inherit asyncio's own 64KiB limit, which does not truncate --
# it raises ValueError and fails the whole turn. `cat` on a file with one long
# line (minified JS, a base64 blob, a JSON log) is an ordinary thing to ask an
# agent to do, so output is chunked here rather than assumed to have newlines.
MAX_LINE = 64 * 1024


async def run(
    cmd: str,
    on_line: Callable[[str], Awaitable[None]],
    timeout: float = DEFAULT_TIMEOUT,
    cwd: str | None = None,
) -> int:
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,
    )

    async def pump() -> None:
        assert proc.stdout is not None
        buf = bytearray()

        async def flush(upto: int) -> None:
            await on_line(bytes(buf[:upto]).decode("utf-8", "replace"))
            del buf[: upto + 1]

        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                break
            buf += chunk
            while (nl := buf.find(b"\n")) >= 0:
                await flush(nl)
            # No newline in sight and the buffer is getting long: emit what we
            # have rather than growing without bound waiting for one.
            while len(buf) >= MAX_LINE:
                await on_line(bytes(buf[:MAX_LINE]).decode("utf-8", "replace"))
                del buf[:MAX_LINE]
        if buf:
            await on_line(bytes(buf).decode("utf-8", "replace"))

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await on_line(f"[afos] killed after {timeout:.0f}s")
        return TIMEOUT_RC
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    return proc.returncode or 0


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
