"""Command-line entry point: orchestrates the tested core against a live system."""

from __future__ import annotations

import json
import sys
import tarfile
import time
import urllib.request
import uuid
from importlib import resources
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

    log.step("Deploying PostgreSQL backend...")
    apply_manifest(runner, "sample-postgres.yaml")
    _deploy_rustfs(runner)

    _ensure_metrics_server(runner)

    log.step("Waiting for backends to be ready...")
    for dep in ("postgres", "rustfs"):
        runner.run(["kubectl", "wait", "--for=condition=available", "--timeout=300s",
                    f"deployment/{dep}", "-n", "materialize"], check=False)
    log.info("Waiting for RustFS buckets to be created...")
    runner.run(["kubectl", "wait", "--for=condition=complete", "--timeout=300s",
                "job/rustfs-createbuckets", "-n", "materialize"], check=False)

    _install_or_skip_operator(runner, cfg)
    _deploy_or_skip_instance(runner, cfg)

    if cfg.install_dashboards:
        install_monitoring(runner)

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


def _deploy_rustfs(runner: Runner) -> None:
    log.step("Deploying RustFS storage backend...")
    rustfs = resources.files("mzk3.data") / "rustfs.yaml"
    Path("rustfs.yaml").write_text(rustfs.read_text())
    apply_manifest(runner, "rustfs.yaml")


def _patch_configs(runner: Runner, cfg: Config) -> None:
    from . import patch

    log.info("Patching configuration for K3s...")
    values = Path("sample-values-k3s.yaml")
    values.write_text(patch.patch_region(values.read_text()))

    log.info(f"Patching Materialize CR with version {cfg.version}...")
    mz = Path("sample-materialize.yaml")
    mz.write_text(patch.patch_environmentd_image(mz.read_text(), cfg.version))

    # The upstream Materialize CR ships pointing at a `minio` service; repoint
    # persist at our RustFS service.
    log.info("Repointing persist backend endpoint to RustFS...")
    mz.write_text(patch.patch_persist_backend_host(mz.read_text(), "minio", "rustfs"))

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


def install_monitoring(runner: Runner) -> None:
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

    # Umbrella stack. Disable Loki/Thanos (their `.enabled` conditions override
    # the group tags), so no object storage is needed — suitable for local k3d.
    log.info("Installing materialize-monitoring stack "
             "(this may take several minutes)...")
    runner.run([
        "helm", "upgrade", "--install", "mz-monitoring",
        str(charts / "materialize-monitoring"),
        "--namespace", "monitoring",
        "--set", "thanos.enabled=false",
        "--set", "loki.enabled=false",
        # Install grafana-operator CRDs from the chart's crds/ dir (applied
        # before templates) so the Grafana CRs in this same release can be
        # mapped on a fresh cluster. With the chart default (false) the CRDs
        # land in templates/ and a first-time install fails.
        "--set", "grafana-operator.crds.immutable=true",
        # v0.6.0 bug: in bundled mode the Grafana CR points at a creds secret
        # the chart never creates (`<fullname>-grafana-admin-credentials`) and
        # a URL using the wrong (fullname-based) host, so grafana-operator
        # can't authenticate to the bundled Grafana. Point it at the real
        # subchart service + admin secret.
        "--set", "connections.grafana.external.url="
                 "http://mz-monitoring-grafana.monitoring.svc.cluster.local:80",
        "--set", "connections.grafana.external.adminUser.name=mz-monitoring-grafana",
        "--set", "connections.grafana.external.adminUser.key=admin-user",
        "--set", "connections.grafana.external.adminPassword.name=mz-monitoring-grafana",
        "--set", "connections.grafana.external.adminPassword.key=admin-password",
        "--wait", "--timeout", "15m",
    ])

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
