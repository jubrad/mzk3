---
name: mzk3
description: Deploy and operate self-managed Materialize on a local k3d cluster with the mzk3 CLI — fresh installs, upgrades, deploying custom/dev Materialize builds (custom environmentd images, a local materialize checkout's operator helm chart, pinned operator chart versions), add-ons/demo data, and reading monitoring data (Grafana, Thanos/PromQL, raw environmentd metrics). Use whenever the user wants to "spin up mzk3", "install Materialize on k3d/k3s", "deploy my dev build", "test my operator chart change", "upgrade to vX", "reset the environment", "open Grafana", "query metrics", or "check why environmentd is unhealthy" in this repo.
---

# mzk3: Materialize on k3d

`mzk3` is a Python CLI (this repo, `src/mzk3/`) that creates a k3d cluster and
installs the full self-managed stack: PostgreSQL (metadata), RustFS (S3 for
persist), the Materialize operator (Helm), a Materialize instance, and
optionally the materialize-monitoring stack and upstream add-ons.

## Ground rules

- **License key**: required for `install` and `recreate-environment`. The
  path differs per user. Find it in this order: `MZ_LICENSE_KEY` already set
  in the environment, `"license_key"` in the `--config` file, a location
  named in the user's CLAUDE.md or memory, otherwise **ask the user** — don't
  guess or search the filesystem for it. Examples below use `-l "$KEY"` for
  that path (or export `MZ_LICENSE_KEY=<path>` once and drop `-l`). Pass it
  **by path only**; never `cat`, print, or paste its contents.
- **Working directory matters.** mzk3 writes generated files into the cwd
  (`sample-values-k3s.yaml`, `sample-materialize.yaml`, `monitoring-values.yaml`,
  `addon-*.sql`, tarballs). `upgrade` *reuses* an existing
  `sample-values-k3s.yaml` from the cwd. Run from the repo root (these are
  gitignored there) and delete a stale `sample-values-k3s.yaml` if you switch
  between published and local charts and want fresh values.
- **Invocation**: `mzk3 ...` if installed (`uv tool install .`), otherwise
  `uv run mzk3 ...` from the repo root. After changing mzk3's own code, use
  `uv run` (or `uv tool install --reinstall .`) so you run the new code.
- Long operations (install: 5–10 min first run; monitoring adds several more)
  should run with a generous timeout or in the background.
- Destructive commands (`reset`, `destroy-environment`, `recreate-environment`)
  need no confirmation: this is a disposable local cluster, so run them
  directly with `-y`/`--yes` (they otherwise block on an interactive prompt).
- `mzk3 help` / `mzk3 -h` prints every flag and env var; `src/mzk3/config.py`
  is the source of truth for defaults (`DEFAULT_VERSION`).

## Topology

| Namespace | Contents |
|---|---|
| `materialize` | operator (release `my-materialize-operator`), postgres, RustFS Tenant `rustfs` (S3 svc `rustfs-io:9000`, `minioadmin`/`minioadmin`) |
| `rustfs-system` | RustFS operator |
| `materialize-environment` | Materialize CR, environmentd, clusterd, balancerd, console |
| `addons` | optional postgres / mysql / redpanda / polaris (iceberg) |
| `monitoring` | optional Alloy, Thanos receive/query, grafana-operator + Grafana |

Default k3d cluster name: `mzk3-cluster` (`-c` / `K3D_CLUSTER_NAME`).

## 1. Deploy

```bash
# Fresh cluster + default Materialize version, with monitoring
mzk3 install --create-cluster -l "$KEY" --install-dashboards

# Specific version
mzk3 install --create-cluster -l "$KEY" -v v26.30.0

# With upstream systems: clean (--addon) or seeded with demo data (--install-demo)
mzk3 install --create-cluster -l "$KEY" --addon postgres,kafka
mzk3 install --create-cluster -l "$KEY" --install-demo     # all four, seeded (see DEMO.md)
```

`install` is idempotent: re-run it on an existing cluster to add monitoring or
add-ons. Settings can also come from a JSON config (`--config mzk3.json`),
including per-component `resources` — see README "Config file".
Precedence: flags > config file > env vars > defaults.

Check health:

```bash
mzk3 status
kubectl get materialize -n materialize-environment -o yaml   # CR status/conditions
kubectl get pods -A
```

### Connect

```bash
# SQL (balancerd)
kubectl port-forward -n materialize-environment \
  "$(kubectl -n materialize-environment get svc -o name | grep balancerd)" 6875:6875
psql -h localhost -p 6875 -U materialize materialize

# Web console
kubectl port-forward -n materialize-environment \
  "$(kubectl -n materialize-environment get svc -o name | grep console)" 8080:8080
# open http://localhost:8080
```

Any ordinary user name works (authenticatorKind None); `mz_system` is refused
on the external port. For internal/system access, `kubectl exec` into the
environmentd pod or port-forward its internal SQL port 6877.

## 2. Upgrade (published versions)

```bash
mzk3 upgrade --preview                 # print the plan, change nothing
mzk3 upgrade -v v26.41.0               # all installed components
mzk3 upgrade -v latest                 # newest stable chart in the helm repo
mzk3 upgrade -v v26.41.0 --component materialize -y
mzk3 upgrade --component monitoring    # just re-helm the monitoring stack
mzk3 list-versions                     # available operator chart versions
```

Materialize upgrade = helm-upgrade the operator, patch the CR's
`environmentdImageRef`, then patch `requestRollout` + `forcePromote` (a fresh
UUID) so the 0dt cutover completes on a single-node box. `--force` adds
`forceRollout` (rolls even when nothing changed).

Operator chart vs Materialize version: they default to the same value. If the
chart for a version isn't published yet, mzk3 warns and uses the newest chart.
Pin the chart independently with `-o / --operator-version`:

```bash
mzk3 upgrade -v v26.42.0 -o v26.41.0 --component materialize
```

## 3. Custom Materialize builds and helm charts

### Custom images (`--environmentd-image`)

```bash
mzk3 install -l "$KEY" \
  --environmentd-image myuser/environmentd:v26.42.0-dev.0--local.my-branch

mzk3 upgrade --component materialize \
  --environmentd-image myuser/environmentd:v26.42.0-dev.0--local.my-branch
```

- The ref is set verbatim as the CR's `environmentdImageRef`. The operator
  derives clusterd/balancerd from it (same registry prefix + tag), and mzk3
  sets the operator image to `<prefix>/orchestratord:<tag>`. So **all of
  `environmentd`, `clusterd`, `balancerd`, `orchestratord` must exist** with
  that tag under that prefix.
- Images must be pullable from k3d — push to a registry (e.g. Docker Hub). An
  alternative for unpushed local builds is
  `k3d image import <img>... -c mzk3-cluster` for each of the four images
  (works because dev tags aren't `:latest`, so pull policy is IfNotPresent).
- Building: in a materialize checkout, `bin/mzimage` / `bin/mzcompose` build
  images; see the `mz-run` skill. Tag/push them so the four names line up.

### Local operator chart (`--local-repo`)

```bash
mzk3 install -l "$KEY" --local-repo ~/src/materialize
mzk3 upgrade --component materialize --local-repo ~/src/materialize
```

- Uses `<repo>/misc/helm-charts/operator` as the operator chart and copies its
  `values.yaml` plus `misc/helm-charts/testing/{postgres,materialize}.yaml`
  into the cwd (overwriting `sample-*.yaml`). Chart version resolution is
  skipped; `-o` is ignored.
- `-v` still picks the environmentd image (`materialize/environmentd:<v>`)
  unless `--environmentd-image` is given.
- Typical dev loop for an operator change: edit the chart/orchestratord in the
  checkout, build+push images, then

  ```bash
  mzk3 upgrade --component materialize --local-repo ~/src/materialize \
    --environmentd-image myuser/environmentd:<tag> --force -y
  ```

### Extra helm values (`-f / --values-file`)

`-f FILE` (or `MZ_VALUES_FILE`) replaces the operator values file
(default `sample-values-k3s.yaml`). mzk3 always layers its own `--set`s on top
(observability on, `operator.cloudProvider.region=k3s`, and the operator image
when `--environmentd-image` is set). Start from the downloaded
`sample-values-k3s.yaml` and edit a copy.

### Verify what's actually running

```bash
helm list -n materialize
helm get values my-materialize-operator -n materialize
kubectl get materialize -n materialize-environment \
  -o jsonpath='{.items[0].spec.environmentdImageRef}{"\n"}'
kubectl get pods -n materialize-environment \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'
kubectl get deploy my-materialize-operator -n materialize \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

## 4. Resetting state

```bash
mzk3 destroy-environment --keep-state --yes   # delete the instance, keep persist + metadata
mzk3 destroy-environment --yes                # delete instance AND wipe persist bucket + postgres metadata
mzk3 recreate-environment --yes               # wipe + fresh instance (needs license key)
mzk3 reset -y                                 # delete & recreate the whole k3d cluster
k3d cluster delete mzk3-cluster               # remove entirely
```

Use `recreate-environment` to recover from persist corruption (environmentd
crash-looping on rollup errors) without reinstalling everything.

## 5. Monitoring data

Requires `--install-dashboards` (install or a later `install` re-run). Pipeline:
Alloy scrapes → alloy-gateway remote-write → **Thanos receive** → **Thanos
query** → Grafana. mzk3 adds a `PodMonitor` scraping environmentd on port 6878
(`/metrics/public` (or `/metrics` before v26.25), `/metrics/mz_compute`,
`/metrics/mz_frontier`, `/metrics/mz_storage`, `/metrics/mz_usage`) plus a
kubelet cAdvisor scrape for container CPU/memory. Allow a few minutes after
install for data to appear.

### Grafana (humans)

```bash
kubectl port-forward -n monitoring \
  "$(kubectl -n monitoring get svc -o name | grep grafana | head -1)" 3000:80
kubectl get secret mz-monitoring-grafana -n monitoring \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

Open http://localhost:3000 as `admin`; dashboards are the `env-*` set
(start with **Materialize Overview** / env-top), datasource `Thanos`.

### PromQL directly against Thanos query (best for agents)

```bash
kubectl port-forward -n monitoring svc/thanos-query 9090:9090 &

# instant query
curl -s 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up' | jq '.data.result[] | {job: .metric.job, pod: .metric.pod, v: .value[1]}'

# range query (last 30m, 30s step)
curl -s 'http://localhost:9090/api/v1/query_range' \
  --data-urlencode 'query=sum(rate(mz_compute_commands_total[5m]))' \
  --data-urlencode "start=$(( $(date +%s) - 1800 ))" \
  --data-urlencode "end=$(date +%s)" --data-urlencode 'step=30' | jq '.data.result'

# discover metric names / labels
curl -s 'http://localhost:9090/api/v1/label/__name__/values' | jq -r '.data[]' | grep '^mz_' | head -50
curl -s 'http://localhost:9090/api/v1/series' --data-urlencode 'match[]=mz_compute_commands_total' | jq '.data[0]'
```

Useful queries:

- Scrape health: `up{namespace="materialize-environment"}`
- Container memory: `container_memory_working_set_bytes{namespace="materialize-environment", container!=""}`
- Container CPU: `sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="materialize-environment", container!=""}[5m]))`
- Dashboards filter on `materialize_cloud_organization_namespace` (relabelled
  from pod labels) — reuse it when copying dashboard PromQL.

Kill the port-forward when done (`kill %1` or `pkill -f 'port-forward.*thanos-query'`).

### Raw metrics straight from environmentd (no monitoring stack needed)

```bash
POD=$(kubectl get pod -n materialize-environment -l materialize.cloud/app=environmentd -o name | head -1)
kubectl port-forward -n materialize-environment "$POD" 6878:6878 &
curl -s localhost:6878/metrics/public | grep -v '^#' | head
curl -s localhost:6878/metrics/mz_compute | grep '^mz_compute' | head
```

### Monitoring troubleshooting

- No data in Grafana: `up` in Thanos query shows which targets are scraped;
  check `kubectl get pods -n monitoring` and alloy/alloy-gateway logs.
- Dashboard variables empty: they need `mz_compute_*` series — confirm the
  `PodMonitor` exists (`kubectl get podmonitor -n materialize-environment`).
- Monitoring stack version is pinned by `MONITORING_VERSION` in
  `src/mzk3/commands.py`; bump + `mzk3 upgrade --component monitoring`.

## 6. Debugging

```bash
kubectl logs -n materialize-environment -l materialize.cloud/app=environmentd --tail=200
kubectl logs -n materialize deploy/my-materialize-operator --tail=200        # orchestratord
kubectl describe materialize -n materialize-environment
kubectl get events -n materialize-environment --sort-by=.lastTimestamp | tail -30
kubectl get pods -n materialize-environment -o wide
```

Common causes:

- **Instance never created / CR rejected** → missing or invalid license key.
- **ImagePullBackOff** after `--environmentd-image` → one of
  environmentd/clusterd/balancerd/orchestratord missing at that prefix+tag,
  or not pushed/imported.
- **Upgrade "hangs"** → check operator logs for the rollout; mzk3 sets
  `forcePromote`, so a stuck generation usually means the new environmentd
  is crash-looping (check its logs).
- **environmentd crash-loops on persist errors** → `mzk3 recreate-environment --yes`.
- **Pods Pending** → Docker resource limits; `kubectl describe nodes`.

## 7. Working on mzk3 itself

```bash
uv run pytest                       # fast unit suite (dry-run Runner, no cluster)
uv run pytest --run-integration     # real k3d install (slow; needs docker/k3d/helm + license key)
```

Layout: `config.py` (flags/env/defaults), `commands.py` (pure helm/kubectl argv
builders + pinned versions), `patch.py` (YAML transforms), `cli.py`
(orchestration), `addons.py`, `runner.py` (single subprocess boundary). Keep the
README and this skill in sync when flags change. Stage explicit paths when
committing — never `git add -A` (other checkouts live inside the mzk3 tree).
