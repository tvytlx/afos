#!/usr/bin/env bash
# T0 acceptance -- runs INSIDE the dev container.
#
# What T0 can prove: the agent works, the protocol holds, the daemon+frontend
# pair is wired correctly. What it cannot prove -- and must not be read as
# proving -- is anything about booting. A container has no boot, no tty1 and no
# display manager to remove.
set -uo pipefail
source "$(dirname "$0")/lib-accept.sh"

SOCK="${AFOS_SOCKET:-/run/afos/afosd.sock}"
SRC="${AFOS_SRC:-/opt/afos/agent}"

echo "afos T0 acceptance -- agent logic and protocol"

section "the daemon comes up"
ok "afosd is running"                     pgrep -f afosd
ok "the socket is bound at $SOCK"         test -S "$SOCK"
ok "the socket is 0660, not world-writable" \
   bash -c "[[ \$(stat -c '%a' '$SOCK') == 660 ]]"

section "the protocol holds"
# Exported so the `bash -c` checks below can call it.
converse() { printf '%s\n:quit\n' "$1" | afos-console --no-color --socket "$AFOS_SOCK" 2>/dev/null; }
export -f converse
export AFOS_SOCK="$SOCK"
ok "hello is answered with a welcome banner" \
   bash -c "converse ':who' | grep -q 'session s'"
ok "an unknown builtin is refused, not ignored" \
   bash -c "converse ':nope' | grep -q \"unknown builtin\""

section "shell is a working capability"
ok "a command runs and its output reaches the frontend" \
   bash -c "converse ':exec echo capability-ok' | grep -q capability-ok"
ok "a successful command reports exit 0" \
   bash -c "converse ':exec true' | grep -q 'exit 0'"
ok "a failing command reports its real exit code" \
   bash -c "converse ':exec exit 42' | grep -q 'exit 42'"
ok "output survives an immediate :quit (no truncation)" \
   bash -c "converse ':exec sleep 0.5; echo drained' | grep -q drained"

section "the userspace afosd actually ships in"
ok "python3 is present"                   python3 --version
ok "/bin/sh exists -- the shell is demoted, not deleted" test -x /bin/sh
no "no display manager in this userspace" bash -c "command -v gdm3 lightdm sddm"

section "the full suite"
ok "unit + protocol tests pass" \
   python3 -m unittest discover -s "$SRC/tests" -q

verdict
