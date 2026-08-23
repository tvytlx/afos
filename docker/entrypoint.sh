#!/bin/sh
# Start the daemon, then attach a console to it -- the same two-process shape
# systemd gives you in T1/T2, so the container is not rehearsing a different
# architecture than the one that ships.
set -eu

: "${AFOS_SOCKET:=/run/afos/afosd.sock}"
mkdir -p "$(dirname "$AFOS_SOCKET")"

afosd --socket "$AFOS_SOCKET" &
daemon=$!
trap 'kill "$daemon" 2>/dev/null || true' EXIT INT TERM

# Wait for the socket rather than sleeping a guessed interval.
i=0
while [ ! -S "$AFOS_SOCKET" ]; do
    i=$((i + 1))
    [ "$i" -gt 100 ] && { echo "afos-dev: afosd never bound $AFOS_SOCKET" >&2; exit 1; }
    sleep 0.05
done

if [ "$#" -gt 0 ]; then
    "$@"
else
    afos-console --socket "$AFOS_SOCKET"
fi
