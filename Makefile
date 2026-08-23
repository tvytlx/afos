# afos -- three tiers of dev environment.
#
#   T0  make dev            container, seconds       agent logic          <- 95% of the time
#   T1  make machine        OrbStack VM, ~30s        units, boot order, rescue
#   T2  make boot           QEMU, minutes            the real boot        <- the gate
#
# The tiers are a ladder, not alternatives. Debugging agent logic in T2 is the
# main way to waste a day on this project.

SHELL      := /bin/bash
BUILD      ?= build
IMAGE      ?= afos:dev
COMPOSE    ?= docker compose -f docker/compose.yml
MACHINE    ?= afos-t1

.DEFAULT_GOAL := help
.PHONY: help test lint dev build shell down machine machine-rm base seed image boot ssh reset clean

help:  ## show this
	@grep -hE '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

# -- T0: container ----------------------------------------------------------

test:  ## run the test suite on the host (no container needed)
	cd agent && python3 -m unittest discover -s tests -v

build:  ## build the dev container image
	$(COMPOSE) build

dev: build  ## T0: daemon + console in a container (the inner loop)
	$(COMPOSE) run --rm afos

shell: build  ## T0: a plain shell in the dev container, for poking at userspace
	$(COMPOSE) run --rm --entrypoint /bin/bash afos

ctest: build  ## run the test suite inside the container
	$(COMPOSE) run --rm --entrypoint python3 afos -m unittest discover -s /opt/afos/agent/tests -v

down:  ## tear down containers
	$(COMPOSE) down --remove-orphans

# -- T1: OrbStack Linux machine (real systemd, no boot) ---------------------

machine:  ## T1: provision an OrbStack VM with the units installed
	@command -v orb >/dev/null || { echo "afos: OrbStack CLI 'orb' not found"; exit 1; }
	@orb list 2>/dev/null | grep -q '^$(MACHINE)\b' || orb create ubuntu:24.04 $(MACHINE)
	orb -m $(MACHINE) -u root env AFOS_SRC="$$PWD" bash -s < scripts/provision-t1.sh

machine-rm:  ## T1: destroy the VM
	-orb delete $(MACHINE) --force

# -- T2: real boot in QEMU --------------------------------------------------

base:  ## T2: download the Ubuntu Server cloud image
	AFOS_BUILD_DIR=$(BUILD) image/fetch-base.sh

seed:  ## T2: render the cloud-init seed (units + agent source)
	AFOS_BUILD_DIR=$(BUILD) image/build-seed.py

image: base seed  ## T2: everything needed to boot

boot: image  ## T2: boot afos on the serial console (Ctrl-A X to quit)
	AFOS_BUILD_DIR=$(BUILD) image/boot.sh

ssh:  ## T2: ssh into the running VM (port 2222)
	ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@localhost

reset:  ## T2: discard the VM disk, keep the download
	rm -f $(BUILD)/afos.qcow2

clean:  ## remove all build artifacts including the download
	rm -rf $(BUILD)
