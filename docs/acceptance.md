# Acceptance

Acceptance is executable. Each tier is a `make` target that prints PASS/FAIL per
criterion and keeps going after a failure — a run that stops at the first
problem tells you less than one that shows all of them.

```bash
make accept-t0     # ~40s   13 checks
make accept-t1     # ~3min  18 checks   (creates an OrbStack machine)
make accept-t2     # ~6min  20 checks   (downloads 618MB once, then boots QEMU twice)
make accept        # all three, fast to slow
```

Last full run: **T0 13/13 · T1 18/18 · T2 20/20.**

## What each tier can and cannot prove

The tiers are not three flavours of the same test. Each one can prove things
the tier below is structurally blind to, and none of them is redundant.

| | environment | proves | **cannot** prove |
|---|---|---|---|
| **T0** | container | agent logic, wire protocol, shell capability | anything about boot, tty, or init |
| **T1** | live systemd, no boot | units, restart policy, escalation decision | boot; and not the isolate itself (see below) |
| **T2** | real boot in QEMU | the agent owns the console and nothing else does, on the first boot *and* after a reboot | — this is the gate |

## T0 — `make accept-t0`

Runs inside the dev container.

- **the daemon comes up** — afosd running, socket bound, socket is `0660` and
  not world-writable
- **the protocol holds** — `hello` is answered with a welcome; an unknown
  builtin is refused rather than silently ignored
- **shell is a working capability** — a command runs and its output reaches the
  frontend; exit 0 and a real non-zero code are both reported; output survives
  an immediate `:quit` without truncation
- **the userspace afosd ships in** — python3 present, `/bin/sh` present (the
  shell is demoted, not deleted), no display manager
- **the full suite** — 9 unit and protocol tests

A container has no boot, no tty1 and no display manager to remove. Passing T0
says the agent works; it says nothing about whether afos boots.

## T1 — `make accept-t1`

Runs inside a live OrbStack machine with real systemd.

- **the daemon is a real service** — active, enabled, socket bound and
  group-owned by `afos`
- **getty loses its terminal** — the console unit declares
  `Conflicts=getty@tty1`; logind autospawns no VTs
- **break-glass exists but stays shut** — the rescue target is installed and
  pulls in break-glass; break-glass is *not* running on a healthy system;
  escalation is conditional rather than fired on every failure
- **a single crash is absorbed** — SIGKILL the daemon, systemd restarts it with
  a new PID, the socket is rebound, a frontend can talk to it again, and no
  root shell was opened
- **escalation decides correctly** — against a disposable canary unit that
  cannot stay up: the condition fires for a unit systemd has given up on and
  stays quiet for the healthy daemon

### One check deliberately does not live here

Isolating a target inside an OrbStack machine tears down the channel the T1
checks arrive on, so the run dies before it can report — and a check that
cannot report is not a check. T1 therefore verifies the escalation *decision*;
the end-to-end surrender is verified in T2, where a real serial console
survives it.

## T2 — `make accept-t2`

Boots afos in QEMU and interrogates it **through the agent console on the
serial line**. That is deliberate: if these checks can run at all, the agent is
by definition the working interactive entry, and the serial transcript
(`build/t2-serial.log`) is the evidence.

- **it boots and the agent takes the console** — the afos banner appears on the
  serial line, and no login prompt ever did
- **the agent is a working entry point, not just a banner** — a command
  submitted over serial runs and answers
- **every other way in is gone** — `getty@tty1` masked, the serial getty masked,
  no getty instance running on any terminal, no user account besides root, ssh
  password auth off, and no pip or setuptools shipped
- **break-glass is present but shut** — not running on a healthy boot; the
  rescue target is installed and reachable
- **systemd, not the agent, is PID 1** — and afosd runs as a supervised unit
- **it still owns the console on the second boot** — reboot, then re-assert the
  banner, no login prompt, no unit on the path to the console failed, afosd
  active. This section exists because its absence hid a bug that left the
  machine with no interactive entry at all from its second boot onward: every
  other check runs on the first boot, where cloud-init hand-starts the console
  as the last step of provisioning and so guarantees the property by side
  effect. A machine that only works the first time it is switched on is not
  an OS.
- **exhausting the restart limit surrenders the machine** — a kill storm drives
  afosd past its restart limit; break-glass hands a root shell to the serial
  console, by autologin rather than a credential prompt, and the agent console
  gives up the line

## Verifying by hand

If you would rather look than run a script:

```bash
make dev          # T0: type :help, then :exec uname -a
make boot         # T2: watch it boot; you should land at `afos>`, never `login:`
```

In the booted machine, the two things worth checking yourself:

```bash
:exec systemctl is-enabled getty@tty1.service     # masked
:exec cat /proc/1/comm                            # systemd
```

Ctrl-A X quits QEMU.
