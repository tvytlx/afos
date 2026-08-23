#!/usr/bin/env python3
"""T2 acceptance -- boot afos in QEMU and interrogate it over the serial line.

This is the only tier that can answer the question the project turns on: after
a real boot, is the agent what you get, and is there nothing else?

The harness talks to the machine the same way a person would -- through the
agent console on the serial port. That is deliberate: if these checks can run,
the agent is by definition the working interactive entry, and the transcript
this prints is the evidence.
"""

from __future__ import annotations

import argparse
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

BOOT_TIMEOUT = 900.0   # cloud-init installs packages and purges others
TURN_TIMEOUT = 120.0

# Anything matching this on the serial console is a failure by itself: it means
# something other than the agent is asking a human for something.
LOGIN_PROMPT = re.compile(rb"(?m)^\s*\S+ login:|^Password:", re.IGNORECASE)

# ...except agetty's autologin banner, which reads "afos login: root (automatic
# login)". It contains "login:" but asks for nothing -- it is break-glass
# working as designed, and matching it would fail the very check it satisfies.
AUTOLOGIN = re.compile(rb"(?m)^.*login: \S+ \(automatic login\).*$")
# Not the version: matching it turns the gate into a silent 15-minute hang
# the first time anyone bumps __version__, and the update section bumps it
# on purpose.
BANNER = b" -- session "
VALUE = re.compile(rb"AFOSV<([^>]*)>AFOSEND")
# The console colours its output and wraps at the terminal width, so the
# value comes back with escape sequences and line breaks inside it.
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def asks_for_credentials(data: bytes) -> bool:
    return LOGIN_PROMPT.search(AUTOLOGIN.sub(b"", data)) is not None


class Machine:
    """A booted afos, driven through its own console."""

    def __init__(self, transcript: Path) -> None:
        env = dict(os.environ, AFOS_BUILD_DIR=os.environ.get("AFOS_BUILD_DIR", "build"))
        self.proc = subprocess.Popen(
            [str(ROOT / "image/boot.sh")],
            cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.buf = bytearray()
        self.transcript = transcript.open("wb")

    def expect(self, needle: bytes, timeout: float, echo: bool = False) -> bytes:
        """Read until `needle` appears. Returns everything consumed."""
        deadline = time.monotonic() + timeout
        start = len(self.buf)
        while needle not in bytes(self.buf[start:]):
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {needle!r}")
            if self.proc.poll() is not None:
                raise RuntimeError(f"qemu exited with {self.proc.returncode}")
            for _ in self.sel.select(timeout=1.0):
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial console closed")
                self.buf += chunk
                self.transcript.write(chunk)
                self.transcript.flush()
                if echo:
                    sys.stderr.write(DIM + chunk.decode("utf-8", "replace") + OFF)
                    sys.stderr.flush()
        consumed = bytes(self.buf[start:])
        return consumed

    @staticmethod
    def _sentinel(cmd: str) -> str:
        """Wrap a command so its output comes back exactly, and only its output.

        Matching a substring against everything the console printed made checks
        unfalsifiable: the window always ends in "exit 0", so every check whose
        expected value was "0" passed no matter what the machine answered. Two
        of the checks backing "every other way in is gone" were in that state.

        BOTH markers are split ('AFO''SV', 'AFOSE''ND') because the serial tty
        echoes the line as it is typed. Splitting only the first one still let
        the echo satisfy the *expect* marker, so every read stopped at the echo
        and every check reported "<no value>" -- a harness fault that looks
        exactly like a dead machine.

        The command is wrapped in a brace group so that redirection and the
        pipe apply to all of it: without that, `a || b` bound them to `b` alone
        and compound checks returned two concatenated answers.
        """
        return (
            "printf 'AFO''SV<%s>AFOSE''ND\\n' "
            f""""$({{ {cmd} ; }} 2>/dev/null | tr '\\n' ',')" """
        )

    def value(self, cmd: str) -> str:
        """Exact output of a command, run through the agent console."""
        out = self.ask(f":exec {self._sentinel(cmd)}", "AFOSEND")
        # Consume through the next prompt. Leaving the turn's trailing "exit 0"
        # in the buffer made the *following* check match it and report the
        # previous command's answer -- an off-by-one that reads exactly like a
        # broken machine.
        self.expect(b"afos>", 30.0)
        return self._extract(out)

    def raw_value(self, cmd: str) -> str:
        """Exact output of a command, run through a plain shell.

        Used after break-glass, when there is no agent left to ask.
        """
        return self._extract(self.ask(self._sentinel(cmd), "AFOSEND"))

    def resync(self, tries: int = 20) -> bool:
        """Re-establish the request/response rhythm after afosd restarts.

        An update takes the console's connection down mid-turn: the turn's
        trailing "exit 0" never arrives, the console reconnects and prints a
        fresh banner, and anything waiting for the old prompt waits forever.
        Polling until the console answers again is the honest way back in sync.
        """
        for _ in range(tries):
            try:
                if self.value("echo resync") == "resync":
                    return True
            except (TimeoutError, RuntimeError):
                continue
        return False

    @staticmethod
    def _extract(out: bytes) -> str:
        clean = ANSI.sub(b"", out).replace(b"\r", b"")
        hit = VALUE.search(clean)
        if not hit:
            return "<no value>"
        # tr turned the trailing newline into a separator; line wrapping may
        # have split the value across rows.
        return hit.group(1).decode("utf-8", "replace").replace("\n", "").strip().rstrip(",")

    def ask(self, line: str, marker: str, soft: bool = False) -> bytes:
        """Send a line to the agent and read until its marker comes back.

        soft=True tolerates the marker never arriving -- `systemctl reboot`
        takes the console down mid-turn, which is the expected outcome, not a
        failure.
        """
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.encode() + b"\n")
        self.proc.stdin.flush()
        try:
            return self.expect(marker.encode(), 20.0 if soft else TURN_TIMEOUT)
        except TimeoutError:
            if soft:
                return b""
            raise

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.transcript.close()


