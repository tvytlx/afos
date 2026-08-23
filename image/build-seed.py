#!/usr/bin/env python3
"""Render the cloud-init NoCloud seed that turns the base image into afos.

Templating a YAML file with sed is how you get silent indentation bugs, so the
unit files get embedded here where the indentation is computed rather than
typed. Runs on a stock macOS python3 -- no dependencies, because needing a
Linux box to build the seed would defeat the point of the T2 loop.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UNITS = {
    "@AFOSD_SERVICE@": "init/afosd.service",
    "@AFOS_CONSOLE_SERVICE@": "init/afos-console@.service",
    "@AFOS_RESCUE_TARGET@": "init/afos-rescue.target",
    "@AFOS_BREAKGLASS_SERVICE@": "init/afos-breakglass.service",
    "@AFOS_ESCALATE_SERVICE@": "init/afos-escalate@.service",
    "@LOGIND_CONF@": "init/logind.conf.d-afos.conf",
    "@SSHD_CONF@": "image/sshd_afos.conf",
}

INDENT = " " * 6  # depth of `content: |` blocks in user-data.tmpl


def check_no_dependencies() -> None:
    """Refuse to build a seed that the seed cannot install.

    T0 and T1 install the agent with pip; T2 untars pure-Python source with no
    package manager involved. That divergence is invisible until the first
    dependency is added -- and then T0 and T1 stay green while T2 boots into
    break-glass, with cloud-init still announcing success.

    Failing here makes the divergence a build error with a name on it, instead
    of a boot-time mystery. When afos genuinely needs a dependency, the answer
    is a decision (vendor a wheelhouse into the seed, or build a real rootfs),
    not a quieter check.
    """
    text = (ROOT / "agent/pyproject.toml").read_text()
    body = text.split("dependencies", 1)
    if len(body) < 2:
        return
    declared = body[1].split("]", 1)[0]
    if any(ch.isalnum() for ch in declared.split("[", 1)[-1]):
        raise SystemExit(
            "build-seed: agent/pyproject.toml declares dependencies, but the T2 "
            "image installs the agent by untarring source with no package "
            "manager.\n"
            "            Decide how code reaches an afos machine before adding "
            "one: vendor a wheelhouse into the seed and pip install --no-index, "
            "or build the rootfs properly.\n"
            f"            declared: {declared.strip()}"
        )


def agent_tarball_b64() -> str:
    """The agent source, as a gzipped tar, as base64 -- small enough to ride
    along inside the seed ISO, which keeps the VM off the network at boot."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in ("agent/pyproject.toml", "agent/afos"):
            src = ROOT / path
            tar.add(src, arcname=str(Path(path).relative_to("agent")),
                    filter=_strip_noise)
    return base64.b64encode(buf.getvalue()).decode()


def _strip_noise(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name == "__pycache__" or name.endswith(".pyc"):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0  # reproducible: the same source produces the same seed
    return info


def test_keypair(build: Path) -> str:
    """An ed25519 key so T2 can actually ssh in and see where it lands.

    Test-only, generated once per build directory and never leaving it. Without
    a key the ssh frontend can only be checked by reading sshd's config back,
    which proves the file was written -- not that a login lands in the agent
    rather than a shell. That distinction is the entire point of the frontend.
    """
    key = build / "afos_test_id"
    if not key.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "afos-t2", "-f", str(key)],
            check=True, capture_output=True,
        )
    return key.with_suffix(".pub").read_text().strip()


def render(build: Path) -> str:
    text = (ROOT / "image/user-data.tmpl").read_text()
    text = text.replace("@TEST_SSH_KEY@", test_keypair(build))
    for token, rel in UNITS.items():
        body = (ROOT / rel).read_text().rstrip("\n")
        block = "\n".join(INDENT + line if line else "" for line in body.split("\n"))
        text = text.replace(token, block)
    keep = (ROOT / "image/packages.keep").read_text().rstrip("\n")
    text = text.replace(
        "@PACKAGES_KEEP@",
        "\n".join(INDENT + line if line else "" for line in keep.split("\n")),
    )
    purge = (ROOT / "image/packages.purge").read_text().rstrip("\n")
    text = text.replace(
        "@PACKAGES_PURGE@",
        "\n".join(INDENT + line if line else "" for line in purge.split("\n")),
    )
    text = text.replace("@AGENT_TARBALL_B64@", agent_tarball_b64())
    leftover = [t for t in UNITS if t in text] + (["@AGENT_TARBALL_B64@"] if "@AGENT_TARBALL_B64@" in text else [])
    if leftover:
        raise SystemExit(f"build-seed: unsubstituted tokens {leftover}")
    return text


def make_iso(seed_dir: Path, out: Path) -> None:
    """cloud-init's NoCloud datasource finds this by the CIDATA volume label."""
    out.unlink(missing_ok=True)
    if shutil.which("xorriso"):
        cmd = ["xorriso", "-as", "mkisofs", "-quiet", "-output", str(out),
               "-volid", "CIDATA", "-joliet", "-rock", str(seed_dir)]
    elif shutil.which("hdiutil"):  # macOS, no brew packages required
        cmd = ["hdiutil", "makehybrid", "-quiet", "-o", str(out), "-iso", "-joliet",
               "-default-volume-name", "CIDATA", str(seed_dir)]
    else:
        raise SystemExit("build-seed: need xorriso (brew install xorriso) or hdiutil")
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-dir", default=os.environ.get("AFOS_BUILD_DIR", "build"))
    args = ap.parse_args()

    check_no_dependencies()
    build = ROOT / args.build_dir
    seed_dir = build / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    (seed_dir / "user-data").write_text(render(build))
    (seed_dir / "meta-data").write_text("instance-id: afos-dev\nlocal-hostname: afos\n")

    iso = build / "seed.iso"
    make_iso(seed_dir, iso)
    print(f"afos: seed -> {iso} ({iso.stat().st_size // 1024} KiB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
