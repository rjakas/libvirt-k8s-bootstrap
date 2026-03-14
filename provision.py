#!/usr/bin/env python3
"""
libvirt-provision: Declarative VM provisioning with cloud-init on bare-metal KVM.

Reads an infrastructure YAML definition and creates libvirt networks and VMs
with cloud-init NoCloud datasource configuration.

Secrets are injected via environment variables:
  VM_SSH_PUBKEY        - (required) SSH public key for the default user
  VM_DEFAULT_PASSWORD  - (optional) password hash for console access fallback

Usage:
  ./provision.py [OPTIONS] <infra.yaml>

Examples:
  export VM_SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)"
  ./provision.py infra.yaml
  ./provision.py --dry-run infra.yaml
  ./provision.py --only k8s-cp-01,k8s-cp-02 infra.yaml
  ./provision.py --skip-networks infra.yaml
"""

import argparse
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CIDATA_DIR = "/var/lib/libvirt/cloud-init"
DISK_POOL = "/var/lib/libvirt/images"
LOG_FMT = "%(asctime)s [%(levelname)-5s] %(message)s"

log = logging.getLogger("provision")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], check: bool = True, capture: bool = False,
        dry_run: bool = False) -> subprocess.CompletedProcess | None:
    """Run a subprocess command with logging."""
    pretty = " ".join(cmd)
    if dry_run:
        log.info("[DRY-RUN] %s", pretty)
        return None
    log.debug("exec: %s", pretty)
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True,
    )


def require_commands(names: list[str]) -> None:
    """Verify required CLI tools are on PATH."""
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        log.error("Missing required tools: %s", ", ".join(missing))
        log.error("Install: sudo apt install -y qemu-kvm libvirt-daemon-system "
                   "virtinst cloud-image-utils genisoimage qemu-utils")
        sys.exit(1)


def require_env(name: str, required: bool = True) -> str | None:
    """Read an environment variable, exit if required and missing."""
    val = os.environ.get(name)
    if required and not val:
        log.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return val


def load_config(path: str) -> dict:
    """Load and validate the infrastructure YAML."""
    p = Path(path)
    if not p.is_file():
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(p) as f:
        cfg = yaml.safe_load(f)
    # Basic structural validation
    if not isinstance(cfg, dict):
        log.error("Config root must be a YAML mapping")
        sys.exit(1)
    if "vms" not in cfg or not cfg["vms"]:
        log.error("Config must contain at least one VM in 'vms'")
        sys.exit(1)
    return cfg


