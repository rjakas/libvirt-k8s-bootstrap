# =============================================================================
# Makefile — libvirt-provision convenience targets
# =============================================================================
SHELL  := /bin/bash
CONFIG ?= examples/infra.yaml

.PHONY: help validate provision destroy status dry-run deps download-image

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

deps: ## Install host dependencies (run with sudo)
	apt update
	apt install -y \
		qemu-kvm qemu-utils \
		libvirt-daemon-system libvirt-clients \
		virtinst bridge-utils \
		cloud-image-utils genisoimage \
		python3 python3-yaml \
		whois  # provides mkpasswd

download-image: ## Download Ubuntu 24.04 cloud image to default location
	@mkdir -p /var/lib/libvirt/images/base
	wget -nc -O /var/lib/libvirt/images/base/ubuntu-24.04-server-cloudimg-amd64.img \
		https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

validate: ## Validate config syntax and references
	python3 validate.py $(CONFIG)

validate-full: ## Validate including base image existence
	python3 validate.py --check-images $(CONFIG)

dry-run: ## Preview provisioning without changes (requires: source secrets.env first)
	@if [ -z "$$VM_SSH_PUBKEY" ]; then \
		echo "ERROR: VM_SSH_PUBKEY not set. Run: source secrets.env"; \
		exit 1; \
	fi
	python3 provision.py --dry-run $(CONFIG)

provision: validate ## Provision all networks and VMs (requires: source secrets.env first)
	@if [ -z "$$VM_SSH_PUBKEY" ]; then \
		echo "ERROR: VM_SSH_PUBKEY not set. Run: source secrets.env"; \
		exit 1; \
	fi
	python3 provision.py $(CONFIG)

destroy: ## Destroy all VMs and networks
	python3 destroy.py $(CONFIG)

destroy-vms: ## Destroy VMs only, keep networks
	python3 destroy.py --vms-only $(CONFIG)

status: ## Show status of defined infrastructure
	python3 status.py $(CONFIG)