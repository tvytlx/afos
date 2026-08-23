"""Wire protocol: newline-delimited JSON over a stream.

Deliberately human-readable. During development the console frontend is itself
under construction, so being able to drive the daemon with `socat` and read the
frames by eye is worth more than a compact binary encoding.

Frames are objects with a `t` (type) field.

  frontend -> daemon    daemon -> frontend
  ------------------    ------------------
  hello                 welcome
  input                 output   (stream: agent | exec | system | error)
  interrupt             state    (status: idle | busy)
  bye                   error
"""

from __future__ import annotations

import json
import os
from typing import Any

SOCKET_PATH = os.environ.get("AFOS_SOCKET", "/run/afos/afosd.sock")

# Frames are read with asyncio's StreamReader, whose default limit is 64 KiB --
# small enough that pasting a log file into the console blew past it, and
# asyncio signals that by raising ValueError from readline() rather than
# anything a protocol layer would think to catch. Both halves are set here so
# the ceiling is a stated number instead of an inherited default.
MAX_FRAME = 4 * 1024 * 1024
READ_LIMIT = MAX_FRAME + 64 * 1024  # headroom so the reader errors, not truncates


class ProtocolError(Exception):
    """A frame the peer sent that this side refuses.

    Always recoverable: the connection reports it and stays up. Killing a
    frontend over a bad frame is not acceptable when that frontend may be the
    machine's only console.
    """


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def truncate(text: str, limit: int = MAX_FRAME // 2) -> str:
    """Clamp outbound text so afosd cannot be made to emit a frame no peer can
    read back. `:exec cat /some/huge/file` is an ordinary thing to ask an agent."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[afos] ... truncated, {len(text) - limit} more characters"


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(line)} bytes exceeds {MAX_FRAME}")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"bad frame: {e}") from e
    if not isinstance(msg, dict) or "t" not in msg:
        raise ProtocolError("frame must be an object with a 't' field")
    return msg
