"""The seam where a model plugs in.

v0 ships a builtin-only brain on purpose: the daemon, the frontends, the
systemd units and the boot path all need to be built and booted before any
model is wired up, and none of them should have to change when one is. Swap
`respond` for a model loop; the rest of afos does not care.
"""

from __future__ import annotations

import time

from .session import Session

HELP = """\
afos v0 -- no model wired up yet. Builtins:

  :help              this
  :exec <cmd>        run a shell command (shell is a capability, not an entry)
  :sessions          list sessions in this daemon
  :who               this session and its attached frontends
  :quit              detach this frontend (the session keeps running)

Anything not starting with ':' is where the model will go."""


class BuiltinBrain:
    async def respond(self, session: Session, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith(":"):
            await self._builtin(session, text[1:])
            return
        await session.emit(
            "agent",
            "no model wired up yet -- see afos/brain.py. Try :help.",
        )

    async def _builtin(self, session: Session, line: str) -> None:
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()

        if cmd in ("help", "h", "?"):
            await session.emit("system", HELP)

        elif cmd == "exec":
            if not arg:
                await session.emit("error", "usage: :exec <cmd>")
                return
            rc = await session.run_shell(arg)
            await session.emit("system", f"exit {rc}")

        elif cmd == "sessions":
            reg = session.registry
            if reg is None:
                await session.emit("error", "session is not registered")
                return
            now = time.time()
            rows = [
                f"  {s.id:<6} {s.name:<12} {s.frontends} frontend(s)  "
                f"up {now - s.created:.0f}s" + ("  <- you" if s is session else "")
                for s in reg.sessions.values()
            ]
            await session.emit("system", "\n".join(rows))

        elif cmd == "who":
            await session.emit(
                "system",
                f"session {session.id} ({session.name}), "
                f"{session.frontends} frontend(s) attached",
            )

        else:
            await session.emit("error", f"unknown builtin ':{cmd}' -- try :help")
