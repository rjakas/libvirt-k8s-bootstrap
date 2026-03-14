#!/usr/bin/env python3
"""
Validate an infrastructure YAML definition without making changes.

Checks:
  - YAML syntax
  - Required fields
  - Network reference consistency (VMs reference only defined networks)
  - IP address validity and subnet membership
  - MAC address format
  - Duplicate names
  - Base image existence (optional, with --check-images)

Usage:
  ./validate.py <infra.yaml>
  ./validate.py --check-images <infra.yaml>
"""

import argparse
import ipaddress
import os
import re
import sys

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required.", file=sys.stderr)
    sys.exit(1)

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
errors = []
warnings = []


def err(msg):
    errors.append(f"  ERROR: {msg}")


def warn(msg):
    warnings.append(f"  WARN:  {msg}")


def validate_network(net, idx):
    name = net.get("name")
    if not name:
        err(f"networks[{idx}]: missing 'name'")
        return

    ntype = net.get("type", "nat")
    valid_types = ("nat", "route", "isolated", "bridge")
    if ntype not in valid_types:
        err(f"network '{name}': type '{ntype}' not in {valid_types}")

    if ntype == "bridge":
        if not net.get("bridge"):
            err(f"network '{name}': type 'bridge' requires 'bridge' field")
        return  # bridge networks don't need subnet/gateway/dhcp

    if not net.get("subnet"):
        err(f"network '{name}': missing 'subnet'")
    else:
        try:
            ipaddress.IPv4Network(net["subnet"], strict=False)
        except ValueError as e:
            err(f"network '{name}': invalid subnet: {e}")

    if not net.get("gateway"):
        err(f"network '{name}': missing 'gateway'")
    else:
        try:
            ipaddress.IPv4Address(net["gateway"])
        except ValueError as e:
            err(f"network '{name}': invalid gateway: {e}")

    dhcp = net.get("dhcp", {})
    if dhcp:
        for field in ("start", "end"):
            if field not in dhcp:
                err(f"network '{name}': dhcp missing '{field}'")
            else:
                try:
                    ipaddress.IPv4Address(dhcp[field])
                except ValueError as e:
                    err(f"network '{name}': dhcp.{field} invalid: {e}")


def validate_vm(vm, idx, net_names, nets_by_name):
    name = vm.get("name")
    if not name:
        err(f"vms[{idx}]: missing 'name'")
        return

    for field in ("vcpus", "memory_mb", "disk_gb"):
        val = vm.get(field)
        if val is not None and (not isinstance(val, int) or val <= 0):
            err(f"vm '{name}': '{field}' must be a positive integer, got {val}")

    vm_nets = vm.get("networks", [])
    for nidx, vnet in enumerate(vm_nets):
        net_name = vnet.get("name")
        if not net_name:
            err(f"vm '{name}': networks[{nidx}] missing 'name'")
            continue

        if net_name not in net_names:
            err(f"vm '{name}': references undefined network '{net_name}'")
            continue

        ip = vnet.get("ip")
        if ip:
            try:
                addr = ipaddress.IPv4Address(ip)
            except ValueError as e:
                err(f"vm '{name}': invalid IP '{ip}': {e}")
                continue

            net_def = nets_by_name.get(net_name, {})
            subnet = net_def.get("subnet")
            if subnet:
                network = ipaddress.IPv4Network(subnet, strict=False)
                if addr not in network:
                    err(f"vm '{name}': IP {ip} not in network '{net_name}' subnet {subnet}")

        mac = vnet.get("mac")
        if mac and not MAC_RE.match(mac):
            err(f"vm '{name}': invalid MAC '{mac}' (expected XX:XX:XX:XX:XX:XX)")


def main():
    p = argparse.ArgumentParser(description="Validate infrastructure YAML")
    p.add_argument("config", help="Infrastructure YAML file")
    p.add_argument("--check-images", action="store_true",
                   help="Verify base images exist on disk")
    args = p.parse_args()

    try:
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"YAML parse error: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"File not found: {args.config}")
        sys.exit(1)

    if not isinstance(cfg, dict):
        print("ERROR: Root must be a YAML mapping")
        sys.exit(1)

    defaults = cfg.get("defaults", {})
    networks = cfg.get("networks", [])
    vms = cfg.get("vms", [])

    # Check for duplicate network names
    net_names_list = [n.get("name") for n in networks if n.get("name")]
    dupes = set(x for x in net_names_list if net_names_list.count(x) > 1)
    for d in dupes:
        err(f"Duplicate network name: '{d}'")

    # Check for duplicate VM names
    vm_names_list = [v.get("name") for v in vms if v.get("name")]
    dupes = set(x for x in vm_names_list if vm_names_list.count(x) > 1)
    for d in dupes:
        err(f"Duplicate VM name: '{d}'")

    net_names = set(net_names_list)
    nets_by_name = {n["name"]: n for n in networks if n.get("name")}

    for idx, net in enumerate(networks):
        validate_network(net, idx)

    for idx, vm in enumerate(vms):
        validate_vm(vm, idx, net_names, nets_by_name)

    # Optional: check base images
    if args.check_images:
        base = defaults.get("base_image", "")
        if base and not os.path.exists(base):
            warn(f"Default base image not found: {base}")
        for vm in vms:
            img = vm.get("base_image", base)
            if img and not os.path.exists(img):
                warn(f"VM '{vm.get('name', '?')}': base image not found: {img}")

    # Report
    if not vms:
        err("No VMs defined in 'vms'")

    print(f"\nValidation of: {args.config}")
    print(f"  Networks: {len(networks)}  |  VMs: {len(vms)}")
    print()

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(w)
        print()

    if errors:
        print("Errors:")
        for e in errors:
            print(e)
        print(f"\nFAILED: {len(errors)} error(s)")
        sys.exit(1)
    else:
        print("PASSED: No errors found.")
        sys.exit(0)


if __name__ == "__main__":
    main()