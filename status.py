#!/usr/bin/env python3
"""
Show status of all VMs and networks defined in an infrastructure YAML.

Usage:
  ./status.py <infra.yaml>
"""

import subprocess
import sys

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required.", file=sys.stderr)
    sys.exit(1)


def virsh(args):
    r = subprocess.run(["virsh"] + args, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def get_domain_ip(name):
    """Try to get IP via qemu-guest-agent or DHCP leases."""
    out, rc = virsh(["domifaddr", name, "--source", "agent"])
    if rc != 0:
        out, rc = virsh(["domifaddr", name, "--source", "lease"])
    if rc == 0 and out:
        for line in out.splitlines()[2:]:  # skip header
            parts = line.split()
            if len(parts) >= 4 and parts[3] != "N/A":
                return parts[3].split("/")[0]
    return "-"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <infra.yaml>")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f)

    networks = cfg.get("networks", [])
    vms = cfg.get("vms", [])

    # Networks
    print("=" * 70)
    print(f"{'NETWORK':<20} {'TYPE':<10} {'STATE':<10} {'BRIDGE':<15}")
    print("-" * 70)
    for net in networks:
        name = net["name"]
        ntype = net.get("type", "nat")
        bridge = net.get("bridge", f"virbr-{name}")
        out, rc = virsh(["net-info", name])
        state = "missing"
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("Active:"):
                    state = line.split(":")[1].strip()
        print(f"{name:<20} {ntype:<10} {state:<10} {bridge:<15}")

    # VMs
    print()
    print("=" * 70)
    print(f"{'VM':<20} {'STATE':<12} {'vCPU':<6} {'MEM(MB)':<10} {'IP':<16}")
    print("-" * 70)
    for vm in vms:
        name = vm["name"]
        out, rc = virsh(["domstate", name])
        state = out if rc == 0 else "undefined"
        vcpus = vm.get("vcpus", "-")
        mem = vm.get("memory_mb", "-")
        ip = get_domain_ip(name) if state == "running" else "-"
        print(f"{name:<20} {state:<12} {str(vcpus):<6} {str(mem):<10} {ip:<16}")

    print("=" * 70)


if __name__ == "__main__":
    main()