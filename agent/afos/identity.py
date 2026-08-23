"""Who is on the other end of the socket.

The `frontend` field in `hello` is a string the client chooses, so it names a
frontend the way a user-agent header names a browser: useful for a log line,
worthless for a decision. The kernel will tell us the truth about the peer, and
it is the only party in this exchange that cannot be lied to.

Nothing in v0 rejects anyone -- the socket is 0660 root:afos and `:exec` runs
as root, so membership in group `afos` is root-equivalent and pretending
otherwise would be theatre. What this module provides is the seam: a real
identity, and one place where an authorization decision goes when there is a
second frontend to make it about.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from dataclasses import dataclass

# Linux: struct ucred {pid_t, uid_t, gid_t}
_UCRED = struct.Struct("3i")


@dataclass(frozen=True)
class Peer:
    pid: int | None
    uid: int | None
    gid: int | None

    @property
    def known(self) -> bool:
        return self.uid is not None

    def __str__(self) -> str:
        if not self.known:
            return "peer(unknown)"
        return f"uid={self.uid} gid={self.gid} pid={self.pid}"


UNKNOWN = Peer(None, None, None)


def of(writer: asyncio.StreamWriter) -> Peer:
    """Read the peer's credentials from the connected socket.

    SO_PEERCRED is Linux-only; on macOS -- where the tests run but afos does
    not -- there is no equivalent that asyncio exposes cleanly, so the peer is
    simply unknown. That is the honest answer, and it keeps the seam identical
    on both platforms rather than inventing an identity the kernel did not give.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return UNKNOWN
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    except (AttributeError, OSError):
        return UNKNOWN
    pid, uid, gid = _UCRED.unpack(raw)
    return Peer(pid, uid, gid)


class Policy:
    """The authorization seam.

    v0 admits anyone the filesystem already admitted, because the socket mode
    is the whole boundary today and a second check against the same fact would
    only look like security. It exists so that the decision has one home when
    there is more than one frontend -- and so the answer to "who ran this" is
    recorded even while the answer to "may they" is always yes.
    """

    def __init__(self, root_uid: int = 0) -> None:
        self.root_uid = root_uid

    def admits(self, peer: Peer) -> tuple[bool, str]:
        return True, "socket permissions are the boundary in v0"

    def describe(self, peer: Peer) -> str:
        if not peer.known:
            return "unknown peer"
        if peer.uid == self.root_uid:
            return "root"
        try:
            import pwd

            return pwd.getpwuid(peer.uid).pw_name
        except (KeyError, ImportError):
            return f"uid {peer.uid}"


def is_root_equivalent() -> bool:
    """True when reaching the socket at all means root command execution.

    Which is the case whenever afosd runs as root without a policy on `:exec`.
    Stated as code so the docs cannot drift away from it again.
    """
    return os.geteuid() == 0
