# Materialize on k3s/k3d

A deployment script for running [Materialize](https://materialize.com/) self-managed on local k3s/k3d Kubernetes clusters.

## Overview

`mzk3` is a unified script that handles the complete lifecycle of a Materialize deployment:

- **Create Cluster** - Create a new k3d cluster for Materialize
- **Install** - Deploy Materialize with all required dependencies
- **Upgrade** - Upgrade to a new Materialize version with controlled rollouts
- **Reset** - Destroy and recreate the k3d cluster
- **Status** - Check the health of your deployment
- **List Versions** - Show available Materialize versions

## Prerequisites

- [k3d](https://k3d.io/) - k3s in Docker (for local development)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) v3+
- [Docker](https://docs.docker.com/get-docker/)

### Quick k3d Setup

```bash
# Install k3d (macOS)
brew install k3d
```

Note: You don't need to create the cluster manually. Use `mzk3 create-cluster` or `mzk3 install --create-cluster` instead.

## Installation

### Basic Install

```bash
# Create cluster and install in one step
./mzk3 install --create-cluster

# Or create cluster first, then install
./mzk3 create-cluster
./mzk3 install
```

This will:
1. Download required configuration files from the Materialize repository
2. Deploy PostgreSQL (metadata backend)
3. Deploy MinIO (S3-compatible blob storage)
4. Install the Materialize Operator via Helm
5. Deploy a Materialize instance

### Install with Options

```bash
# Install a specific version
./mzk3 install -v v26.12.1

# Install with a license key
./mzk3 install --license-key /path/to/license.key

# Install with Prometheus/Grafana monitoring
./mzk3 install --install-dashboards

# Combine options
./mzk3 install -v v26.12.1 --license-key /path/to/license.key --install-dashboards
```

## Commands

### create-cluster

Create a new k3d cluster for Materialize.

```bash
# Create with default name (mzk3-cluster)
./mzk3 create-cluster

# Create with custom name
./mzk3 create-cluster -c my-cluster
```

The script tracks which clusters it creates. The `install` command will only work on clusters created by mzk3 (use `--force` to bypass this check).

### install

Deploy Materialize and all dependencies.

```bash
./mzk3 install [options]
```

**Cluster Safety:** By default, `install` only works on clusters created by mzk3. This prevents accidental modifications to existing clusters. Options:
- Use `--create-cluster` to create a new cluster and install in one step
- Use `--force` to install on any cluster (bypasses the safety check)

**Idempotent:** Running `install` multiple times is safe. The script checks existing versions and skips components that are already at the requested version.

### upgrade

Upgrade an existing Materialize deployment to a new version.

```bash
# Standard upgrade
./mzk3 upgrade -v v26.12.1

# Force upgrade (bypasses safety checks)
./mzk3 upgrade -v v26.12.1 --force

# Skip confirmation prompt
./mzk3 upgrade -v v26.12.1 -y
```

The upgrade process:
1. Updates the Materialize Operator
2. Patches the Materialize CR with the new `environmentdImageRef`
3. Triggers a rolling upgrade via `requestRollout`

### reset

Destroy and recreate the k3d cluster. **Warning: This deletes all data.**

```bash
./mzk3 reset

# Skip confirmation
./mzk3 reset -y
```

### status

Check the status of your Materialize deployment.

```bash
./mzk3 status
```

### list-versions

Show available Materialize versions from the Helm repository.

```bash
./mzk3 list-versions
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
| `--install-dashboards` | `MZ_INSTALL_DASHBOARDS` | `false` | Install Prometheus/Grafana stack |
| `--create-cluster` | - | `false` | Create k3d cluster before install |
| `--force` | - | `false` | Force operation (upgrade: forceRollout; install: bypass cluster check) |
| `-y, --yes` | - | `false` | Skip confirmation prompts |

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

When installed with `--install-dashboards`, the script deploys:
- **Prometheus** - Metrics collection and storage
- **Grafana** - Visualization dashboards

### Accessing Grafana

```bash
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
```

Open http://localhost:3000 (login: `admin` / `admin`)

Navigate to **Dashboards > Materialize Overview** for:
- CPU and Memory usage by pod
- Wallclock lag (freshness) metrics
- Active sessions and subscribes
- Arrangement size and record counts
- Pod restarts and OOM kills

### Accessing Prometheus

```bash
kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090
```

Open http://localhost:9090

### Key Metrics

| Metric | Description |
|--------|-------------|
| `mz_dataflow_wallclock_lag_seconds` | How far behind real-time a dataflow is |
| `mz_arrangement_size_bytes` | Memory used by arrangements |
| `mz_arrangement_record_count` | Number of records in arrangements |
| `mz_active_sessions` | Current active database sessions |
| `mz_active_subscribes` | Current active SUBSCRIBE queries |
| `kube_pod_container_status_restarts_total` | Container restart count (indicates crashes/OOMs) |
| `kube_pod_container_status_last_terminated_reason` | Why container was terminated (OOMKilled, Error, etc.) |

### Monitoring Limitations on macOS/k3d

Some metrics are not available when running k3d on macOS:
- `container_spec_memory_limit_bytes` - cAdvisor metrics require native Linux
- `container_spec_memory_swap_limit_bytes` - Same limitation

These metrics work correctly on native Linux Kubernetes deployments.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          k3d Cluster                                 │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │                    materialize namespace                         ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  ││
│  │  │  PostgreSQL │  │    MinIO    │  │  Materialize Operator   │  ││
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

## License

See [Materialize License](https://github.com/MaterializeInc/materialize/blob/main/LICENSE) for details.
