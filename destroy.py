#!/usr/bin/env python3
"""
Destroy VMs and networks defined in an infrastructure YAML.

Usage:
  ./destroy.py <infra.yaml>                  # destroy all VMs + networks
  ./destroy.py --only k8s-cp-01 <infra.yaml> # destroy specific VMs only
  ./destroy.py --vms-only <infra.yaml>       # destroy VMs, keep networks
  ./destroy.py --dry-run <infra.yaml>        # preview only
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

CIDATA_DIR = "/var/lib/libvirt/cloud-init"
LOG_FMT = "%(asctime)s [%(levelname)-5s] %(message)s"
log = logging.getLogger("destroy")


def run(cmd, check=False, capture=True, dry_run=False):
    if dry_run:
        log.info("[DRY-RUN] %s", " ".join(cmd))
        return None
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def domain_exists(name):
    r = subprocess.run(["virsh", "dominfo", name], capture_output=True, text=True)
    return r.returncode == 0


def net_exists(name):
    r = subprocess.run(["virsh", "net-info", name], capture_output=True, text=True)
    return r.returncode == 0


def destroy_vm(name, disk_pool, dry_run=False):
    if not domain_exists(name):
        log.info("VM '%s' does not exist, skipping.", name)
        return

    log.info("Destroying VM '%s'", name)
    # Force off if running
    run(["virsh", "destroy", name], dry_run=dry_run)
    # Remove with storage
    run(["virsh", "undefine", name, "--remove-all-storage", "--nvram"], dry_run=dry_run)

    # Clean up cidata directory
    ci_dir = Path(CIDATA_DIR) / name
    if ci_dir.exists() and not dry_run:
        shutil.rmtree(ci_dir)
        log.info("Removed cidata: %s", ci_dir)
    elif ci_dir.exists():
        log.info("[DRY-RUN] Would remove %s", ci_dir)


def destroy_network(name, dry_run=False):
    if not net_exists(name):
        log.info("Network '%s' does not exist, skipping.", name)
        return

    log.info("Destroying network '%s'", name)
    run(["virsh", "net-destroy", name], dry_run=dry_run)
    run(["virsh", "net-undefine", name], dry_run=dry_run)


def main():
    p = argparse.ArgumentParser(description="Destroy libvirt VMs and networks.")
    p.add_argument("config", help="Infrastructure YAML")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--only", type=str, default="", help="Specific VMs to destroy")
    p.add_argument("--vms-only", action="store_true", help="Skip network destruction")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FMT,
    )

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    defaults = cfg.get("defaults", {})
    disk_pool = defaults.get("disk_pool", "/var/lib/libvirt/images")
    vms = cfg.get("vms", [])
    networks = cfg.get("networks", [])

    if args.only:
        only_set = {n.strip() for n in args.only.split(",")}
        vms = [v for v in vms if v["name"] in only_set]

    # Destroy VMs first (reverse order)
    log.info("=== Destroying %d VMs ===", len(vms))
    for vm in reversed(vms):
        destroy_vm(vm["name"], disk_pool, dry_run=args.dry_run)

    # Then networks
    if not args.vms_only:
        log.info("=== Destroying %d networks ===", len(networks))
        for net in reversed(networks):
            # Don't destroy pre-existing bridge networks
            if net.get("type") == "bridge":
                log.info("Skipping bridge network '%s' (externally managed)", net["name"])
                continue
            destroy_network(net["name"], dry_run=args.dry_run)

    log.info("=== Teardown complete ===")


if __name__ == "__main__":
    main()