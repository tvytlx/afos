#!/usr/bin/env bash
# T2 -- boot afos for real and watch the console.
#
# This is the only tier that can answer the question the project actually turns
# on: after a real boot, is the agent what you get, and is there nothing else?
# Serial console only -- there is no display to attach to, by construction.
set -euo pipefail

BUILD="${AFOS_BUILD_DIR:-build}"
MEM="${AFOS_MEM:-2048}"
CPUS="${AFOS_CPUS:-2}"
DISK="${AFOS_DISK:-16G}"
ARCH="$(cat "$BUILD/.arch" 2>/dev/null || echo unknown)"
BASE="$(ls "$BUILD"/ubuntu-*-server-cloudimg-*.img 2>/dev/null | head -1 || true)"
OVERLAY="$BUILD/afos.qcow2"
SEED="$BUILD/seed.iso"

[[ -n "$BASE" ]] || { echo "afos: no base image -- run 'make base'" >&2; exit 1; }
[[ -f "$SEED" ]] || { echo "afos: no seed.iso -- run 'make seed'" >&2; exit 1; }

need() { command -v "$1" >/dev/null || { echo "afos: missing $1 (brew install qemu)" >&2; exit 1; }; }

# A qcow2 overlay, so every boot starts from a pristine base and `make reset`
# is a single unlink rather than a re-download.
if [[ ! -f "$OVERLAY" ]]; then
    need qemu-img
    echo "afos: creating overlay on $(basename "$BASE")"
    qemu-img create -q -f qcow2 -F qcow2 -b "$(cd "$(dirname "$BASE")" && pwd)/$(basename "$BASE")" "$OVERLAY" "$DISK"
fi

common=(
    -m "$MEM" -smp "$CPUS"
    -drive "if=virtio,format=qcow2,file=$OVERLAY"
    -drive "if=virtio,format=raw,file=$SEED,readonly=on"
    -netdev "user,id=net0,hostfwd=tcp::2222-:22"
    -device virtio-net-pci,netdev=net0
    -nographic          # no display: there is nothing to display
    -serial mon:stdio   # Ctrl-A X to quit, Ctrl-A C for the qemu monitor
)

case "$ARCH" in
    arm64)
        need qemu-system-aarch64
        FW="${AFOS_FIRMWARE:-$(brew --prefix qemu 2>/dev/null)/share/qemu/edk2-aarch64-code.fd}"
        [[ -f "$FW" ]] || { echo "afos: UEFI firmware not found at $FW" >&2; exit 1; }
        exec qemu-system-aarch64 \
            -machine virt,accel=hvf,highmem=on -cpu host \
            -drive "if=pflash,format=raw,readonly=on,file=$FW" \
            "${common[@]}"
        ;;
    amd64)
        need qemu-system-x86_64
        accel=tcg; [[ "$(uname -m)" == "x86_64" ]] && accel=hvf
        exec qemu-system-x86_64 -machine "q35,accel=$accel" -cpu max "${common[@]}"
        ;;
    *)
        echo "afos: unknown arch '$ARCH' -- run 'make base' first" >&2; exit 1 ;;
esac
