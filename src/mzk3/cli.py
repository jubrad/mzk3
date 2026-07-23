"""Command-line entry point: orchestrates the tested core against a live system."""

from __future__ import annotations

import json
import re
import sys
import tarfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import commands as c
from . import log
from .commands import HELM_REPO
from .config import Config, build_parser, resolve
from .runner import CommandError, Runner


class Abort(RuntimeError):
    """Raised to stop a command with a nonzero exit, message already logged."""


# --- helpers -------------------------------------------------------------

def download(url: str, dest: str) -> None:
    """Fetch url to dest; abort if the response is empty or a 404 sentinel."""
    try:
        with urllib.request.urlopen(url) as resp:
            body = resp.read()
    except OSError as e:
        log.error(f"Failed to download: {url} ({e})")
        raise Abort from e
    if not body or body.startswith(b"404:"):
        log.error(f"Failed to download: {url}")
        raise Abort
    Path(dest).write_bytes(body)


def _parallel(*thunks) -> None:
    """Run independent install steps concurrently. Each thunk is a no-arg
    callable; blocking subprocess calls release the GIL so they truly overlap.
    Re-raises the first exception once all have finished.
    """
    thunks = [t for t in thunks if t is not None]
    if len(thunks) == 1:
        thunks[0]()
        return
    with ThreadPoolExecutor(max_workers=len(thunks)) as ex:
        futures = [ex.submit(t) for t in thunks]
    for f in futures:  # after the pool drains; surfaces the first error
        f.result()


def _wait_backends_ready(runner: Runner) -> None:
    log.info("Waiting for backends (postgres + rustfs) to be ready...")
    runner.run(["kubectl", "wait", "--for=condition=available", "--timeout=300s",
                "deployment/postgres", "-n", "materialize"], check=False)
    runner.run(["kubectl", "wait", "--for=jsonpath={.status.currentState}=Ready",
                "tenant/rustfs", "-n", "materialize", "--timeout=300s"], check=False)


def require(runner: Runner, binary: str, hint: str) -> None:
    if runner.which(binary) is None:
        log.error(hint)
        raise Abort


def check_prerequisites(runner: Runner) -> None:
    log.info("Checking prerequisites...")
    require(runner, "kubectl", "kubectl is not installed. Please install kubectl first.")
    require(runner, "helm", "Helm is not installed. Please install Helm 3.x first.")
    require(runner, "curl", "curl is not installed. Please install curl first.")
    if not runner.run(["kubectl", "cluster-info"], check=False, capture=True).ok:
        log.error(
            "Cannot connect to Kubernetes cluster. "
            "Please ensure K3s is running and kubectl is configured."
        )
        raise Abort
    log.info("All prerequisites met.")


def verify_k3s_cluster(runner: Runner) -> None:
    log.info("Verifying K3s cluster...")
    res = runner.run(
        ["kubectl", "get", "nodes", "-o",
         "jsonpath={.items[0].status.nodeInfo.kubeletVersion}"],
        check=False, capture=True,
    )
    if "k3s" in res.stdout:
        log.info("K3s cluster detected.")
    else:
        log.warn("This may not be a K3s cluster. Proceeding anyway...")
    runner.run(["kubectl", "get", "nodes"], check=False)


