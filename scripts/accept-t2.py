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
        boot = m.expect(b"afos 0.0.1 -- session", BOOT_TIMEOUT, echo=args.verbose)
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
        # serial-getty is instance-named after the console device, which is
        # arch-dependent -- so ask the machine which one it has rather than
        # asserting against the one this laptop happens to boot.
        checks = [
            ("getty@tty1 is masked", "systemctl is-enabled getty@tty1.service", b"masked"),
            ("the serial getty is masked",
             "systemctl is-enabled serial-getty@$(awk 'NR==1{print $1}' /proc/consoles).service",
             b"masked"),
            ("no getty instance is running on any terminal",
             "systemctl list-units --state=active --no-legend 'getty@*' 'serial-getty@*' | wc -l",
             b"0"),
            ("no user account exists besides root",
             "awk -F: '$3>=1000 && $3<65534 {print $1}' /etc/passwd | wc -l", b"0"),
            ("ssh password auth is off",
             "sshd -T 2>/dev/null | grep -c '^passwordauthentication no' || echo 0", b"1"),
            ("the agent shipped without pip or setuptools",
             "command -v pip3 >/dev/null && echo present || echo absent", b"absent"),
        ]
        for desc, cmd, want in checks:
            out = m.ask(f":exec {cmd}", "exit ")
            r.check(desc, want in out, out.decode(errors="replace"))

        r.section("break-glass is present but shut")
        out = m.ask(":exec systemctl is-active afos-breakglass.service || true", "exit ")
        r.check("break-glass is NOT running on a healthy boot", b"inactive" in out, out.decode(errors="replace"))
        out = m.ask(":exec systemctl cat afos-rescue.target >/dev/null && echo installed", "exit 0")
        r.check("afos-rescue.target is installed and reachable", b"installed" in out, out.decode(errors="replace"))

        r.section("systemd, not the agent, is PID 1")
        out = m.ask(":exec cat /proc/1/comm", "exit 0")
        r.check("PID 1 is systemd", b"systemd" in out, out.decode(errors="replace"))
        out = m.ask(":exec systemctl is-active afosd.service", "exit 0")
        r.check("afosd runs as a supervised unit", b"active" in out, out.decode(errors="replace"))

        # The gap that let a bricking bug ship: every check above runs on the
        # FIRST boot, where cloud-init hand-starts the console at the end of
        # provisioning. That guarantees the property by side effect. A machine
        # that only works the first time it is switched on is not an OS.
        r.section("it still owns the console on the SECOND boot")
        m.ask(":exec systemctl reboot", "afos>", soft=True)
        try:
            second = m.expect(b"afos 0.0.1 -- session", BOOT_TIMEOUT, echo=args.verbose)
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
            out = m.ask(":exec systemctl is-active afosd.service", "exit 0")
            r.check("afosd is active on the second boot", b"active" in out,
                    out.decode(errors="replace"))
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
            r.check(
                "the agent console gave up the line",
                b"afos>" not in window.split(b"root@afos")[-1],
                "the agent is still prompting after the machine surrendered",
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
