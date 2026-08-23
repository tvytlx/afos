#!/usr/bin/env bash
# T1 provisioning -- install afos into a live systemd machine.
#
# T1 exists for the questions a container cannot answer and a full boot answers
# too slowly: does afosd come up under systemd, does the console unit really
# conflict getty off its tty, does OnFailure land in the rescue target. It does
# not test boot itself -- that is T2's only job.
#
# AFOS_SRC is the repo path. OrbStack mirrors the macOS filesystem into the
# machine at the same path, so the host's $PWD is valid in here unchanged.
set -euo pipefail

: "${AFOS_SRC:?AFOS_SRC must point at the afos checkout}"
[[ -d "$AFOS_SRC/agent" ]] || { echo "afos/t1: no agent/ under $AFOS_SRC" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo "afos/t1: installing runtime"
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-pip ca-certificates

echo "afos/t1: installing agent from $AFOS_SRC"
PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install --quiet --no-cache-dir -e "$AFOS_SRC/agent"
groupadd -f afos

echo "afos/t1: installing units"
install -m 0644 "$AFOS_SRC/init/afosd.service"           /etc/systemd/system/
install -m 0644 "$AFOS_SRC/init/afos-console@.service"   /etc/systemd/system/
install -m 0644 "$AFOS_SRC/init/afos-rescue.target"      /etc/systemd/system/
install -m 0644 "$AFOS_SRC/init/afos-breakglass.service" /etc/systemd/system/
install -d -m 0755 /etc/systemd/logind.conf.d
install -m 0644 "$AFOS_SRC/init/logind.conf.d-afos.conf" /etc/systemd/logind.conf.d/afos.conf

systemctl daemon-reload
systemctl enable --now afosd.service

# Assert rather than print-and-hope: T1's whole value is catching a unit that
# silently did not come up.
sleep 1
systemctl is-active --quiet afosd.service \
  || { echo "afos/t1: afosd is not active"; journalctl -u afosd -n 30 --no-pager; exit 1; }
[[ -S /run/afos/afosd.sock ]] \
  || { echo "afos/t1: afosd is active but never bound its socket" >&2; exit 1; }

echo "afos/t1: afosd up, socket bound"
echo "afos/t1: attach with  orb -m afos-t1 -u root afos-console"
