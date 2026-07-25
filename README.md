# Materialize on k3s/k3d

A deployment tool for running [Materialize](https://materialize.com/) self-managed on local k3s/k3d Kubernetes clusters.

## Overview

`mzk3` is a Python CLI that handles the complete lifecycle of a Materialize deployment:

- **Create Cluster** - Create a new k3d cluster for Materialize
- **Install** - Deploy Materialize with all required dependencies
- **Upgrade** - Upgrade to a new Materialize version with controlled rollouts
- **Reset** - Destroy and recreate the k3d cluster
- **Status** - Check the health of your deployment
- **List Versions** - Show available Materialize versions

## Prerequisites

- [Python](https://www.python.org/) 3.11+
- [k3d](https://k3d.io/) - k3s in Docker (for local development)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) v3+
- [Docker](https://docs.docker.com/get-docker/)

### Quick k3d Setup

```bash
# Install k3d (macOS)
brew install k3d
```

### Install the CLI

```bash
# Install as a tool (recommended; requires uv - https://docs.astral.sh/uv/)
uv tool install .

# ...or with pip
pip install .

# ...or run without installing, from the repo root
uv run mzk3 <command>
```

Once installed, invoke it as `mzk3`.

Note: You don't need to create the cluster manually. Use `mzk3 create-cluster` or `mzk3 install --create-cluster` instead.

## Installation

### Basic Install

```bash
# Create cluster and install in one step
mzk3 install --create-cluster

# Or create cluster first, then install
mzk3 create-cluster
mzk3 install
```

This will:
1. Download required configuration files from the Materialize repository
2. Deploy PostgreSQL (metadata backend)
3. Install the RustFS operator and deploy a RustFS Tenant (S3 blob storage for persist)
4. Install the Materialize Operator via Helm
5. Deploy a Materialize instance

### Storage backend

Materialize persist needs S3-compatible blob storage.
[RustFS](https://github.com/rustfs/rustfs) provides it, deployed via the
[RustFS operator](https://github.com/rustfs/operator) (pinned to a release tag,
installed from its in-repo helm chart into `rustfs-system`). mzk3 then applies a
`Tenant` CR (`rustfs`, namespace `materialize`):

- **PVC-backed** (durable — survives pod restarts, unlike a plain emptyDir
  Deployment), single-node (`servers: 1`, `volumesPerServer: 1`).
- **Buckets auto-created** by the operator (`bucket`, `persist`, `thanos`) — no
  separate bucket-creation Job.
- Credentials come from a `rustfs-credentials` secret (`minioadmin`/`minioadmin`;
  the operator requires ≥8-char keys). mzk3 repoints the Materialize CR's
  persist endpoint + credentials at the Tenant's S3 service, `rustfs-io:9000`.

The S3 service is `rustfs-io` (plus `rustfs-hl` headless and `rustfs-console`).
The integration suite (`pytest --run-integration`) asserts environmentd reaches
`Ready` and that persist objects land in the bucket.

### Install with Options

```bash
# Install a specific version
mzk3 install -v v26.12.1

# Install with a license key
mzk3 install --license-key /path/to/license.key

# Install with Prometheus/Grafana monitoring
mzk3 install --install-dashboards

# Combine options
mzk3 install -v v26.12.1 --license-key /path/to/license.key --install-dashboards
```

## Commands

### create-cluster

Create a new k3d cluster for Materialize.

```bash
# Create with default name (mzk3-cluster)
mzk3 create-cluster

# Create with custom name
mzk3 create-cluster -c my-cluster
```

mzk3 tracks which clusters it creates. The `install` command will only work on clusters created by mzk3 (use `--force` to bypass this check).

### install

Deploy Materialize and all dependencies.

```bash
mzk3 install [options]
```

**Cluster Safety:** By default, `install` only works on clusters created by mzk3. This prevents accidental modifications to existing clusters. Options:
- Use `--create-cluster` to create a new cluster and install in one step
- Use `--force` to install on any cluster (bypasses the safety check)

**Idempotent:** Running `install` multiple times is safe. mzk3 checks existing versions and skips components that are already at the requested version.

### upgrade

Upgrade an existing Materialize deployment to a new version.

```bash
# Standard upgrade
mzk3 upgrade -v v26.12.1

# Force upgrade (bypasses safety checks)
mzk3 upgrade -v v26.12.1 --force

# Skip confirmation prompt
mzk3 upgrade -v v26.12.1 -y
```

The upgrade process:
1. Updates the Materialize Operator
2. Patches the Materialize CR with the new `environmentdImageRef`
3. Triggers a rolling upgrade via `requestRollout`

### destroy-environment / recreate-environment

Tear down (and optionally recreate) just the Materialize **environment** — the
instance and its state — while leaving the cluster, operator, backends, and
monitoring in place. Useful for a clean slate or recovering a corrupted
environment without a full `reset`.

```bash
# Tear down the instance and WIPE its state (persist bucket + postgres metadata)
mzk3 destroy-environment --yes

# Same, but keep persist + metadata (just delete/leave the instance down)
mzk3 destroy-environment --keep-state --yes

# Destroy then recreate a fresh instance
mzk3 recreate-environment --yes
```

By default the state is wiped (required to recover from persist corruption);
`--keep-state` preserves it. `recreate-environment` only runs on clusters mzk3
created (use `--force` to override) and requires an existing install
(operator + backends).

### reset

Destroy and recreate the entire k3d cluster. **Warning: This deletes all data.**

```bash
mzk3 reset

# Skip confirmation
mzk3 reset -y
```

### status

Check the status of your Materialize deployment.

```bash
mzk3 status
```

### list-versions

Show available Materialize versions from the Helm repository.

```bash
mzk3 list-versions
```

## Configuration Options

| Flag | Environment Variable | Default | Description |
|------|---------------------|---------|-------------|
| `-v, --version` | `MZ_VERSION` | `v26.4.0` | Materialize version to deploy |
| `-o, --operator-version` | `MZ_OPERATOR_VERSION` | same as `--version` | Materialize Operator Helm chart version |
| `-n, --namespace` | `MZ_NAMESPACE` | `materialize` | Kubernetes namespace for the operator |
| `-i, --instance-ns` | `MZ_INSTANCE_NS` | `materialize-environment` | Namespace for Materialize instances |
| `-r, --release-name` | `MZ_RELEASE_NAME` | `my-materialize-operator` | Helm release name |
| `-l, --license-key` | `MZ_LICENSE_KEY` | - | Path to license key file |
| `-c, --cluster` | `K3D_CLUSTER_NAME` | `mzk3-cluster` | k3d cluster name |
| `--install-dashboards` | `MZ_INSTALL_DASHBOARDS` | `false` | Install upstream materialize-monitoring stack |
| `--config` | `MZ_CONFIG` | - | Path to a JSON config file (see below) |
| `--create-cluster` | - | `false` | Create k3d cluster before install |
| `--force` | - | `false` | Force operation (upgrade: forceRollout; install: bypass cluster check) |
| `-y, --yes` | - | `false` | Skip confirmation prompts |

### Config file

Instead of passing everything on the command line, settings and per-component
resource limits can be read from a JSON file (`--config file.json` or
`MZ_CONFIG=file.json`) — a lightweight, declarative alternative to a pile of
flags.

```json
{
  "version": "v26.30.0",
  "install_dashboards": true,
  "resources": {
    "environmentd": {"cpu": "2", "memory": "4Gi"},
    "rustfs":       {"cpu": "1", "memory": "1Gi"}
  }
}
```

```bash
mzk3 install --create-cluster --config ./mzk3.json
```

Precedence (highest wins): **explicit CLI flag > config file > environment
variable > built-in default**. Any key in the table above may appear in the
config file (using the long flag name with underscores, e.g. `operator_version`,
`instance_ns`, `license_key`). The `resources` object is deep-merged over the
defaults, so you can override just one field (e.g. only `rustfs.cpu`).

## Accessing Materialize

### Web Console

```bash
# Find the console service
MZ_CONSOLE=$(kubectl -n materialize-environment get svc -o name | grep console)

# Port forward
kubectl port-forward $MZ_CONSOLE 8080:8080 -n materialize-environment
```

Open http://localhost:8080 in your browser.

### SQL Client (psql)

```bash
# Find the balancer service
MZ_BALANCER=$(kubectl -n materialize-environment get svc -o name | grep balancerd)

# Port forward
kubectl port-forward $MZ_BALANCER 6875:6875 -n materialize-environment

# Connect with psql
psql -h localhost -p 6875 -U mz_system materialize
```

### Internal Cluster Access

From within the cluster, connect to:
- **Hostname**: `<instance-name>-balancerd.materialize-environment.svc.cluster.local`
- **Port**: `6875`

## Monitoring

When installed with `--install-dashboards`, mzk3 deploys the upstream
[materialize-monitoring](https://github.com/MaterializeInc/materialize-monitoring)
stack (Alloy + Grafana + dashboards + Thanos, with kube-state-metrics /
alertmanager) into the `monitoring` namespace.

Because that chart is not published to a Helm repository yet, mzk3 installs it
from the pinned release tag (`materialize-monitoring/v0.6.0`) — it downloads the
tag tarball (subchart dependencies are vendored in it) and runs `helm install`
against the CRDs chart and then the umbrella chart. Bump `MONITORING_VERSION`
in `src/mzk3/commands.py` to track a newer release.

The metrics pipeline is: Alloy scrapes → alloy-gateway remote-writes → **Thanos
receive** → **Thanos query** → Grafana. mzk3 wires up several things the v0.6.0
chart leaves incomplete for a self-contained local install:

- **Thanos object storage → RustFS.** Thanos is enabled with its object store
  pointed at the in-cluster RustFS (S3), using a dedicated `thanos` bucket — so
  no external/cloud object storage is needed. (Thanos store-gateway and
  compactor are left off; receive + query cover recent data.)
- **Grafana datasource.** The chart ships none; mzk3 creates a `GrafanaDatasource`
  pointing at `thanos-query` (the dashboards' `metricsDatasource` variable
  resolves to it).
- **Materialize scrape target.** The chart doesn't scrape Materialize; mzk3
  creates a `PodMonitor` for environmentd (`/metrics/public`, or `/metrics` for
  versions before v26.25).
- **Grafana auth fix.** Works around a chart bug where the bundled Grafana CR
  references an admin-credentials secret the chart never creates and a wrong
  service URL, so grafana-operator can authenticate and sync dashboards.
- **Container (cAdvisor) metrics.** Adds a kubelet `ServiceMonitor` (plus a
  `Service`/`Endpoints` pointed at the node IPs and a small RBAC grant) so
  alloy-gateway scrapes each node's `/metrics/cadvisor` for container CPU/memory
  usage — the chart doesn't scrape the kubelet, and nothing else maintains
  kubelet endpoints without a prometheus-operator.

### Accessing Grafana

```bash
GRAFANA=$(kubectl -n monitoring get svc -o name | grep grafana | head -1)
kubectl port-forward -n monitoring $GRAFANA 3000:80
# admin password:
kubectl get secret mz-monitoring-grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

Then open http://localhost:3000 (user `admin`) and find the **Materialize
Overview** (env-top) dashboard, backed by the `Thanos` datasource.

Notes:
- Dashboards require Materialize **v26.25+** for the `/metrics/public` endpoint
  (older versions are scraped at `/metrics`) and target Materialize v26.24+.
- Only `env-*` dashboards ship in v0.6.0 (`dashboards.selected`).

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          k3d Cluster                                 │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                    materialize namespace                         ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  ││
│  │  │  PostgreSQL │  │   RustFS    │  │  Materialize Operator   │  ││
│  │  │  (metadata) │  │   (blobs)   │  │                         │  ││
│  │  └─────────────┘  └─────────────┘  └─────────────────────────┘  ││
│  └─────────────────────────────────────────────────────────────────┘│
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │               materialize-environment namespace                  ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  ││
│  │  │ environmentd│  │  balancerd  │  │        clusterd         │  ││
│  │  │   (coord)   │  │   (proxy)   │  │  (compute/storage)      │  ││
│  │  └─────────────┘  └─────────────┘  └─────────────────────────┘  ││
│  │                                                                  ││
│  │  ┌─────────────────────────────────────────────────────────────┐││
│  │  │                        console                               │││
│  │  │                     (web UI)                                 │││
│  │  └─────────────────────────────────────────────────────────────┘││
│  └─────────────────────────────────────────────────────────────────┘│
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                   monitoring namespace                           ││
│  │  ┌─────────────────────────┐  ┌─────────────────────────────┐   ││
│  │  │       Prometheus        │  │          Grafana            │   ││
│  │  └─────────────────────────┘  └─────────────────────────────┘   ││
│  └─────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

## Troubleshooting

### Check Pod Status

```bash
# Operator namespace
kubectl get pods -n materialize

# Instance namespace
kubectl get pods -n materialize-environment

# Monitoring (if enabled)
kubectl get pods -n monitoring
```

### View Logs

```bash
# Environmentd logs
kubectl logs -n materialize-environment -l app=environmentd --tail=100

# Operator logs
kubectl logs -n materialize -l app.kubernetes.io/name=materialize-operator --tail=100
```

### Common Issues

**Pods stuck in Pending**
- Check node resources: `kubectl describe nodes`
- Check events: `kubectl get events -n materialize-environment`

**Connection refused on port forward**
- Ensure the pod is running: `kubectl get pods -n materialize-environment`
- Check if the service exists: `kubectl get svc -n materialize-environment`

**Monitoring metrics missing**
- Wait a few minutes for scraping to begin
- Check Prometheus targets: Access Prometheus UI > Status > Targets

## Development

`mzk3` is a Python package under `src/mzk3/`, built test-first.

### Layout

```
src/mzk3/
  config.py     # Config dataclass + resolve(): defaults < env < flags
  patch.py      # pure YAML transforms (replaces the bash `sed` edits)
  commands.py   # pure argv builders (helm/kubectl/k3d) + download URLs
  runner.py     # the single subprocess boundary (test seams: dry_run, responder)
  log.py        # colored [INFO]/[WARN]/[STEP] output
  cli.py        # orchestration: wires the tested core to a live cluster
  data/         # bundled monitoring assets (dashboard, prometheus values)
tests/          # pytest suite
```

Side effects flow through a single `Runner`. Pure logic (config, patching,
command builders) is tested directly; command flows are tested with a dry-run
`Runner` that records the argv it would execute — no cluster or network needed.

### Setup and tests

```bash
# Requires uv (https://docs.astral.sh/uv/)
uv run pytest                    # fast, hermetic unit suite (no cluster)
uv run pytest --run-integration  # also spin up a real k3d cluster end to end
uv run mzk3 --help               # run the Python CLI
```

The unit suite is pure and runs in well under a second. Integration tests
(`tests/test_integration.py`, marked `integration`) are skipped unless
`--run-integration` is passed; they create a throwaway k3d cluster, install
Materialize, assert the environmentd pod reaches `Ready`, and tear the cluster
down. They need docker/k3d/kubectl/helm on PATH plus network access.

Tests are the source of truth for the port — add or update a test before
changing behavior.

## License

See [Materialize License](https://github.com/MaterializeInc/materialize/blob/main/LICENSE) for details.
