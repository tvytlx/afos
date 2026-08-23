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
}

INDENT = " " * 6  # depth of `content: |` blocks in user-data.tmpl


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


def render() -> str:
    text = (ROOT / "image/user-data.tmpl").read_text()
    for token, rel in UNITS.items():
        body = (ROOT / rel).read_text().rstrip("\n")
        block = "\n".join(INDENT + line if line else "" for line in body.split("\n"))
        text = text.replace(token, block)
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

    build = ROOT / args.build_dir
    seed_dir = build / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)

    (seed_dir / "user-data").write_text(render())
    (seed_dir / "meta-data").write_text("instance-id: afos-dev\nlocal-hostname: afos\n")

    iso = build / "seed.iso"
    make_iso(seed_dir, iso)
    print(f"afos: seed -> {iso} ({iso.stat().st_size // 1024} KiB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
