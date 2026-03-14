# libvirt-provision

Declarative VM provisioning on bare-metal KVM/libvirt with cloud-init. Define your infrastructure in a single YAML file — networks, VMs, cloud-init configuration — and run one command to bring it up.

## Architecture

```
infra.yaml          ← You define networks + VMs here
    │
    ├─ validate.py  ← Checks YAML structure, IP ranges, references
    ├─ provision.py ← Creates networks → disks → cloud-init ISOs → VMs
    ├─ destroy.py   ← Tears down VMs and networks
    └─ status.py    ← Shows current state of defined infra
```

The provisioning flow for each VM:

1. Create a QCOW2 disk backed by a cloud image (copy-on-write, space-efficient)
2. Generate cloud-init `user-data`, `meta-data`, and `network-config`
3. Pack them into a NoCloud ISO (`cidata.iso`)
4. Run `virt-install --import` with the disk and ISO attached
5. VM boots, cloud-init configures hostname, users, SSH keys, networking, packages

## Prerequisites

Ubuntu 24.04 L0 host (bare metal). Install dependencies:

```bash
sudo make deps
```

Or manually:

```bash
sudo apt install -y \
  qemu-kvm qemu-utils \
  libvirt-daemon-system libvirt-clients \
  virtinst bridge-utils \
  cloud-image-utils genisoimage \
  python3 python3-yaml whois
```

Download a base cloud image:

```bash
sudo make download-image
```

## Secrets Handling

**No secrets are stored in the YAML config or in the repository.**

Secrets are injected via environment variables at runtime:

| Variable | Required | Description |
|---|---|---|
| `VM_SSH_PUBKEY` | Yes | SSH public key injected into all VMs |
| `VM_DEFAULT_PASSWORD` | No | Password hash for console fallback login |

Setup:

```bash
# Copy the example and fill in your values
cp secrets.env.example secrets.env
# Edit secrets.env with your SSH key

# Source before running
source secrets.env
```

Generate a password hash (if you want console access):

```bash
mkpasswd -m sha-512
```

## Usage

```bash
# Source secrets
source secrets.env

# Validate your config
make validate CONFIG=examples/infra.yaml

# Preview what will happen
make dry-run CONFIG=examples/infra.yaml

# Provision everything
make provision CONFIG=examples/infra.yaml

# Check status
make status CONFIG=examples/infra.yaml

# Tear down
make destroy CONFIG=examples/infra.yaml
```

### Selective provisioning

```bash
# Only specific VMs
./provision.py --only k8s-cp-01,k8s-cp-02 examples/infra.yaml

# Skip network creation (if they already exist)
./provision.py --skip-networks examples/infra.yaml

# Destroy only VMs, keep networks
./destroy.py --vms-only examples/infra.yaml
```

## YAML Configuration Schema

### `defaults`

Global defaults inherited by all VMs (overridable per-VM):

```yaml
defaults:
  os_variant: ubuntu24.04
  base_image: /var/lib/libvirt/images/base/ubuntu-24.04-server-cloudimg-amd64.img
  vcpus: 2
  memory_mb: 2048
  disk_gb: 20
  disk_pool: /var/lib/libvirt/images
  user: ops
  timezone: UTC
  locale: en_US.UTF-8
  packages:
    - qemu-guest-agent
  runcmd: []
```

### `networks`

Each entry creates a libvirt virtual network:

```yaml
networks:
  - name: mgmt            # unique name
    type: nat              # nat | route | isolated | bridge
    bridge: virbr-mgmt     # bridge device name
    subnet: 10.10.0.0/24
    gateway: 10.10.0.1
    dhcp:
      start: 10.10.0.100
      end: 10.10.0.199
    dns:
      domain: mgmt.lab
      forwarders:
        - 1.1.1.1

  - name: external
    type: bridge           # uses pre-existing host bridge
    bridge: br0
```

| Type | Behavior |
|---|---|
| `nat` | NAT forwarding to host's default route |
| `route` | Routed (no NAT, requires upstream static routes) |
| `isolated` | No external connectivity, inter-VM only |
| `bridge` | Attach to a pre-existing host bridge (e.g., `br0`) |

### `vms`

```yaml
vms:
  - name: k8s-cp-01
    vcpus: 4               # overrides defaults.vcpus
    memory_mb: 8192
    disk_gb: 50
    networks:
      - name: mgmt         # references networks[].name
        ip: 10.10.0.10     # static IP (must be in subnet)
        mac: "52:54:00:a1:00:10"  # optional fixed MAC
      - name: workload
        ip: 10.20.0.10
    cloud_init:
      packages:             # merged with defaults.packages
        - apt-transport-https
      runcmd:               # appended to defaults.runcmd
        - "swapoff -a"
```

Each entry in `networks` attaches a NIC in order (`ens3`, `ens4`, ...). Static IPs produce netplan-style cloud-init network config. If no `ip` is given, DHCP is used.

## Project Layout

```
.
├── provision.py          # Main provisioning script
├── destroy.py            # Teardown script
├── validate.py           # Config validation
├── status.py             # Infrastructure status
├── Makefile              # Convenience targets
├── secrets.env.example   # Template for secrets (never commit secrets.env)
├── .gitignore
├── examples/
│   └── infra.yaml        # Full example config (K8s cluster)
└── README.md
```

## Runtime Artifacts (on host, not in repo)

| Path | Contents |
|---|---|
| `/var/lib/libvirt/images/base/` | Downloaded cloud images |
| `/var/lib/libvirt/images/<vm>.qcow2` | VM disks (COW backed by base) |
| `/var/lib/libvirt/cloud-init/<vm>/` | Generated cloud-init files + ISO |

## Idempotency

Both `provision.py` and `destroy.py` are idempotent:

- `provision.py` skips networks and VMs that already exist
- `destroy.py` skips resources that don't exist

This means you can safely re-run after a partial failure.

## Verification

After provisioning:

```bash
# List all VMs
virsh list --all

# Check a specific VM's console
virsh console k8s-cp-01
# (Ctrl+] to exit)

# SSH into a VM (once cloud-init finishes)
ssh ops@10.10.0.10

# Check cloud-init status from inside the VM
cloud-init status --long
```