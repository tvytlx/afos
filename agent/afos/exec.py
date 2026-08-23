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
        async for raw in proc.stdout:
            await on_line(raw.decode("utf-8", "replace").rstrip("\n"))

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
