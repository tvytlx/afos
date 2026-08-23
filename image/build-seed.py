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
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
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
    "@AFOS_UPDATE@": "image/afos-update",
}

INDENT = " " * 6  # depth of `content: |` blocks in user-data.tmpl


def declared_dependencies() -> list[str]:
    """Dependencies from pyproject, without importing a TOML parser.

    Deliberately crude -- this reads one field of one file we control, and
    pulling in tomllib logic here would be more machinery than the job needs.
    """
    text = (ROOT / "agent/pyproject.toml").read_text()
    if "dependencies" not in text:
        return []
    body = text.split("dependencies", 1)[1].split("[", 1)[1].split("]", 1)[0]
    return [d.strip().strip('"').strip("'") for d in body.split(",") if d.strip()]


PLATFORM_TAGS = {
    "arm64": ["manylinux_2_17_aarch64", "manylinux2014_aarch64"],
    "amd64": ["manylinux_2_17_x86_64", "manylinux2014_x86_64"],
}
TARGET_PYTHON = "3.12"  # what Ubuntu 24.04 ships


def vendor_wheels(build: Path, deps: list[str], arch: str) -> Path:
    """Download wheels for the TARGET platform and unpack them into a lib dir.

    The image has no package manager, on purpose: `pip` would drag setuptools
    and its tree onto a system whose whole premise is subtraction. So resolution
    happens here, on a machine that has a network and a pip, and the machine
    receives a directory that is already importable.

    Wheels are downloaded for Linux and the image's Python, not for whatever
    this laptop happens to be -- a macOS wheel unpacked onto the image would
    import on the build host and fail on the machine, which is the failure this
    whole mechanism exists to prevent.
    """
    lib = build / "lib-stage"
    if lib.exists():
        shutil.rmtree(lib)
    lib.mkdir(parents=True)

    if deps:
        wheels = build / "wheels"
        wheels.mkdir(exist_ok=True)
        cmd = [
            sys.executable, "-m", "pip", "download",
            "--only-binary=:all:",          # never build an sdist for a foreign platform
            "--python-version", TARGET_PYTHON,
            "--implementation", "cp",
            "--dest", str(wheels),
        ]
        for tag in PLATFORM_TAGS[arch]:
            cmd += ["--platform", tag]
        result = subprocess.run(cmd + deps, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                "build-seed: could not vendor dependencies for "
                f"linux/{arch} python{TARGET_PYTHON}.\n"
                "            A package with no wheel for that platform cannot be "
                "installed on a machine with no compiler and no pip.\n"
                f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
            )
        for wheel in sorted(wheels.glob("*.whl")):
            with zipfile.ZipFile(wheel) as zf:
                zf.extractall(lib)

    shutil.copytree(ROOT / "agent/afos", lib / "afos",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = "\n".join(sorted(p.name for p in (build / "wheels").glob("*.whl"))) \
        if deps else "(no third-party dependencies)"
    (lib / "VENDORED").write_text(manifest + "\n")
    return lib


def agent_tarball_b64(lib: Path) -> str:
    """The importable lib directory, gzipped, as base64.

    Small enough to ride inside the seed ISO, which keeps the machine off the
    network while it provisions.
    """
    buf = io.BytesIO()
    # gzip, not tarfile's "w:gz": gzip stamps the wall clock into its header, so
    # the "reproducible" comment on _strip_noise was only ever half true -- the
    # tar members were normalised and every build still produced a different
    # blob, a different user-data and a different ISO.
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for child in sorted(lib.iterdir()):
                tar.add(child, arcname=child.name, filter=_strip_noise)
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


def render(build: Path, arch: str) -> str:
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
    lib = vendor_wheels(build, declared_dependencies(), arch)
    text = text.replace("@AGENT_TARBALL_B64@", agent_tarball_b64(lib))
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

    arch = (build / ".arch").read_text().strip() if (build / ".arch").exists() else "arm64"
    (seed_dir / "user-data").write_text(render(build, arch))
    (seed_dir / "meta-data").write_text("instance-id: afos-dev\nlocal-hostname: afos\n")

    iso = build / "seed.iso"
    make_iso(seed_dir, iso)
    print(f"afos: seed -> {iso} ({iso.stat().st_size // 1024} KiB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
