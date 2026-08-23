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


class ProtocolError(Exception):
    pass


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"bad frame: {e}") from e
    if not isinstance(msg, dict) or "t" not in msg:
        raise ProtocolError("frame must be an object with a 't' field")
    return msg
