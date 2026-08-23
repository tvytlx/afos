# afos architecture

## The thesis

Reuse everything Linux is good at — kernel, drivers, network stack, init,
cgroups, journald — and replace exactly one thing: the interactive surface.

A stock Ubuntu box offers a human many ways to a prompt: a display manager, a
getty on every VT, a login shell over ssh, a serial console. afos removes all
of them and puts a single agent daemon in their place.

## Two things this is *not*

**Not a new kernel.** afos does not fork, patch, or replace anything below
userspace. The claim is about the interface, not the machine.

**Not agent-as-PID-1.** systemd stays PID 1. That is not a compromise — process
supervision, restart policy, cgroup accounting, socket activation and ordered
dependencies are exactly the "underlying capability" the project set out to
reuse. Writing a PID 1 would mean reimplementing zombie reaping and signal
forwarding, and would make every agent crash a kernel panic.

```
        Linux kernel · drivers · net stack      unchanged
                        │
                     systemd  (PID 1)           unchanged
                        │
        ┌───────────────┼────────────────┐
     afosd        afos-console@tty1   (everything else Ubuntu runs)
   the daemon      the frontend
```

## Sessions live in the daemon

The one structural decision everything else follows from.

A frontend — the console on tty1, an ssh client, an HTTP caller — holds no
state. It opens a Unix socket, says `hello`, and renders frames. The session,
its history and its running turn all live in `afosd`.

That inversion buys three things a shell cannot offer:

- **Frontends are disposable.** A console dying does not end a conversation;
  reattaching resumes it. There is no `tmux` here because there is nothing to
  reattach *to* except the daemon.
- **Several frontends, one conversation.** Attach over ssh and watch what is
  happening on tty1, live, in the same stream.
- **The transport is testable.** The socket is newline-delimited JSON, so a
  test — or `socat` — is a first-class frontend.

## Shell is a capability, not an entry point

`/bin/sh` is not deleted. The agent needs to run commands; that is the job.
What is deleted is the shell as a *human* entry: no login, no getty on a normal
boot, no autospawned VTs.

So `afos.exec` runs commands the way any other tool would be called — scoped,
streamed, timed out, in its own process group so a timeout can actually kill
the whole tree.

## Nothing the agent produces may be able to kill it

`afosd` runs for the life of the machine and is the only way into it, so "small
leak" and "eventually a brick" are the same sentence. Three limits, all of them
found by flooding a running daemon rather than by reading the code:

- **The outbox is bounded in bytes, not frames.** A frame cap alone does not
  close the hole it looks like it closes: frames are capped at 2 MB, so 4096 of
  them is still 8 GB. `:exec cat /var/log/syslog` on a box with long JSON lines
  gets there in seconds.
- **`state` frames are never evicted, and gaps are announced in band.** busy/idle
  is what draws the prompt, and it sits at the head of exactly the flood that
  would evict it — leaving a console that finished working but never said so.
  And reporting dropped output only to journald reports it only to a place you
  need this console to read.
- **`MemoryMax=512M` on the unit.** A bounded cgroup failure with a journal
  entry beats the kernel picking a victim, because on this machine the OOM
  killer and a brick are the same outcome.

Sessions are reaped when no frontend can reach them, and a frame larger than the
stated limit is refused rather than allowed to raise `ValueError` out of
`readline()` and kill the connection.

## Input is queued

Turns run one at a time, in order, from a per-session queue. The tempting
alternative — reject input while busy — reads fine on a tty where a human waits
for the prompt, and loses turns everywhere else: a piped script, or two
frontends typing at once.

## What the agent spawns dies with the agent

Commands run through `afos.exec` live in afosd's cgroup, and `KillMode=mixed`
takes the whole cgroup down when the unit restarts. So a task the agent
backgrounds does not survive an agent restart.

This surfaced while writing the T2 acceptance run: a kill-storm loop the agent
had backgrounded died with the first kill it landed, because the restart took
its own parent's cgroup with it. The test now launches it as a transient unit
with `systemd-run --collect`.

Treat it as the rule rather than a quirk: work that must outlive the agent has
to be its own unit. Nothing is orphaned by accident, which is the behaviour you
want on a machine where the agent is the only supervisor a human talks to.

## Nothing on the path to the console may give up

