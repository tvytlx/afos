# afos

**Agent-First Operating System** — Ubuntu with every human entry point removed
and a single agent daemon in their place.

The kernel, the drivers, the network stack and systemd are Ubuntu's, untouched.
What afos replaces is the interactive surface: no desktop, no display manager,
no getty, no login shell. One agent, and nothing else to talk to.

> Early exploration. v0 boots and takes the console; the model is not wired up
> yet — see [`agent/afos/brain.py`](agent/afos/brain.py).

## Quick start

```bash
make dev
```

Builds a container and drops you into the agent console. Try `:help`.

## The three tiers

The dev environment is a ladder, not a menu. Debugging agent logic in the
slowest tier is the main way to waste a day on this project.

| | command | speed | answers |
|---|---|---|---|
| **T0** | `make dev` | seconds | agent logic, the protocol, userspace — **95% of the time** |
| **T1** | `make machine` | ~30s | units, boot order, restart policy, break-glass |
| **T2** | `make boot` | minutes | the real boot: does the agent take the console, and is there nothing else? |

T0 is a container: no boot, no tty1, no display manager to remove. It gives you
the exact userspace `afosd` runs in, in about a second. It cannot tell you
whether afos *boots* — only T2 can, which is why T2 is the gate rather than the
workbench.

```bash
make test      # 25 tests, no container needed
make boot      # downloads the Ubuntu cloud image, provisions it, boots it
make accept    # every tier's acceptance checks, fast to slow
```

Acceptance is executable, not a checklist to read — each tier prints PASS/FAIL
per criterion. Last full run: **T0 13/13 · T1 18/18 · T2 45/45**. What each
tier can and cannot prove: [docs/acceptance.md](docs/acceptance.md).

`make boot` uses QEMU on the serial console — there is no display to attach to,
by construction. Ctrl-A X to quit.

## Layout

```
agent/afos/
  daemon.py     afosd — owns every session; the only interactive surface
  client.py     afos-console — a thin frontend; tty1 and ssh are the same file
  session.py    a session, its queue, its subscribers
  brain.py      where a model plugs in
  exec.py       shell as a capability, not an entry point
  protocol.py   newline-delimited JSON over a Unix socket
init/           the systemd units that replace getty
scripts/        acceptance checks, one per tier
image/          cloud image → bootable afos
```

## Design

Three decisions worth knowing before reading the code:

- **systemd stays PID 1.** Process supervision, cgroups and ordered
  dependencies are exactly the underlying capability the project set out to
  reuse. Agent-first is about the interface, not about init.
- **Sessions live in the daemon, not the frontend.** So a console dying does
  not end a conversation, and ssh can watch tty1's session live.
- **The shell is not deleted, it is demoted** — from the human's entry point to
  one of the agent's tools.

And one that is easy to skip and expensive to skip: a machine with no getty is
a brick the moment the agent stops coming up, so
[break-glass](docs/architecture.md#break-glass) is part of the design.

Full write-up: [docs/architecture.md](docs/architecture.md).

## License

MIT