def virsh_net_exists(name: str) -> bool:
    """Check if a libvirt network already exists."""
    r = subprocess.run(
        ["virsh", "net-info", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def virsh_domain_exists(name: str) -> bool:
    """Check if a libvirt domain (VM) already exists."""
    r = subprocess.run(
        ["virsh", "dominfo", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def subnet_info(cidr: str) -> tuple[str, str]:
    """Return (network_address, netmask) from CIDR."""
    net = ipaddress.IPv4Network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


def cidr_prefix(cidr: str) -> int:
    """Return prefix length from CIDR string."""
    return ipaddress.IPv4Network(cidr, strict=False).prefixlen


# ---------------------------------------------------------------------------
# Network provisioning
# ---------------------------------------------------------------------------
def build_network_xml(net: dict) -> str:
    """Generate libvirt network XML from a network definition."""
    ntype = net.get("type", "nat")
    name = net["name"]
    bridge = net.get("bridge", f"virbr-{name}")

    # Bridge-passthrough to pre-existing host bridge
    if ntype == "bridge":
        return textwrap.dedent(f"""\
            <network>
              <name>{name}</name>
              <forward mode="bridge"/>
              <bridge name="{bridge}"/>
            </network>""")

    # Build forward element
    if ntype == "nat":
        forward = '  <forward mode="nat"/>'
    elif ntype == "route":
        forward = '  <forward mode="route"/>'
    elif ntype == "isolated":
        forward = ""  # no forward element = isolated
    else:
        log.error("Unknown network type '%s' for network '%s'", ntype, name)
        sys.exit(1)

    _, netmask = subnet_info(net["subnet"])
    gateway = net["gateway"]

    # DHCP block
    dhcp = net.get("dhcp", {})
    dhcp_xml = ""
    if dhcp:
        dhcp_xml = textwrap.dedent(f"""\
            <dhcp>
                <range start="{dhcp['start']}" end="{dhcp['end']}"/>
              </dhcp>""")

    # DNS block
    dns_cfg = net.get("dns", {})
    dns_xml = ""
    if dns_cfg:
        domain = dns_cfg.get("domain", "")
        forwarders = dns_cfg.get("forwarders", [])
        parts = []
        if forwarders:
            for fwd in forwarders:
                parts.append(f'    <forwarder addr="{fwd}"/>')
        dns_xml = "  <dns>\n" + "\n".join(parts) + "\n  </dns>"
        if domain:
            dns_xml += f'\n  <domain name="{domain}" localOnly="yes"/>'

    lines = [f"<network>", f"  <name>{name}</name>"]
    if forward:
        lines.append(forward)
    lines.append(f'  <bridge name="{bridge}" stp="on" delay="0"/>')
    if dns_xml:
        lines.append(dns_xml)
    lines.append(f'  <ip address="{gateway}" netmask="{netmask}">')
    if dhcp_xml:
        lines.append(f"    {dhcp_xml.strip()}")
    lines.append("  </ip>")
    lines.append("</network>")
    return "\n".join(lines)


def provision_networks(networks: list[dict], dry_run: bool = False) -> None:
    """Create all libvirt networks that don't already exist."""
    if not networks:
        log.info("No networks defined, skipping.")
        return

    for net in networks:
        name = net["name"]
        if virsh_net_exists(name):
            log.info("Network '%s' already exists, skipping.", name)
            continue

        xml = build_network_xml(net)
        log.info("Creating network '%s' (type=%s):", name, net.get("type", "nat"))
        log.debug("Network XML:\n%s", xml)

        if dry_run:
            log.info("[DRY-RUN] Would define network:\n%s", xml)
            continue

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", prefix=f"net-{name}-", delete=False
        ) as f:
            f.write(xml)
            tmp_path = f.name

        try:
            run(["virsh", "net-define", tmp_path])
            run(["virsh", "net-start", name])
            run(["virsh", "net-autostart", name])
            log.info("Network '%s' created and started.", name)
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Cloud-init generation
# ---------------------------------------------------------------------------
def generate_meta_data(vm: dict) -> str:
    """Generate cloud-init meta-data YAML."""
    instance_id = str(uuid.uuid4())
    return yaml.dump({
        "instance-id": instance_id,
        "local-hostname": vm["name"],
    }, default_flow_style=False)


def generate_user_data(vm: dict, defaults: dict, ssh_pubkey: str,
                       password_hash: str | None) -> str:
    """Generate cloud-init user-data (#cloud-config)."""
    name = vm["name"]
    ci = vm.get("cloud_init", {})
    user = vm.get("user", defaults.get("user", "ops"))
    timezone = vm.get("timezone", defaults.get("timezone", "UTC"))
    locale = vm.get("locale", defaults.get("locale", "en_US.UTF-8"))

    # Merge packages: defaults + vm-specific
    default_pkgs = list(defaults.get("packages", []))
    vm_pkgs = list(ci.get("packages", []))
    all_pkgs = list(dict.fromkeys(default_pkgs + vm_pkgs))  # dedupe, preserve order

    # Merge runcmd: defaults first, then vm-specific
    default_runcmd = list(defaults.get("runcmd", []))
    vm_runcmd = list(ci.get("runcmd", []))
    all_runcmd = default_runcmd + vm_runcmd

    cloud_config: dict[str, Any] = {
        "hostname": name,
        "fqdn": name,
        "manage_etc_hosts": True,
        "timezone": timezone,
        "locale": locale,
        "users": [
            {
                "name": user,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "groups": "sudo",
                "shell": "/bin/bash",
                "lock_passwd": password_hash is None,
                "ssh_authorized_keys": [ssh_pubkey],
            }
        ],
        "ssh_pwauth": password_hash is not None,
        "package_update": True,
        "package_upgrade": True,
    }

    if password_hash:
        cloud_config["chpasswd"] = {
            "expire": False,
        }
        cloud_config["users"][0]["passwd"] = password_hash

    if all_pkgs:
        cloud_config["packages"] = all_pkgs

    if all_runcmd:
        cloud_config["runcmd"] = all_runcmd

    # Enable and start qemu-guest-agent if installed
    if "qemu-guest-agent" in all_pkgs:
        cloud_config.setdefault("runcmd", [])
        cloud_config["runcmd"].append("systemctl enable --now qemu-guest-agent")

    # Power state: reboot after cloud-init finishes (clean network state)
    cloud_config["power_state"] = {
        "delay": "now",
        "mode": "reboot",
        "message": "cloud-init completed, rebooting",
        "condition": True,
    }

    return "#cloud-config\n" + yaml.dump(
        cloud_config, default_flow_style=False, sort_keys=False,
    )


def generate_network_config(vm: dict, networks_by_name: dict) -> str:
    """Generate cloud-init network-config (v2 / netplan format)."""
    vm_nets = vm.get("networks", [])
    if not vm_nets:
        # Fallback: single DHCP interface
        return yaml.dump({
            "version": 2,
            "ethernets": {
                "ens3": {"dhcp4": True},
            }
        }, default_flow_style=False)

    ethernets = {}
    for idx, vnet in enumerate(vm_nets):
        # Interface naming: ens3, ens4, ens5, ...
        iface_name = f"ens{3 + idx}"
        net_def = networks_by_name.get(vnet["name"], {})
        ip = vnet.get("ip")

        if ip and net_def.get("subnet"):
            prefix = cidr_prefix(net_def["subnet"])
            ethernets[iface_name] = {
                "dhcp4": False,
                "addresses": [f"{ip}/{prefix}"],
            }
            # Add gateway only on the first interface with a static IP
            # and only if the network has NAT/route (not isolated)
            gw = net_def.get("gateway")
            ntype = net_def.get("type", "nat")
            if idx == 0 and gw and ntype in ("nat", "route", "bridge"):
                ethernets[iface_name]["routes"] = [
                    {"to": "default", "via": gw}
                ]
            # DNS from the network definition
            dns = net_def.get("dns", {})
            nameservers = dns.get("forwarders", [])
            search = [dns["domain"]] if dns.get("domain") else []
            if nameservers or search:
                ns_cfg = {}
                if nameservers:
                    ns_cfg["addresses"] = nameservers
                if search:
                    ns_cfg["search"] = search
                ethernets[iface_name]["nameservers"] = ns_cfg
            # MAC address if specified
            mac = vnet.get("mac")
            if mac:
                ethernets[iface_name]["match"] = {"macaddress": mac}
                ethernets[iface_name]["set-name"] = iface_name
        else:
            # DHCP (bridge networks, or no IP specified)
            ethernets[iface_name] = {"dhcp4": True}
            mac = vnet.get("mac")
            if mac:
                ethernets[iface_name]["match"] = {"macaddress": mac}
                ethernets[iface_name]["set-name"] = iface_name

    return yaml.dump({
        "version": 2,
        "ethernets": ethernets,
    }, default_flow_style=False, sort_keys=False)


def create_cidata_iso(vm_name: str, meta_data: str, user_data: str,
                      network_config: str, dry_run: bool = False) -> str:
    """Create a NoCloud ISO with cloud-init data. Returns path to ISO."""
    ci_dir = Path(CIDATA_DIR) / vm_name
    iso_path = str(ci_dir / "cidata.iso")

    if dry_run:
        log.info("[DRY-RUN] Would create cidata ISO at %s", iso_path)
        return iso_path

    ci_dir.mkdir(parents=True, exist_ok=True)

    (ci_dir / "meta-data").write_text(meta_data)
    (ci_dir / "user-data").write_text(user_data)
    (ci_dir / "network-config").write_text(network_config)

    log.debug("meta-data:\n%s", meta_data)
    log.debug("user-data:\n%s", user_data)
    log.debug("network-config:\n%s", network_config)

    # Prefer cloud-localds if available (cleaner), fallback to genisoimage
    if shutil.which("cloud-localds"):
        run([
            "cloud-localds",
            "--network-config", str(ci_dir / "network-config"),
            iso_path,
            str(ci_dir / "user-data"),
            str(ci_dir / "meta-data"),
        ])
    elif shutil.which("genisoimage"):
        run([
            "genisoimage",
            "-output", iso_path,
            "-volid", "cidata",
            "-joliet",
            "-rock",
            str(ci_dir / "meta-data"),
            str(ci_dir / "user-data"),
            str(ci_dir / "network-config"),
        ])
    else:
        log.error("Neither cloud-localds nor genisoimage found.")
        sys.exit(1)

    log.info("Created cidata ISO: %s", iso_path)
    return iso_path


# ---------------------------------------------------------------------------
# VM provisioning
# ---------------------------------------------------------------------------
def create_vm_disk(vm_name: str, base_image: str, disk_gb: int,
                   disk_pool: str, dry_run: bool = False) -> str:
    """Create a QCOW2 disk backed by the base cloud image."""
    disk_path = os.path.join(disk_pool, f"{vm_name}.qcow2")

    if os.path.exists(disk_path):
        log.warning("Disk %s already exists, reusing.", disk_path)
        return disk_path

    if not os.path.exists(base_image):
        log.error("Base image not found: %s", base_image)
        log.error("Download it first, e.g.:")
        log.error("  wget -O %s https://cloud-images.ubuntu.com/noble/current/"
                   "noble-server-cloudimg-amd64.img", base_image)
        sys.exit(1)

    run([
        "qemu-img", "create",
        "-f", "qcow2",
        "-F", "qcow2",
        "-b", base_image,
        disk_path,
        f"{disk_gb}G",
    ], dry_run=dry_run)

    log.info("Created disk: %s (%dG, backing: %s)", disk_path, disk_gb, base_image)
    return disk_path


def provision_vm(vm: dict, defaults: dict, networks_by_name: dict,
                 ssh_pubkey: str, password_hash: str | None,
                 dry_run: bool = False) -> None:
    """Provision a single VM: disk, cloud-init ISO, virt-install."""
    name = vm["name"]

    if virsh_domain_exists(name):
        log.info("VM '%s' already exists, skipping.", name)
        return

    # Resolve values (VM overrides defaults)
    vcpus = vm.get("vcpus", defaults.get("vcpus", 2))
    memory = vm.get("memory_mb", defaults.get("memory_mb", 2048))
    disk_gb = vm.get("disk_gb", defaults.get("disk_gb", 20))
    disk_pool = vm.get("disk_pool", defaults.get("disk_pool", DISK_POOL))
    base_image = vm.get("base_image", defaults.get("base_image", ""))
    os_variant = vm.get("os_variant", defaults.get("os_variant", "ubuntu24.04"))

    # Create disk
    disk_path = create_vm_disk(name, base_image, disk_gb, disk_pool, dry_run)

    # Generate cloud-init
    meta_data = generate_meta_data(vm)
    user_data = generate_user_data(vm, defaults, ssh_pubkey, password_hash)
    network_config = generate_network_config(vm, networks_by_name)
    cidata_iso = create_cidata_iso(name, meta_data, user_data, network_config, dry_run)

    # Build virt-install command
    cmd = [
        "virt-install",
        "--name", name,
        "--vcpus", str(vcpus),
        "--memory", str(memory),
        "--os-variant", os_variant,
        "--disk", f"path={disk_path},format=qcow2,bus=virtio",
        "--disk", f"path={cidata_iso},device=cdrom",
        "--import",
        "--noautoconsole",
        "--graphics", "vnc,listen=0.0.0.0",
        "--serial", "pty",
        "--console", "pty,target_type=serial",
        "--virt-type", "kvm",
        "--cpu", "host-passthrough",
        "--channel", "unix,target_type=virtio,name=org.qemu.guest_agent.0",
    ]

    # Attach network interfaces in order
    vm_nets = vm.get("networks", [])
    if vm_nets:
        for vnet in vm_nets:
            net_name = vnet["name"]
            net_def = networks_by_name.get(net_name, {})
            net_type = net_def.get("type", "nat")

            nic = f"network={net_name},model=virtio"
            mac = vnet.get("mac")
            if mac:
                nic += f",mac={mac}"
            cmd.extend(["--network", nic])
    else:
        cmd.extend(["--network", "network=default,model=virtio"])

    # Execute
    log.info("Provisioning VM '%s' (vcpus=%d, mem=%dMB, disk=%dGB)", name, vcpus, memory, disk_gb)
    run(cmd, dry_run=dry_run)

    if not dry_run:
        log.info("VM '%s' started. Console: virsh console %s", name, name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Declarative libvirt/KVM VM provisioning with cloud-init.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Environment variables:
              VM_SSH_PUBKEY        SSH public key (required)
              VM_DEFAULT_PASSWORD  Password hash for console access (optional)
                                   Generate with: mkpasswd -m sha-512

            Example:
              export VM_SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)"
              ./provision.py examples/infra.yaml
        """),
    )
    p.add_argument("config", help="Path to infrastructure YAML definition")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Print what would be done without making changes")
    p.add_argument("--only", type=str, default="",
                   help="Comma-separated list of VM names to provision (default: all)")
    p.add_argument("--skip-networks", action="store_true",
                   help="Skip network creation (assume networks exist)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FMT,
    )

    # Preflight
    require_commands(["virsh", "virt-install", "qemu-img"])
    ssh_pubkey = require_env("VM_SSH_PUBKEY")
    password_hash = require_env("VM_DEFAULT_PASSWORD", required=False)

    cfg = load_config(args.config)
    defaults = cfg.get("defaults", {})
    networks = cfg.get("networks", [])
    vms = cfg.get("vms", [])

    # Build network lookup
    networks_by_name = {n["name"]: n for n in networks}

    # Filter VMs if --only specified
    only_set = set()
    if args.only:
        only_set = {n.strip() for n in args.only.split(",")}
        vms = [v for v in vms if v["name"] in only_set]
        if not vms:
            log.error("No VMs matched --only filter: %s", args.only)
            sys.exit(1)

    # Phase 1: Networks
    if not args.skip_networks:
        log.info("=== Phase 1: Networks (%d defined) ===", len(networks))
        provision_networks(networks, dry_run=args.dry_run)
    else:
        log.info("=== Phase 1: Networks (skipped) ===")

    # Phase 2: VMs
    log.info("=== Phase 2: Virtual Machines (%d to provision) ===", len(vms))
    for vm in vms:
        provision_vm(vm, defaults, networks_by_name, ssh_pubkey, password_hash,
                     dry_run=args.dry_run)

    # Summary
    log.info("=== Provisioning complete ===")
    if not args.dry_run:
        log.info("Verify with:")
        log.info("  virsh list --all")
        log.info("  virsh net-list --all")
        for vm in vms:
            log.info("  virsh console %s", vm["name"])


if __name__ == "__main__":
    main()