There is exactly one way for a human to reach this machine. Every unit between
boot and that console — `afosd`, `afos-console@` — must therefore keep trying
forever, and must not be gated on anything that can be unavailable.

Two concrete rules follow, and both were learned the expensive way:

- **`Wants=`, never `Requires=`, along that path.** `afos-console` originally
  required `afosd`. A single failed `afosd` start job failed the console's
  dependency, systemd never retries a dependency failure, and `afosd` itself
  recovered seconds later — leaving a machine with a healthy agent, no console,
  and no getty. From the second boot onward it was unreachable except by
  editing the kernel command line. `StartLimitIntervalSec=0` on the console
  makes the same point about restart limits: a console that gives up is a brick.
- **No network in the dependency chain.** `afosd` is not ordered after
  `network-online.target`. It binds a Unix socket and needs no network to serve
  a console, and gating the only interactive entry on DHCP is what dragged it
  into the failing early-boot job to begin with. A brain that needs the network
  deals with its absence at call time.
- **The terminal reflexes must be inert.** Ctrl-Z on the console would suspend
  it, and systemd cannot see a stopped process: the unit still reports `active`,
  `afosd` is healthy so break-glass stays shut by design, and the machine is
  deaf until someone power-cycles it. `SIGTSTP`, `SIGTTIN`, `SIGTTOU` and
  `SIGQUIT` are ignored — there is nothing to suspend *to*. A getty could afford
  to be stopped; this cannot.
- **Nothing may leave the console's file descriptor non-blocking.** systemd
  hands a tty unit the same open file description for stdin and stdout, so
  `loop.connect_read_pipe()` on stdin made `stdout` non-blocking too — and the
  first burst of output large enough to fill the tty buffer raised
  `BlockingIOError` and exited the console. Stdin is read on a thread instead.

The escalation logic behaved perfectly during that failure and made it worse:
`afosd` was healthy, so break-glass correctly stayed shut. Correct components
composed into an unreachable machine — which is the argument for testing the
property end to end, on the boot that matters, rather than testing the parts.

## Break-glass

A machine with no getty is a brick the moment the agent stops coming up, so the
way back in is part of the design rather than a rescue afterthought.

| trigger | mechanism |
|---|---|
| at boot | append `systemd.unit=afos-rescue.target` to the kernel cmdline |
| at runtime | `afosd` exceeds `StartLimitBurst=5`; `afos-escalate@` isolates the rescue target |

Both land in `afos-breakglass.service`, an autologin root agetty on the serial
console. It is never `WantedBy` a normal target, so a healthy boot never starts
it — and both paths are visible in the journal.

Using systemd's own `systemd.unit=` rather than a custom kernel-cmdline
condition is deliberate: it is the mechanism every Linux admin already knows,
and it costs nothing to adopt.

### Escalation is conditional, and that took a helper unit

`OnFailure=` fires on *every* failure, not only when the restart limit is hit.
Wired straight to the rescue target it produced the opposite of the intended
behaviour: one transient crash isolated rescue, whose `Conflicts=` then
cancelled the restart systemd had already scheduled — so a single SIGKILL took
the agent down for good and opened a root shell.

`afos-escalate@.service` holds the condition systemd has no syntax for. Two
details in it are not obvious and are both load-bearing:

- It reads **`ActiveState`, not `Result`.** When the start limit is hit systemd
  leaves `Result` at the original cause (`signal`, `exit-code`); only the state
  separates `activating (auto-restart)` from `failed`.
- It sets **`StartLimitIntervalSec=0`.** It is triggered once per failure, so a
  restart-looping daemon triggers it in a burst — with a rate limit of its own
  it gets refused for "start request repeated too quickly" exactly when it is
  needed.

## Wire protocol

Newline-delimited JSON. Human-readable on purpose — while the console frontend
is itself under construction, being able to drive the daemon by hand is worth
more than a compact encoding.

| frontend → daemon | daemon → frontend |
|---|---|
| `hello` `{frontend, session}` | `welcome` `{version, session, name}` |
| `input` `{text}` | `output` `{stream: agent\|exec\|system\|error, text}` |
| `interrupt` | `state` `{status: idle\|busy}` |
| `bye` | `error` `{text}` |