def is_mzk3_cluster(runner: Runner, cluster_name: str) -> bool:
    """True if the cluster's k3d server container carries our created-by label."""
    if runner.which("k3d") is None or runner.which("docker") is None:
        return False
    res = runner.run(
        ["docker", "inspect", f"k3d-{cluster_name}-server-0"],
        check=False, capture=True,
    )
    if not res.ok:
        return False
    try:
        labels = json.loads(res.stdout)[0]["Config"]["Labels"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return False
    return labels.get("created-by") == "mzk3"


def list_mzk3_clusters(runner: Runner) -> list[str]:
    if runner.which("k3d") is None:
        return []
    res = runner.run(["k3d", "cluster", "list", "-o", "json"], check=False, capture=True)
    if not res.ok:
        return []
    try:
        clusters = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    names = [
        cl["name"]
        for cl in clusters
        if cl.get("serversRunning", 0) > 0 and "name" in cl
    ]
    return [n for n in names if is_mzk3_cluster(runner, n)]


def wait_for_node_ready(runner: Runner, retries: int = 30, delay: int = 5) -> None:
    log.info("Waiting for cluster to be ready...")
    time.sleep(delay if not runner.dry_run else 0)
    while retries > 0:
        res = runner.run(["kubectl", "get", "nodes"], check=False, capture=True)
        if res.ok and " Ready" in res.stdout:
            return
        log.info(f"Waiting for node... ({retries} retries left)")
        if not runner.dry_run:
            time.sleep(delay)
        retries -= 1
    log.error("Timeout waiting for k3d node to be ready.")
    raise Abort


def use_cluster_context(runner: Runner, cluster_name: str) -> None:
    """Point kubectl at the given k3d cluster.

    k3d only switches the kubeconfig context when it *creates* a cluster, so a
    reused cluster (or any stale active context, e.g. a cloud cluster) would
    otherwise leave kubectl talking to the wrong API server. Re-merge and
    switch explicitly so all subsequent kubectl/helm calls target this cluster.
    """
    if runner.which("k3d") is None:
        return
    runner.run(["k3d", "kubeconfig", "merge", cluster_name,
                "--kubeconfig-merge-default", "--kubeconfig-switch-context"],
               check=False)


def create_cluster(runner: Runner, cluster_name: str) -> None:
    """Create a labeled k3d cluster, idempotently."""
    require(runner, "k3d",
            "k3d is not installed. Please install k3d first.\nSee: https://k3d.io/")

    existing = runner.run(["k3d", "cluster", "list"], check=False, capture=True)
    if existing.ok and any(
        line.split()[:1] == [cluster_name]
        for line in existing.stdout.splitlines()
    ):
        if is_mzk3_cluster(runner, cluster_name):
            log.info(f"Cluster '{cluster_name}' already exists and is an mzk3 "
                     "cluster. Using it.")
            use_cluster_context(runner, cluster_name)
            return
        log.error(f"Cluster '{cluster_name}' already exists but was not created "
                  "by mzk3.")
        log.error("Use a different name with -c/--cluster, delete the existing "
                  "cluster, or use --force.")
        raise Abort

    log.step(f"Creating k3d cluster '{cluster_name}'...")
    runner.run(c.k3d_create(cluster_name))
    use_cluster_context(runner, cluster_name)
    wait_for_node_ready(runner)
    log.info(f"Cluster '{cluster_name}' created successfully.")
    runner.run(["kubectl", "get", "nodes"], check=False)


def helm_repo_setup(runner: Runner, name: str, url: str) -> None:
    runner.run(["helm", "repo", "add", name, url], check=False)
    runner.run(["helm", "repo", "update", name])


def apply_manifest(runner: Runner, path: str) -> None:
    runner.run(["kubectl", "apply", "-f", path])


def ensure_namespace(runner: Runner, ns: str) -> None:
    created = runner.run(
        ["kubectl", "create", "namespace", ns, "--dry-run=client", "-o", "yaml"],
        capture=True,
    )
    runner.run(["kubectl", "apply", "-f", "-"], text_input=created.stdout)


# --- commands ------------------------------------------------------------

def cmd_list_versions(runner: Runner, cfg: Config) -> None:
    log.info("Updating Helm repository...")
    helm_repo_setup(runner, "materialize", HELM_REPO)
    print()
    log.info("Available Materialize Operator versions:")
    res = runner.run(
        ["helm", "search", "repo", "materialize/materialize-operator", "--versions"],
        check=False, capture=True,
    )
    print("\n".join(res.stdout.splitlines()[:20]))


def cmd_status(runner: Runner, cfg: Config) -> None:
    check_prerequisites(runner)
    for title, argv, empty in [
        ("=== Helm Releases ===",
         ["helm", "list", "-n", cfg.namespace], f"No releases found in {cfg.namespace}"),
        ("=== Materialize Instances ===",
         ["kubectl", "get", "materialize", "-A"], "No Materialize instances found"),
        (f"=== Pods in {cfg.instance_ns} ===",
         ["kubectl", "get", "pods", "-n", cfg.instance_ns],
         f"No pods found in {cfg.instance_ns}"),
        (f"=== Services in {cfg.instance_ns} ===",
         ["kubectl", "get", "svc", "-n", cfg.instance_ns],
         f"No services found in {cfg.instance_ns}"),
    ]:
        print()
        log.info(title)
        res = runner.run(argv, check=False, capture=True)
        print(res.stdout.strip() if res.ok and res.stdout.strip() else empty)


def cmd_create_cluster(runner: Runner, cfg: Config) -> None:
    create_cluster(runner, cfg.cluster_name)
    print()
    log.info("=" * 46)
    log.info(" K3d Cluster Created!")
    log.info("=" * 46)
    log.info(f"Cluster '{cfg.cluster_name}' is ready for Materialize installation.")
    log.info("To install Materialize:")
    log.info(f"  mzk3 install -c {cfg.cluster_name}")


def cmd_reset(runner: Runner, cfg: Config) -> None:
    require(runner, "k3d",
            "k3d is not installed. Please install k3d first.\nSee: https://k3d.io/")
    print()
    log.warn("=" * 46)
    log.warn(" WARNING: This will destroy the K3s cluster!")
    log.warn("=" * 46)
    log.warn(f"This deletes k3d cluster '{cfg.cluster_name}' and ALL its data.")
    log.warn("This action is IRREVERSIBLE.")
    print()
    if not cfg.skip_confirm:
        reply = input("Are you sure you want to reset the cluster? Type 'yes' to "
                      "confirm: ")
        if reply != "yes":
            log.info("Reset cancelled.")
            return

    log.step(f"Step 1: Deleting k3d cluster '{cfg.cluster_name}'...")
    if not runner.run(["k3d", "cluster", "delete", cfg.cluster_name], check=False).ok:
        log.warn(f"Cluster '{cfg.cluster_name}' not found or already deleted.")

    log.step(f"Step 2: Creating new k3d cluster '{cfg.cluster_name}'...")
    runner.run(c.k3d_create(cfg.cluster_name))

    log.step("Step 3: Waiting for cluster to be ready...")
    wait_for_node_ready(runner)
    runner.run(["kubectl", "get", "nodes"], check=False)
    print()
    log.info("K3d Cluster Reset Complete!")


def cmd_install(runner: Runner, cfg: Config) -> None:
    if cfg.create_cluster:
        create_cluster(runner, cfg.cluster_name)
    else:
        # Targeting an existing cluster via -c: make sure kubectl points at it
        # rather than whatever context happens to be active.
        use_cluster_context(runner, cfg.cluster_name)

    check_prerequisites(runner)
    verify_k3s_cluster(runner)

    if not is_mzk3_cluster(runner, cfg.cluster_name):
        if not cfg.force:
            log.error(f"Cluster '{cfg.cluster_name}' was not created by mzk3.")
            log.error("This script only installs on clusters it created.")
            avail = list_mzk3_clusters(runner)
            if avail:
                log.info("Available mzk3 clusters:")
                for name in avail:
                    log.info(f"  - {name}")
            else:
                log.info("No mzk3-created clusters found.")
            log.info("Create one:  mzk3 install --create-cluster")
            log.info("Or bypass:   mzk3 install --force")
            raise Abort
        log.warn(f"Cluster '{cfg.cluster_name}' was not created by mzk3.")
        log.warn("Proceeding anyway due to --force flag.")

    log.info(f"Using Materialize version: {cfg.version}")
    log.info(f"Using Operator version: {cfg.operator_version}")

    _label_nodes(runner)
    _download_configs(runner, cfg)
    _patch_configs(runner, cfg)

    log.step("Setting up Helm repositories...")
    helm_repo_setup(runner, "materialize", HELM_REPO)
    helm_repo_setup(runner, "metrics-server",
                    "https://kubernetes-sigs.github.io/metrics-server/")

    log.step("Creating materialize namespace...")
    ensure_namespace(runner, "materialize")

    install_rustfs_operator(runner)

    log.step("Deploying PostgreSQL and RustFS backends...")
    apply_manifest(runner, "sample-postgres.yaml")
    _deploy_rustfs(runner, cfg)

    # These three are independent: the operator chart doesn't need the backends,
    # metrics-server is standalone, and the backends just need time to come up.
    log.step("Installing operator + metrics-server while backends start "
             "(in parallel)...")
    _parallel(
        lambda: _install_or_skip_operator(runner, cfg),
        lambda: _ensure_metrics_server(runner),
        lambda: _wait_backends_ready(runner),
    )

    # The instance needs the operator + backends (now ready); the monitoring
    # stack is independent of the instance, so bring them up together. Pre-create
    # the instance namespace so monitoring's PodMonitor has somewhere to land.
    ensure_namespace(runner, cfg.instance_ns)
    tasks = [lambda: _deploy_or_skip_instance(runner, cfg)]
    if cfg.install_dashboards:
        log.step("Deploying Materialize instance and monitoring (in parallel)...")
        tasks.append(lambda: install_monitoring(runner, cfg))
    _parallel(*tasks)

    _print_success_install(cfg)


def cmd_upgrade(runner: Runner, cfg: Config) -> None:
    check_prerequisites(runner)
    require(runner, "uuidgen", "uuidgen is not installed. Please install it first.")

    log.info(f"Target Materialize version: {cfg.version}")
    log.info(f"Target Operator version: {cfg.operator_version}")

    log.step("Step 1: Updating Helm repository...")
    # add + update: `helm repo update <name>` fails if the repo was never added
    # (e.g. upgrading from a fresh checkout), which would make the operator
    # chart unresolvable.
    helm_repo_setup(runner, "materialize", HELM_REPO)

    log.step("Step 2: Checking current deployment...")
    runner.run(["helm", "list", "-n", cfg.namespace], check=False)

    instance = _instance_name(runner, cfg)
    if not instance:
        log.warn(f"No Materialize instance found in {cfg.instance_ns}. "
                 "Will only upgrade the operator.")

    print()
    log.warn(f"This will upgrade Materialize to version {cfg.version}")
    if not cfg.skip_confirm:
        reply = input("Continue with upgrade? (y/N) ")
        if reply.strip().lower() not in ("y", "yes"):
            log.info("Upgrade cancelled.")
            return

    ensure_operator_chart_version(runner, cfg)
    log.step(f"Step 3: Upgrading Materialize Operator to {cfg.operator_version}...")
    # Ensure a values file matching the *target* operator version. A custom or
    # prior file is respected; a missing one is fetched (and region-patched) so
    # we never upgrade the operator with no values / stale keys.
    if Path(cfg.values_file).is_file():
        log.info(f"Using values file: {cfg.values_file}")
    else:
        log.info(f"Values file {cfg.values_file} not found; downloading operator "
                 f"values for {cfg.operator_version}...")
        from . import patch
        download(c.operator_values_url(cfg), cfg.values_file)
        vf = Path(cfg.values_file)
        vf.write_text(patch.patch_region(vf.read_text()))
    runner.run(c.operator_upgrade(cfg))

    log.info("Waiting for operator to be ready...")
    runner.run(["kubectl", "wait", "--for=condition=available", "--timeout=300s",
                f"deployment/{cfg.release_name}", "-n", cfg.namespace])
    log.info("Operator upgraded successfully.")

    if instance:
        _upgrade_instance(runner, cfg, instance)

    print()
    log.info("Materialize Upgrade Complete!")


# --- install sub-steps ---------------------------------------------------

def _label_nodes(runner: Runner) -> None:
    log.step("Labeling nodes for Materialize workloads...")
    res = runner.run(
        ["kubectl", "get", "nodes", "--no-headers", "-o",
         "custom-columns=:metadata.name"],
        check=False, capture=True,
    )
    for node in res.stdout.split():
        log.info(f"Labeling node: {node}")
        runner.run(["kubectl", "label", "node", node,
                    "materialize.cloud/swap=true", "--overwrite"], check=False)
        runner.run(["kubectl", "label", "node", node,
                    "workload=materialize-instance", "--overwrite"], check=False)


def _download_configs(runner: Runner, cfg: Config) -> None:
    log.step("Downloading Materialize configuration files...")
    download(c.operator_values_url(cfg), "sample-values-k3s.yaml")
    download(c.postgres_manifest_url(cfg), "sample-postgres.yaml")
    download(c.materialize_manifest_url(cfg), "sample-materialize.yaml")
    log.info("All configuration files downloaded successfully.")


def install_rustfs_operator(runner: Runner) -> None:
    """Install the RustFS operator from its pinned release-tag helm chart."""
    log.step(f"Installing RustFS operator {c.RUSTFS_OPERATOR_VERSION}...")
    if runner.dry_run:
        return
    tarball = "rustfs-operator.tar.gz"
    src = Path("rustfs-operator-src")
    download(c.rustfs_operator_tarball_url(), tarball)
    src.mkdir(exist_ok=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(src, filter="data")
    root = next(p for p in src.iterdir() if p.is_dir())
    chart = root / "deploy" / "rustfs-operator"
    runner.run([
        "helm", "upgrade", "--install", "rustfs-operator", str(chart),
        "--namespace", "rustfs-system", "--create-namespace",
        "--wait", "--timeout", "5m",
    ])


def _deploy_rustfs(runner: Runner, cfg: Config) -> None:
    """Deploy a RustFS Tenant (operator-managed): PVC-backed, buckets
    auto-created, S3 on service rustfs-io:9000."""
    log.step("Deploying RustFS storage backend (Tenant)...")
    runner.run(["kubectl", "apply", "-f", "-"], text_input=_rustfs_tenant(cfg))


def _rustfs_tenant(cfg: Config) -> str:
    rf = cfg.resources.get("rustfs", {})
    cpu = rf.get("cpu", "1")
    memory = rf.get("memory", "1Gi")
    return f"""\
apiVersion: v1
kind: Secret
metadata:
  name: rustfs-credentials
  namespace: materialize
type: Opaque
stringData:
  accesskey: "{c.RUSTFS_ACCESS_KEY}"
  secretkey: "{c.RUSTFS_SECRET_KEY}"
---
apiVersion: rustfs.com/v1alpha1
kind: Tenant
metadata:
  name: rustfs
  namespace: materialize
spec:
  image: rustfs/rustfs:latest
  credsSecret:
    name: rustfs-credentials
  buckets:
    - name: bucket
    - name: persist
    - name: thanos
  pools:
    - name: pool-0
      servers: 1
      persistence:
        volumesPerServer: 1
        volumeClaimTemplate:
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: 20Gi
      resources:
        requests:
          cpu: "{cpu}"
          memory: "{memory}"
        limits:
          cpu: "{cpu}"
          memory: "{memory}"
"""


def _patch_configs(runner: Runner, cfg: Config) -> None:
    from . import patch

    log.info("Patching configuration for K3s...")
    values = Path("sample-values-k3s.yaml")
    values.write_text(patch.patch_region(values.read_text()))

    log.info(f"Patching Materialize CR with version {cfg.version}...")
    mz = Path("sample-materialize.yaml")
    mz.write_text(patch.patch_environmentd_image(mz.read_text(), cfg.version))

    envd = cfg.resources.get("environmentd", {})
    log.info(f"Setting environmentd resources (cpu={envd.get('cpu')}, "
             f"memory={envd.get('memory')})...")
    mz.write_text(patch.patch_environmentd_resources(
        mz.read_text(), envd.get("cpu", "2"), envd.get("memory")))

    # The upstream Materialize CR ships pointing at a `minio` service with
    # minio/minio123 creds; repoint persist at the RustFS operator's S3 service
    # (rustfs-io) with the operator's (>=8 char) credentials.
    log.info("Repointing persist backend endpoint to RustFS...")
    mz.write_text(patch.patch_persist_backend_host(mz.read_text(), "minio", "rustfs-io"))
    mz.write_text(patch.patch_persist_backend_creds(
        mz.read_text(), c.RUSTFS_ACCESS_KEY, c.RUSTFS_SECRET_KEY))

    if cfg.license_key_file:
        key_path = Path(cfg.license_key_file)
        if not key_path.is_file():
            log.error(f"License key file does not exist: {cfg.license_key_file}")
            raise Abort
        log.info(f"Reading license key from file: {cfg.license_key_file}")
        mz.write_text(patch.patch_license_key(mz.read_text(), key_path.read_text()))
        log.info("License key configured successfully.")
    else:
        log.warn("No license key provided. Materialize will run without a "
                 "license key.")


def _ensure_metrics_server(runner: Runner) -> None:
    log.step("Checking for metrics-server...")
    if runner.run(["kubectl", "get", "deployment", "metrics-server", "-n",
                   "kube-system"], check=False, capture=True).ok:
        log.info("metrics-server is already installed in the cluster.")
        return
    log.info("Installing metrics-server...")
    args = ("{--kubelet-insecure-tls,"
            "--kubelet-preferred-address-types=InternalIP,Hostname,ExternalIP}")
    if not runner.run(
        ["helm", "install", "metrics-server", "metrics-server/metrics-server",
         "--namespace", "kube-system", "--set", f"args={args}", "--wait"],
        check=False,
    ).ok:
        log.warn("metrics-server installation failed. Pod metrics may not be "
                 "available.")


def operator_chart_versions(runner: Runner) -> list[str]:
    """Available materialize-operator chart versions, newest first."""
    res = runner.run(
        ["helm", "search", "repo", "materialize/materialize-operator",
         "--versions", "-o", "json"],
        check=False, capture=True,
    )
    if not res.ok:
        return []
    try:
        return [e["version"] for e in json.loads(res.stdout) if e.get("version")]
    except (json.JSONDecodeError, TypeError):
        return []


def ensure_operator_chart_version(runner: Runner, cfg: Config) -> None:
    """Resolve cfg.operator_version to a chart version that actually exists.

    The operator Helm chart is versioned independently of the environmentd
    image, so an image version like v26.30.0 may have no matching chart (the
    repo may only have v26.30.1). Use the exact version if present, otherwise
    fall back to the latest available chart and warn.
    """
    versions = operator_chart_versions(runner)
    if not versions or cfg.operator_version in versions:
        return
    latest = versions[0]
    log.warn(f"Operator chart version '{cfg.operator_version}' not found in the "
             f"materialize helm repo.")
    log.warn(f"Using latest available operator chart: {latest} "
             f"(pin with -o/--operator-version to override).")
    cfg.operator_version = latest


def _current_operator_version(runner: Runner, cfg: Config) -> str:
    res = runner.run(["helm", "list", "-n", cfg.namespace, "-o", "json"],
                     check=False, capture=True)
    if not res.ok:
        return ""
    try:
        for rel in json.loads(res.stdout):
            if rel.get("name") == cfg.release_name:
                return rel.get("chart", "").replace("materialize-operator-", "")
    except json.JSONDecodeError:
        pass
    return ""


def _install_or_skip_operator(runner: Runner, cfg: Config) -> None:
    ensure_operator_chart_version(runner, cfg)
    if _current_operator_version(runner, cfg) == cfg.operator_version:
        log.info(f"Materialize Operator already at version {cfg.operator_version}. "
                 "Skipping.")
        return
    log.step("Installing Materialize Operator...")
    runner.run(c.operator_install(cfg))
    log.info("Waiting for Materialize Operator to be ready...")
    runner.run(["kubectl", "wait", "--for=condition=available", "--timeout=300s",
                f"deployment/{cfg.release_name}", "-n", cfg.namespace], check=False)


def _current_instance_image(runner: Runner, cfg: Config) -> str:
    res = runner.run(
        ["kubectl", "get", "materialize", "-n", cfg.instance_ns, "-o",
         "jsonpath={.items[0].spec.environmentdImageRef}"],
        check=False, capture=True,
    )
    return res.stdout if res.ok else ""


def _wait_environmentd_ready(runner: Runner, cfg: Config) -> None:
    """Wait for the environmentd StatefulSet pod to appear, then to be Ready.

    `kubectl wait` errors immediately if nothing matches yet, so poll for the
    pod to show up first (the operator creates it a beat after the CR is
    applied/patched).
    """
    if not runner.dry_run:
        time.sleep(10)
    for _ in range(30):
        pods = runner.run(c.get_environmentd_pods(cfg.instance_ns),
                          check=False, capture=True)
        if pods.stdout.strip():
            break
        if runner.dry_run:
            break
        time.sleep(5)
    else:
        log.warn(f"environmentd pod never appeared. Check: kubectl get pods "
                 f"-n {cfg.instance_ns}")
        return
    if not runner.run(c.wait_environmentd(cfg.instance_ns), check=False).ok:
        log.warn(f"Timeout waiting for environmentd to be Ready. Check: "
                 f"kubectl get pods -n {cfg.instance_ns}")


def _deploy_or_skip_instance(runner: Runner, cfg: Config) -> None:
    target = f"materialize/environmentd:{cfg.version}"
    if _current_instance_image(runner, cfg) == target:
        log.info(f"Materialize instance already at version {cfg.version}. Skipping.")
        return
    log.step("Deploying Materialize instance...")
    apply_manifest(runner, "sample-materialize.yaml")
    log.info("Waiting for Materialize instance to be ready...")
    _wait_environmentd_ready(runner, cfg)


# --- upgrade sub-steps ---------------------------------------------------

def _instance_name(runner: Runner, cfg: Config) -> str:
    res = runner.run(
        ["kubectl", "get", "materialize", "-n", cfg.instance_ns, "-o",
         "jsonpath={.items[0].metadata.name}"],
        check=False, capture=True,
    )
    return res.stdout if res.ok else ""


def _upgrade_instance(runner: Runner, cfg: Config, instance: str) -> None:
    log.step(f"Step 4: Upgrading Materialize instance {instance}...")
    log.info(f"Target image: materialize/environmentd:{cfg.version}")
    log.info("Staging version change...")
    runner.run(c.patch_instance_image(instance, cfg.instance_ns, cfg.version))

    log.step("Step 5: Triggering rollout...")
    rollout = str(uuid.uuid4()) if runner.dry_run else _uuidgen(runner)
    log.info(f"Rollout UUID: {rollout}")
    if cfg.force:
        log.warn("Force rollout enabled - setting forceRollout")
    runner.run(c.patch_rollout(instance, cfg.instance_ns, rollout, force=cfg.force))

    log.info("Rollout triggered. Waiting for new pods to be ready...")
    _wait_environmentd_ready(runner, cfg)


def _uuidgen(runner: Runner) -> str:
    res = runner.run(["uuidgen"], capture=True)
    return res.stdout.strip()


# --- monitoring ----------------------------------------------------------

def _fetch_monitoring_charts(runner: Runner) -> Path:
    """Download and extract the upstream monitoring chart tag; return its
    `charts/` directory."""
    src = Path("materialize-monitoring-src")
    if runner.dry_run:
        return src / "charts"

    tarball = "materialize-monitoring.tar.gz"
    log.info(f"Downloading materialize-monitoring {c.MONITORING_VERSION}...")
    download(c.monitoring_tarball_url(), tarball)
    src.mkdir(exist_ok=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(src, filter="data")
    # The tarball extracts to a single top-level directory.
    root = next(p for p in src.iterdir() if p.is_dir())
    return root / "charts"


# Values overlay for the umbrella chart. Enables Thanos as the metrics store,
# backed by the in-cluster RustFS S3 (a `thanos` bucket) so no external object
# storage is needed on local k3d. Store-gateway/compactor are left off (they
# only add value with long-term object storage); receive+query cover recent
# data. Also carries the grafana-operator CRD + bundled-Grafana auth fixes.
_MONITORING_OVERLAY = """\
loki:
  enabled: false
grafana-operator:
  crds:
    immutable: true
connections:
  grafana:
    external:
      url: http://mz-monitoring-grafana.monitoring.svc.cluster.local:80
      adminUser:
        name: mz-monitoring-grafana
        key: admin-user
      adminPassword:
        name: mz-monitoring-grafana
        key: admin-password
thanos:
  enabled: true
  storegateway:
    enabled: false
  compactor:
    enabled: false
  queryFrontend:
    enabled: false
  ruler:
    enabled: false
  global:
    objstore:
      createSecret: true
      secretName: thanos-objstore-config
      secretKey: objstore.yml
      config: |
        type: S3
        config:
          bucket: thanos
          endpoint: rustfs-io.materialize.svc.cluster.local:9000
          access_key: minioadmin
          secret_key: minioadmin
          insecure: true
"""

# The chart ships no Grafana datasource and its dashboards use a
# `metricsDatasource` Prometheus variable that defaults to the default
# datasource — so provide one pointing at thanos-query.
_GRAFANA_DATASOURCE = """\
apiVersion: grafana.integreatly.org/v1beta1
kind: GrafanaDatasource
metadata:
  name: mzmon-thanos
  namespace: monitoring
spec:
  allowCrossNamespaceImport: false
  instanceSelector:
    matchLabels: {}
  datasource:
    name: Thanos
    type: prometheus
    access: proxy
    url: http://thanos-query.monitoring.svc.cluster.local:9090
    isDefault: true
    jsonData:
      timeInterval: "30s"
"""


def _mz_base_metrics_path(version: str) -> str:
    """environmentd's general metrics endpoint. `/metrics/public` exists from
    v26.25; older releases only expose `/metrics`."""
    m = re.match(r"v?(\d+)\.(\d+)", version)
    if m and (int(m.group(1)), int(m.group(2))) < (26, 25):
        return "/metrics"
    return "/metrics/public"


# environmentd exposes several metric families on separate paths (declared via
# materialize.prometheus.io/* pod annotations). The dashboards' variables depend
# on compute metrics (mz_compute_*), so scrape them all — not just the base set.
_MZ_METRIC_PATHS = ["/metrics/mz_compute", "/metrics/mz_frontier",
                    "/metrics/mz_storage", "/metrics/mz_usage"]


def _materialize_podmonitor(instance_ns: str, base_path: str) -> str:
    # Relabel the operator's pod labels into metric labels the dashboards
    # select on (e.g. the `metricsNamespace` variable is
    # label_values(mz_compute_commands_total, materialize_cloud_organization_namespace)).
    relabel = (
        "      relabelings:\n"
        "        - sourceLabels: "
        "[__meta_kubernetes_pod_label_materialize_cloud_organization_namespace]\n"
        "          targetLabel: materialize_cloud_organization_namespace\n"
        "        - sourceLabels: "
        "[__meta_kubernetes_pod_label_materialize_cloud_organization_name]\n"
        "          targetLabel: materialize_cloud_organization_name")
    endpoints = "".join(
        f"    - targetPort: 6878\n      path: {p}\n      interval: 30s\n{relabel}\n"
        for p in [base_path, *_MZ_METRIC_PATHS]
    )
    return f"""\
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: materialize
  namespace: {instance_ns}
  labels:
    app.kubernetes.io/part-of: materialize
spec:
  namespaceSelector:
    matchNames: [{instance_ns}]
  selector:
    matchLabels:
      materialize.cloud/app: environmentd
  podMetricsEndpoints:
{endpoints}"""


def _kubelet_cadvisor_manifests(node_ips: list[str]) -> str:
    """RBAC + kubelet Service/Endpoints + ServiceMonitor so alloy-gateway
    scrapes container metrics from each node's kubelet /metrics/cadvisor.

    The chart doesn't scrape the kubelet, and without a running
    prometheus-operator nothing maintains kubelet Endpoints, so we point them at
    the node IPs ourselves. Auth uses alloy-gateway's own service-account token
    (granted nodes/metrics below); TLS to the kubelet is skipped (self-signed).
    """
    addresses = "\n".join(f"  - ip: {ip}" for ip in node_ips)
    return f"""\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: mzk3-kubelet-scrape
rules:
- apiGroups: [""]
  resources: ["nodes/metrics", "nodes/proxy", "nodes"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mzk3-kubelet-scrape
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: mzk3-kubelet-scrape
subjects:
- kind: ServiceAccount
  name: alloy-gateway
  namespace: monitoring
---
apiVersion: v1
kind: Service
metadata:
  name: kubelet
  namespace: kube-system
  labels:
    mzk3.io/scrape: kubelet
spec:
  clusterIP: None
  ports:
  - name: https-metrics
    port: 10250
    protocol: TCP
---
apiVersion: v1
kind: Endpoints
metadata:
  name: kubelet
  namespace: kube-system
  labels:
    mzk3.io/scrape: kubelet
subsets:
- addresses:
{addresses}
  ports:
  - name: https-metrics
    port: 10250
    protocol: TCP
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: kubelet-cadvisor
  namespace: monitoring
  labels:
    mzk3.io/scrape: kubelet
spec:
  namespaceSelector:
    matchNames: [kube-system]
  selector:
    matchLabels:
      mzk3.io/scrape: kubelet
  endpoints:
  - port: https-metrics
    scheme: https
    path: /metrics/cadvisor
    interval: 30s
    honorLabels: true
    tlsConfig:
      insecureSkipVerify: true
    bearerTokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
    relabelings:
    - action: replace
      targetLabel: job
      replacement: kubelet-cadvisor
"""


def _node_internal_ips(runner: Runner) -> list[str]:
    res = runner.run(
        ["kubectl", "get", "nodes", "-o",
         "jsonpath={.items[*].status.addresses[?(@.type=='InternalIP')].address}"],
        check=False, capture=True,
    )
    return res.stdout.split() if res.ok else []


def install_monitoring(runner: Runner, cfg: Config) -> None:
    log.step("Installing upstream Materialize monitoring stack "
             "(materialize-monitoring)...")
    ensure_namespace(runner, "monitoring")

    charts = _fetch_monitoring_charts(runner)

    # CRDs first (grafana-operator / prometheus-operator CRDs).
    log.info("Installing monitoring CRDs...")
    runner.run([
        "helm", "upgrade", "--install", "materialize-monitoring-crds",
        str(charts / "materialize-monitoring-crds"),
        "--namespace", "monitoring", "--wait", "--timeout", "5m",
    ])

    Path("monitoring-values.yaml").write_text(_MONITORING_OVERLAY)
    log.info("Installing materialize-monitoring stack "
             "(Thanos on RustFS; this may take several minutes)...")
    runner.run([
        "helm", "upgrade", "--install", "mz-monitoring",
        str(charts / "materialize-monitoring"),
        "--namespace", "monitoring",
        "-f", "monitoring-values.yaml",
        "--wait", "--timeout", "15m",
    ])

    # The chart delivers no Grafana datasource and never scrapes Materialize
    # (v0.6.0 leaves both unimplemented), so provide them ourselves.
    log.info("Provisioning Thanos datasource and Materialize scrape target...")
    runner.run(["kubectl", "apply", "-f", "-"], text_input=_GRAFANA_DATASOURCE)
    runner.run(["kubectl", "apply", "-f", "-"],
               text_input=_materialize_podmonitor(cfg.instance_ns,
                                                  _mz_base_metrics_path(cfg.version)))

    # Container CPU/memory metrics from each node's kubelet (the chart doesn't
    # scrape the kubelet).
    node_ips = _node_internal_ips(runner)
    if node_ips:
        log.info("Provisioning kubelet cAdvisor scrape...")
        runner.run(["kubectl", "apply", "-f", "-"],
                   text_input=_kubelet_cadvisor_manifests(node_ips))
    elif not runner.dry_run:
        log.warn("Could not determine node IPs; skipping cAdvisor scrape.")

    log.info("Monitoring stack installed successfully.")


# --- success banners -----------------------------------------------------

def _print_success_install(cfg: Config) -> None:
    print()
    log.info("=" * 46)
    log.info(" Materialize Self-Managed Setup Complete!")
    log.info("=" * 46)
    log.info("Check instance status:")
    log.info("  mzk3 status")
    log.info("Access the console:")
    log.info(f"  MZ_CONSOLE=$(kubectl -n {cfg.instance_ns} get svc -o name | "
             "grep console)")
    log.info(f"  kubectl port-forward $MZ_CONSOLE 8080:8080 -n {cfg.instance_ns}")
    if cfg.install_dashboards:
        log.info("Access Grafana (materialize-monitoring):")
        log.info("  GRAFANA=$(kubectl -n monitoring get svc -o name | grep grafana | "
                 "head -1)")
        log.info("  kubectl port-forward -n monitoring $GRAFANA 3000:80")


# --- dispatch ------------------------------------------------------------

def cmd_help(runner: Runner, cfg: Config) -> None:
    build_parser().print_help()


_DISPATCH = {
    "install": cmd_install,
    "upgrade": cmd_upgrade,
    "reset": cmd_reset,
    "create-cluster": cmd_create_cluster,
    "list-versions": cmd_list_versions,
    "status": cmd_status,
    "help": cmd_help,
}


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    command, cfg = resolve(argv if argv is not None else sys.argv[1:])
    if runner is None:
        runner = Runner()
    try:
        _DISPATCH[command](runner, cfg)
    except Abort:
        return 1
    except CommandError as e:
        log.error(str(e))
        return 1
    except KeyboardInterrupt:
        log.warn("Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
