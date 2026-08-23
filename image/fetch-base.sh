#!/usr/bin/env bash
# Download the Ubuntu Server cloud image afos subtracts from.
#
# The cloud image, not the ISO installer: it already boots unattended, already
# has no GUI, and is the same artifact a cloud provider would hand you. The
# only thing afos has to remove from it is the ways in.
set -euo pipefail

RELEASE="${AFOS_RELEASE:-24.04}"
BUILD="${AFOS_BUILD_DIR:-build}"

case "$(uname -m)" in
    arm64 | aarch64) ARCH=arm64 ;;
    x86_64 | amd64)  ARCH=amd64 ;;
    *) echo "afos: unsupported host arch $(uname -m)" >&2; exit 1 ;;
esac

IMG="ubuntu-${RELEASE}-server-cloudimg-${ARCH}.img"
URL="https://cloud-images.ubuntu.com/releases/${RELEASE}/release/${IMG}"

mkdir -p "$BUILD"
if [[ -f "$BUILD/$IMG" ]]; then
    echo "afos: $BUILD/$IMG already present"
else
    echo "afos: fetching $URL"
    curl -fL --progress-bar -o "$BUILD/$IMG.part" "$URL"
    mv "$BUILD/$IMG.part" "$BUILD/$IMG"
fi

# Never boot the pristine download: a QEMU run writes to its disk, and
# re-provisioning has to start from a clean base every time.
echo "afos: base image -> $BUILD/$IMG"
echo "$ARCH" > "$BUILD/.arch"
