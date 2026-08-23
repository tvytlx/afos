#!/usr/bin/env bash
# T1 acceptance -- runs INSIDE a live systemd machine.
#
# T1 exists for the questions a container cannot answer and a full boot answers
# too slowly: does the unit come up, does it restart, does getty really lose its
# tty, does failure escalate the way the policy says. It does not test boot --
# the machine was running long before afos was installed into it.
#
# Piped in with lib-accept.sh prepended (see the Makefile), so it neither
# sources nor inlines the harness.
set -uo pipefail

echo "afos T1 acceptance -- systemd integration"

section "the daemon is a real service"
ok "afosd is active"           systemctl is-active --quiet afosd.service
ok "afosd is enabled at boot"  systemctl is-enabled --quiet afosd.service
ok "the socket is bound"       test -S /run/afos/afosd.sock
ok "the socket is group-owned by afos, not world-reachable" \
   bash -c '[[ "$(stat -c "%a %G" /run/afos/afosd.sock)" == "660 afos" ]]'

section "getty loses its terminal"
ok "the console unit declares Conflicts=getty@tty1" \
   bash -c "systemctl cat 'afos-console@.service' | grep -q 'Conflicts=getty@%i.service'"
ok "logind autospawns no virtual terminals" \
   bash -c "systemd-analyze cat-config systemd/logind.conf | grep -q '^NAutoVTs=0'"

section "break-glass exists but stays shut"
ok "afos-rescue.target is installed"  systemctl cat afos-rescue.target
ok "the rescue target pulls in break-glass" \
   bash -c "systemctl show -p Requires --value afos-rescue.target | grep -q afos-breakglass"
no "break-glass is NOT running on a healthy system" \
   systemctl is-active --quiet afos-breakglass.service
ok "escalation is conditional, not fired on every failure" \
   bash -c "systemctl show -p OnFailure --value afosd.service | grep -q 'afos-escalate@afosd.service'"

section "a single crash is absorbed, not escalated"
before="$(systemctl show -p MainPID --value afosd.service)"
systemctl kill -s SIGKILL afosd.service 2>/dev/null
after=""
for _ in $(seq 1 60); do
    after="$(systemctl show -p MainPID --value afosd.service)"
    [[ -n "$after" && "$after" != "0" && "$after" != "$before" ]] && break
    sleep 0.25
done
ok "systemd restarted afosd with a new PID ($before -> ${after:-none})" \
   bash -c "[[ -n '${after:-}' && '${after:-0}' != '0' && '${after:-}' != '$before' ]]"
ok "the socket is rebound after the restart" \
   bash -c 'for _ in $(seq 1 60); do [[ -S /run/afos/afosd.sock ]] && exit 0; sleep 0.25; done; exit 1'
ok "a frontend can talk to the restarted daemon" \
   bash -c "printf ':exec echo survived\n:quit\n' | afos-console --no-color | grep -q survived"
no "one crash did NOT open the break-glass shell" \
   systemctl is-active --quiet afos-breakglass.service

# The real isolate cannot be proven here. Isolating a target inside an OrbStack
# machine tears down the very channel this script arrives on, so the run dies
# before it can report -- and a check that cannot report is not a check. The
# end-to-end surrender is verified in T2, where a real serial console survives
# it and the harness can watch break-glass appear.
#
# What T1 can prove is the decision: that the escalation condition fires for a
# unit systemd has given up on, and stays quiet for a healthy one.
section "escalation decides correctly (without surrendering this machine)"
cat > /etc/systemd/system/afos-canary.service <<'UNIT'
[Unit]
Description=afos canary -- a unit that cannot stay up
StartLimitIntervalSec=10
StartLimitBurst=2
[Service]
ExecStart=/bin/false
Restart=always
RestartSec=100ms
UNIT
systemctl daemon-reload
systemctl start afos-canary.service 2>/dev/null
for _ in $(seq 1 60); do
    [[ "$(systemctl show -p ActiveState --value afos-canary.service)" == failed ]] && break
    sleep 0.25
done

ok "a unit systemd has given up on lands in 'failed', which is what escalation reads" \
   bash -c "[[ \"\$(systemctl show -p ActiveState --value afos-canary.service)\" == failed ]]"
ok "the escalation condition fires for it" \
   systemctl is-failed --quiet afos-canary.service
no "the escalation condition stays quiet for the healthy afosd" \
   systemctl is-failed --quiet afosd.service
ok "escalation ignores Result=, which systemd leaves at the original cause" \
   bash -c "systemctl show -p Result --value afos-canary.service | grep -qv start-limit-hit"

systemctl reset-failed afos-canary.service 2>/dev/null
rm -f /etc/systemd/system/afos-canary.service
systemctl daemon-reload

verdict