class Report:
    def __init__(self) -> None:
        self.passed = self.failed = 0

    def section(self, title: str) -> None:
        print(f"\n{DIM}-- {title} {OFF}")

    def check(self, desc: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  {GREEN}PASS{OFF} {desc}")
        else:
            self.failed += 1
            print(f"  {RED}FAIL{OFF} {desc}")
            if detail:
                print("\n".join("         " + l for l in detail.strip().splitlines()[-8:]))

    def check_eq(self, desc: str, got: str, want: str) -> None:
        """Equality, always. Substring scoring is how three of these checks
        became incapable of failing -- and how one of them scored `"active" in
        "inactive"` as a pass."""
        self.check(desc, got == want, f"expected {want!r}, machine answered {got!r}")

    def verdict(self) -> int:
        print(f"\n  {self.passed} passed, {self.failed} failed\n")
        return 1 if self.failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true", help="stream the serial console")
    args = ap.parse_args()

    build = ROOT / os.environ.get("AFOS_BUILD_DIR", "build")
    build.mkdir(exist_ok=True)
    transcript = build / "t2-serial.log"

    print("afos T2 acceptance -- the real boot")
    print(f"{DIM}  serial transcript -> {transcript}{OFF}")

    r = Report()
    m = Machine(transcript)
    try:
        r.section("it boots, and the agent takes the console")
        boot = m.expect(BANNER, BOOT_TIMEOUT, echo=args.verbose)
        r.check("the machine boots and afos-console owns the serial line", True)
        r.check(
            "no login prompt ever appeared on the console",
            not asks_for_credentials(boot),
            "a login prompt was printed -- something other than the agent is an entry point",
        )
        m.expect(b"afos>", 30.0)

        r.section("the agent is a working entry point, not just a banner")
        out = m.ask(":exec echo t2-alive", "exit 0")
        r.check("a command submitted over serial runs and answers", b"t2-alive" in out, out.decode(errors="replace"))

        r.section("every other way in is gone")
        # Every value here is compared for equality against sentinel-delimited
        # output. serial-getty is instance-named after the console device, which
        # is arch-dependent, so the machine is asked which one it has rather
        # than asserted against the one this laptop happens to boot.
        checks = [
            ("getty@tty1 is masked",
             "systemctl is-enabled getty@tty1.service", "masked"),
            # The instance nobody named. Masking getty@tty1 alone left this one
            # free, and logind autospawns it on Ctrl-Alt-F2.
            ("an unnamed getty instance is masked too",
             "systemctl is-enabled getty@tty7.service", "masked"),
            ("logind autospawns no virtual terminals on THIS boot",
             "systemd-analyze cat-config systemd/logind.conf | grep -c '^NAutoVTs=0'",
             "1"),
            ("getty@tty1 is not merely masked but STOPPED",
             "systemctl is-active getty@tty1.service", "inactive"),
            ("the serial getty is masked",
             "systemctl is-enabled serial-getty@$(awk 'NR==1{print $1}' /proc/consoles).service",
             "masked"),
            ("no getty instance is running on any terminal",
             "systemctl list-units --state=active --no-legend 'getty@*' 'serial-getty@*' | wc -l",
             "0"),
            ("no agetty process is alive anywhere", "pgrep -c agetty || true", "0"),
            ("no user account exists besides root",
             "awk -F: '$3>=1000 && $3<65534 {print $1}' /etc/passwd | wc -l", "0"),
            ("the agent shipped without pip",
             "command -v pip3 >/dev/null && echo present || echo absent", "absent"),
        ]
        for desc, cmd, want in checks:
            r.check_eq(desc, m.value(cmd), want)

        # ssh is the entry point afos claims not to have. Assert the absence of
        # every way in, not just one setting -- password auth off means nothing
        # if a provider key or root login is what actually lets someone in.
        for desc, cmd, want in [
            ("ssh password authentication is off",
             "mkdir -p /run/sshd; /usr/sbin/sshd -T | awk '/^passwordauthentication /{print $2; f=1} "
             "END{if(!f) print \"no-sshd\"}'", "no"),
            ("root has no usable password",
             "passwd -S root | awk '{print $2}'", "L"),
            # ssh is a frontend now, not a bypass. Assert every lever that
            # would turn it back into one -- ForceCommand alone is not a
            # boundary if sftp, tunnels or agent forwarding still work.
            ("ssh forces the agent console and nothing else",
             "mkdir -p /run/sshd; /usr/sbin/sshd -T | awk '/^forcecommand /{print $2}'",
             "/usr/local/bin/afos-console"),
            # sshd -T prints the legacy synonym `without-password` for what
            # sshd_config calls `prohibit-password`; normalise rather than
            # hardcode whichever spelling this version happens to emit.
            ("root ssh login is key-only",
             "mkdir -p /run/sshd; /usr/sbin/sshd -T | awk '/^permitrootlogin /{print $2}'"
             " | sed 's/without-password/prohibit-password/'",
             "prohibit-password"),
            ("ssh cannot become a file transfer",
             "mkdir -p /run/sshd; /usr/sbin/sshd -T | awk '/^subsystem /{print $3}'", "/bin/false"),
            ("ssh cannot become a tunnel",
             "mkdir -p /run/sshd; /usr/sbin/sshd -T | awk '/^allowtcpforwarding /{print $2}'", "no"),
        ]:
            r.check_eq(desc, m.value(cmd), want)

        # Reported, not asserted: afos has not yet decided whether sshd should
        # be present at all. A check that always passes is worse than a note.
        print(f"{DIM}  note  ssh.service/ssh.socket state: "
              f"{m.value('systemctl is-active ssh.service ssh.socket')}{OFF}")

        r.section("break-glass is present but shut")
        for desc, cmd, want in [
            ("break-glass is NOT running on a healthy boot",
             "systemctl is-active afos-breakglass.service", "inactive"),
            ("afos-rescue.target is installed and reachable",
             "systemctl cat afos-rescue.target >/dev/null && echo installed", "installed"),
        ]:
            r.check_eq(desc, m.value(cmd), want)

        r.section("systemd, not the agent, is PID 1")
        for desc, cmd, want in [
            ("PID 1 is systemd", "cat /proc/1/comm", "systemd"),
            ("afosd runs as a supervised unit", "systemctl is-active afosd.service", "active"),
        ]:
            r.check_eq(desc, m.value(cmd), want)

        # Reading sshd's config back proves the file was written. It does not
        # prove that a login lands in the agent rather than a shell, and that
        # distinction is the whole difference between ssh as a frontend and ssh
        # as a bypass -- so actually log in.
        r.section("an ssh login lands in the agent, not a shell")
        key = build / "afos_test_id"
        ssh = [
            "ssh", "-p", "2222", "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "root@localhost",
        ]
        try:
            # No command: a plain interactive login.
            plain = subprocess.run(ssh, input=":who\n:quit\n", capture_output=True,
                                   text=True, timeout=90).stdout
            r.check("a plain ssh login reaches the afos console",
                    "afos" in plain and "session" in plain, plain[-400:])

            # With a command: ForceCommand must win. If this lands in a shell,
            # ssh is a root bypass no matter what the console does.
            forced = subprocess.run(ssh + ["/bin/bash -i"], input=":who\n:quit\n",
                                    capture_output=True, text=True, timeout=90).stdout
            r.check("ssh cannot override the forced command with its own",
                    "afos" in forced and "session" in forced, forced[-400:])

            # sftp is a second channel that ForceCommand does not cover.
            sftp = subprocess.run(
                ["sftp", "-P", "2222", "-i", str(key),
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "BatchMode=yes", "root@localhost"],
                input="ls\nquit\n", capture_output=True, text=True, timeout=90)
            r.check("sftp is refused", sftp.returncode != 0,
                    f"sftp exited {sftp.returncode}: {sftp.stdout[-200:]}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            r.check("an ssh login reaches the afos console", False, str(e))

        # Vendored dependencies are resolved on the build host for the
        # target platform and unpacked into the lib directory. Whether they
        # actually import on the machine is a different claim -- a macOS wheel
        # imports fine on the laptop that built it.
        r.section("the agent and everything vendored with it imports")
        r.check_eq(
            "afos imports from the versioned lib directory",
            m.value("python3 -c \"import sys; sys.path.insert(0,'/opt/afos/lib');"
                    " import afos; print(afos.__version__)\""),
            "0.0.1",
        )
        vendored = m.value("cat /opt/afos/lib/VENDORED | head -1")
        print(f"{DIM}  note  vendored: {vendored}{OFF}")
        # One line, not a heredoc: every line typed at the console is its own
        # turn, so a multi-line command arrives as several unrelated inputs.
        r.check_eq(
            "every vendored package imports on the machine",
            m.value(
                "ok=1; for d in /opt/afos/lib/*/; do n=$(basename $d); "
                "case $n in *.dist-info|*.data) continue;; esac; "
                "PYTHONPATH=/opt/afos/lib python3 -c \"import $n\" 2>/dev/null "
                "|| { ok=0; echo bad:$n; }; done; "
                "[ $ok = 1 ] && echo all-import"
            ),
            "all-import",
        )

        # An OS has to answer "how does new software get here". afos's answer
        # was "it doesn't" -- the code arrived frozen in the seed ISO. These two
        # checks are the answer and its safety net, in that order.
        r.section("the agent can be replaced on a running machine")
        version = ("python3 -c \"import sys; sys.path.insert(0,'/opt/afos/lib');"
                   " import afos; print(afos.__version__)\"")

        m.value("rm -rf /tmp/u && mkdir -p /tmp/u && cp -r /opt/afos/lib/. /tmp/u/")
        m.value("sed -i 's/^__version__.*/__version__ = \"0.0.2\"/' /tmp/u/afos/__init__.py")
        m.value("tar -czf /tmp/good.tgz -C /tmp/u .")
        m.ask(":update /tmp/good.tgz", "update applied", soft=True)
        r.check("the console comes back after the agent replaces itself", m.resync(),
                bytes(m.buf[-800:]).decode("utf-8", "replace"))
        r.check_eq("the running agent is the new version", m.value(version), "0.0.2")
        r.check_eq("afosd is answering on the new version",
                   m.value("systemctl is-active afosd.service"), "active")

        # The failure that matters: a version that installs fine and then does
        # not come up. On a machine with no other way in, "it looked installed"
        # and "the box went dark" are the same event.
        m.value("rm -rf /tmp/b && mkdir -p /tmp/b && cp -r /opt/afos/lib/. /tmp/b/")
        m.value("echo 'raise ImportError(1)' >> /tmp/b/afos/__init__.py")
        m.value("tar -czf /tmp/bad.tgz -C /tmp/b .")
        m.ask(":update /tmp/bad.tgz", "update failed", soft=True)
        m.resync()
        r.check_eq("a version that cannot import leaves the machine untouched",
                   m.value(version), "0.0.2")
        r.check_eq("and the agent is still answering",
                   m.value("systemctl is-active afosd.service"), "active")
        r.check("the refusal is on the record",
                b"does not import" in bytes(m.buf),
                "no refusal message in the transcript")

        # Everything the agent runs shares afosd's cgroup, so a limit meant to
        # bound the daemon silently became the ceiling on the machine's actual
        # work. MemoryHigh=256M throttled rather than failing: a 400MB
        # allocation never returned and was never killed. `apt upgrade` would
        # simply hang, with nothing in any log to say why.
        r.section("the agent's own limits do not strangle the work")
        r.check_eq(
            "no memory throttle on the agent's cgroup",
            m.value("cat /sys/fs/cgroup/system.slice/afosd.service/memory.high"),
            "max",
        )
        r.check_eq(
            "a command may use a few hundred MB without stalling",
            m.value("python3 -c 'a=bytearray(400*1024*1024); print(\"ok\")'"),
            "ok",
        )
        r.check_eq(
            "and nothing was throttled doing it",
            m.value("awk '/^high /{print $2}' "
                    "/sys/fs/cgroup/system.slice/afosd.service/memory.events"),
            "0",
        )

        # No tier could see this one: T0 and T1 drive the console through a
        # pipe, and T2 drove it through qemu's stdio without ever sending the
        # byte a human at a terminal would eventually send. A suspended console
        # is invisible to systemd -- Restart= cannot see a stopped process, the
        # unit still reports active, afosd is healthy so break-glass stays shut,
        # and the machine is deaf until someone power-cycles it.
        r.section("the console survives the keys a human will actually press")
        for name, keys in [("Ctrl-Z", b"\x1a"), ("Ctrl-\\", b"\x1c")]:
            assert m.proc.stdin is not None
            m.proc.stdin.write(keys)
            m.proc.stdin.flush()
            try:
                r.check_eq(
                    f"the console still answers after {name}",
                    m.value("echo alive"),
                    "alive",
                )
            except TimeoutError:
                r.check(f"the console still answers after {name}", False,
                        f"the console stopped responding after {name}")

        # The gap that let a bricking bug ship: every check above runs on the
        # FIRST boot, where cloud-init hand-starts the console at the end of
        # provisioning. That guarantees the property by side effect. A machine
        # that only works the first time it is switched on is not an OS.
        r.section("it still owns the console on the SECOND boot")
        m.ask(":exec systemctl reboot", "afos>", soft=True)
        try:
            second = m.expect(BANNER, BOOT_TIMEOUT, echo=args.verbose)
            r.check("the agent owns the console after a reboot", True)
            r.check(
                "no login prompt appeared on the second boot either",
                not asks_for_credentials(second),
            )
            r.check(
                "no unit on the path to the console failed",
                b"Dependency failed for afos-console" not in second,
                bytes(m.buf[-2000:]).decode("utf-8", "replace"),
            )
            m.expect(b"afos>", 60.0)
            got = "<no value>"
            for _ in range(6):
                try:
                    got = m.value("systemctl is-active afosd.service")
                except TimeoutError:
                    continue
                if got != "<no value>":
                    break
            r.check_eq("afosd is active on the second boot", got, "active")
        except TimeoutError:
            r.check("the agent owns the console after a reboot", False,
                    bytes(m.buf[-2500:]).decode("utf-8", "replace"))

        # Last, and destructive on purpose. T1 cannot prove this -- isolating a
        # target inside an OrbStack machine tears down the channel the checks
        # arrive on. Here the serial console survives it, so the harness can
        # watch the agent go away and a root shell take its place. This is the
        # property the entire break-glass design exists for.
        r.section("exhausting the restart limit surrenders the machine to break-glass")
        m.ask(":exec systemctl reset-failed afosd.service || true", "exit ")
        m.ask(
            ":exec systemd-run --collect --unit=afos-storm /bin/sh -c "
            "'for i in 1 2 3 4 5 6 7 8 9 10; do "
            "systemctl is-active --quiet afosd.service && "
            "systemctl kill -s SIGKILL afosd.service; sleep 1.5; done'",
            "exit 0",
        )
        try:
            # The proof is the root prompt itself, not a systemd log line --
            # unit state changes are not echoed to the console at this point,
            # and waiting for one consumes the very output that proves the
            # handover happened.
            window = m.expect(b"root@afos:~#", 240.0, echo=args.verbose)
            r.check("break-glass handed a root shell to the serial console", True)
            r.check(
                "it was an autologin, not a login prompt",
                not asks_for_credentials(window),
                "a login prompt appeared -- break-glass should not ask for credentials",
            )
            # Name the actual unit and compare for equality. The previous
            # two forms both could not fail: one sliced the window after the
            # root prompt, the other globbed a pattern that matches nothing
            # and then scored `"active" in "inactive"` as a pass.
            r.check_eq(
                "the agent console unit is stopped, not merely quiet",
                m.raw_value(
                    "systemctl is-active "
                    "afos-console@$(awk 'NR==1{print $1}' /proc/consoles).service"
                ),
                "inactive",
            )
        except TimeoutError:
            r.check("break-glass handed a root shell to the serial console", False,
                    bytes(m.buf[-2000:]).decode("utf-8", "replace"))

    except (TimeoutError, RuntimeError) as e:
        r.check(f"boot sequence: {e}", False,
                bytes(m.buf[-1500:]).decode("utf-8", "replace"))
    finally:
        m.close()

    print(f"{DIM}  full serial transcript: {transcript}{OFF}")
    return r.verdict()


if __name__ == "__main__":
    raise SystemExit(main())