`bye` is graceful: the daemon drains accepted work before closing, so a script
that submits a turn and quits still sees the output.

## Who is allowed in, honestly

`/run/afos/afosd.sock` is `0660 root:afos`. An earlier version of this document
said that meant a frontend "needs group membership, not root — which is what
makes an ssh `ForceCommand` frontend possible without handing out a root shell."

**That was false, and worth stating plainly because it invited the mistake.**
`afosd` runs as root with no `User=`, no `NoNewPrivileges=`, and `:exec` runs
`/bin/sh -c` on an arbitrary string with no allow-list. So reaching the socket
at all *is* root command execution: `:exec cat /etc/shadow` works. Adding an
unprivileged operator to group `afos` gives them root while the sentence above
told you it had not.

Today nothing is exposed by it — the image creates no users and group `afos` is
empty — but the claim is what a reader would have built on.

What exists now instead:

- **The peer's identity comes from the kernel.** `hello`'s `frontend` field is a
  string the client picks; it names a frontend the way a user-agent header names
  a browser. `SO_PEERCRED` gives the real uid/gid/pid, and that is what gets
  logged and displayed (`console:ttyAMA0[root] attached`).
- **There is one place an authorization decision goes.** `identity.Policy`
  admits everyone in v0, because the socket mode is the whole boundary and a
  second check against the same fact would be theatre. It exists so the decision
  has a home before there is more than one frontend to make it about.

## ssh is a frontend, not a way past the agent

On any real cloud the default configuration made the project's central claim
false: cloud-init writes the provider's key into `/root/.ssh/authorized_keys`
and `ssh.socket` is active, so port 22 was a root shell that never touched
`afosd`. Every T2 check still passed, because T2 only ever looked at the serial
console.

`ForceCommand /usr/local/bin/afos-console --wait` in `sshd_config` — chosen over
a `command=` option in `authorized_keys` precisely because it applies to keys
nobody in this project put there. Around it, the channels `ForceCommand` does
not cover are closed: sftp, TCP/agent/socket forwarding, X11, tunnels,
`PermitUserRC`.

One counter-intuitive detail, because it costs an afternoon to rediscover:
`PermitRootLogin forced-commands-only` does **not** mean "only the forced
command". It means "only if the *key* carries a `command=` option" and ignores
`ForceCommand` entirely; sshd's privsep monitor then kills the session with
`fatal: monitor_child_preauth: unexpected authentication`. The correct setting
is `prohibit-password`, with `ForceCommand` doing the constraining.

The drop-in is `00-afos.conf`, not `60-`: sshd takes the *first* value it
obtains, and cloud-init writes `50-cloud-init.conf`.

T2 proves this by logging in — a plain session, a session that asks for
`/bin/bash -i`, and an sftp attempt — rather than by reading the config back.
Reading the config back proves the file was written, which is not the property.

## Audit

afos removes every audit surface Linux had — no login, no `sudo`, no shell
history — so without something in their place the machine has no record that it
ever did anything.

Every `:exec` writes a line to `afos.audit` (stderr, which systemd routes to
journald): the command, the session, the peer's kernel-supplied identity, the
exit code and the duration, with a separate record if it was cancelled. It is
deliberately not the `python3-systemd` binding, because adding a dependency is a
decision this project has not made yet — see below.

## Open question: how does code reach an afos machine?

The current answer, arrived at by accident rather than decision: a base64
tarball of pure-Python source inside a seed ISO, with no package manager
involved. T0 and T1 `pip install`; T2 untars. That divergence is invisible until
the first dependency is added, at which point T0 and T1 stay green and T2 boots
into break-glass.

`image/build-seed.py` now refuses to build a seed whose `pyproject.toml`
declares dependencies, so the trap is a build error with a name on it. That is a
guard, not an answer. The answer is a choice — vendor a wheelhouse into the seed
and `pip install --no-index`, or build a real rootfs — and it should be made
before wiring up a model, because a model SDK is exactly the dependency that
springs it.

There is also no way to update the agent on a running machine. Today that is
`make reset`.

## Where the model goes
## Where the model goes

`afos/brain.py`. v0 ships a builtin-only brain on purpose: the daemon, the
frontends, the units and the boot path all had to be built and booted before a
model was wired up, and none of them should have to change when one is.
