"""Orchestration tests: drive whole commands through a dry-run Runner.

No real cluster, no network. The Runner records argv and returns canned output
via `responder`, so we assert on the *sequence* of commands each flow issues.
"""

import json

from mzk3.cli import (
    _kubelet_cadvisor_manifests,
    _materialize_podmonitor,
    _mz_base_metrics_path,
    _rustfs_tenant,
    ensure_operator_chart_version,
    _upgrade_plan,
    is_mzk3_cluster,
    list_mzk3_clusters,
    main,
    resolve_latest_version,
)
from mzk3.config import resolve
from mzk3.runner import Result, Runner

ALL_BINS = {"kubectl", "helm", "curl", "k3d", "docker", "uuidgen"}


def ran(runner, *prefix):
    """True if some recorded call starts with the given token prefix."""
    return any(call[: len(prefix)] == list(prefix) for call in runner.calls)


# --- cluster label detection ---------------------------------------------

def test_is_mzk3_cluster_true_when_label_present():
    labels = {"Config": {"Labels": {"created-by": "mzk3"}}}

    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([labels]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert is_mzk3_cluster(r, "mzk3-cluster") is True


def test_is_mzk3_cluster_false_for_foreign_cluster():
    labels = {"Config": {"Labels": {"created-by": "someone-else"}}}

    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([labels]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert is_mzk3_cluster(r, "mzk3-cluster") is False


def test_is_mzk3_cluster_false_when_k3d_missing():
    r = Runner(dry_run=True, binaries=set())  # no binaries on PATH
    assert is_mzk3_cluster(r, "mzk3-cluster") is False


def test_list_mzk3_clusters_filters_by_label():
    clusters = [
        {"name": "mine", "serversRunning": 1},
        {"name": "stopped", "serversRunning": 0},
        {"name": "foreign", "serversRunning": 1},
    ]

    def responder(argv):
        if argv[:3] == ["k3d", "cluster", "list"]:
            return Result(0, json.dumps(clusters), "")
        if argv[:2] == ["docker", "inspect"]:
            label = "mzk3" if "k3d-mine-server-0" in argv else "other"
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": label}}}]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert list_mzk3_clusters(r) == ["mine"]


# --- whole-command flows via main() ---------------------------------------

def test_status_reports_empties_and_issues_all_queries():
    r = Runner(dry_run=True, binaries=ALL_BINS)  # empty output -> "No ... found"
    rc = main(["status"], runner=r)
    assert rc == 0
    assert ran(r, "kubectl", "cluster-info")
    assert ran(r, "helm", "list", "-n", "materialize")
    assert ran(r, "kubectl", "get", "materialize", "-A")
    assert ran(r, "kubectl", "get", "pods", "-n", "materialize-environment")
    assert ran(r, "kubectl", "get", "svc", "-n", "materialize-environment")


def test_status_aborts_when_kubectl_missing():
    r = Runner(dry_run=True, binaries={"helm", "curl"})
    assert main(["status"], runner=r) == 1
    assert not ran(r, "helm", "list")  # aborted before querying


def test_create_cluster_creates_labeled_cluster():
    def responder(argv):
        if argv[:3] == ["k3d", "cluster", "list"]:
            return Result(0, "NAME  SERVERS\n", "")  # no existing cluster
        if argv[:3] == ["kubectl", "get", "nodes"]:
            return Result(0, "node Ready", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert main(["create-cluster", "-c", "demo"], runner=r) == 0
    assert ran(r, "k3d", "cluster", "create", "demo")


def test_reset_with_yes_deletes_then_recreates():
    def responder(argv):
        if argv[:3] == ["kubectl", "get", "nodes"]:
            return Result(0, "node Ready", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    rc = main(["reset", "-c", "demo", "--yes"], runner=r)
    assert rc == 0
    # order matters: delete precedes create
    delete_i = r.calls.index(["k3d", "cluster", "delete", "demo"])
    create_i = next(
        i for i, cl in enumerate(r.calls) if cl[:3] == ["k3d", "cluster", "create"]
    )
    assert delete_i < create_i


def test_upgrade_updates_operator_before_instance(tmp_path, monkeypatch):
    # a values file so upgrade doesn't try to download one
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sample-values-k3s.yaml").write_text('region: "k3s"\n')

    def responder(argv):
        if argv[:3] == ["kubectl", "get", "materialize"] and "metadata.name" in argv[-1]:
            return Result(0, "myinst", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    rc = main(["upgrade", "-v", "v27.0.0", "--yes", "--component", "materialize"],
              runner=r)
    assert rc == 0

    def first_index(pred):
        return next(i for i, cl in enumerate(r.calls) if pred(cl))

    repo_add = first_index(lambda c: c[:4] == ["helm", "repo", "add", "materialize"])
    op_upgrade = first_index(
        lambda c: c[:2] == ["helm", "upgrade"] and "materialize/materialize-operator" in c
    )
    instance_patch = first_index(
        lambda c: c[:3] == ["kubectl", "patch", "materialize"]
    )
    # repo ready, then operator upgraded, then the instance touched
    assert repo_add < op_upgrade < instance_patch


def _env_responder(argv):
    # _instance_name lookup returns an instance
    if argv[:3] == ["kubectl", "get", "materialize"] and "metadata.name" in argv[-1]:
        return Result(0, "myinst", "")
    return None


def test_destroy_environment_deletes_cr_and_wipes_state():
    r = Runner(dry_run=True, responder=_env_responder, binaries=ALL_BINS)
    assert main(["destroy-environment", "--yes"], runner=r) == 0
    assert ran(r, "kubectl", "delete", "materialize", "myinst")
    # wipes persist (mc pod) + resets postgres metadata
    assert any(cl[:3] == ["kubectl", "run", "mzk3-wipe-persist"] for cl in r.calls)
    assert any(cl[:3] == ["kubectl", "exec", "-n"] and "deploy/postgres" in cl
               for cl in r.calls)


def test_destroy_environment_keep_state_skips_wipe():
    r = Runner(dry_run=True, responder=_env_responder, binaries=ALL_BINS)
    assert main(["destroy-environment", "--yes", "--keep-state"], runner=r) == 0
    assert ran(r, "kubectl", "delete", "materialize", "myinst")
    assert not any(cl[:3] == ["kubectl", "run", "mzk3-wipe-persist"]
                   for cl in r.calls)
    assert not any("deploy/postgres" in cl for cl in r.calls)


def test_upgrade_plan_describes_selected_components():
    _, cfg = resolve(["upgrade", "-v", "v27.0.0"])
    plan = _upgrade_plan(cfg, ["rustfs", "postgres", "materialize", "monitoring"])
    joined = " ".join(plan)
    assert "RustFS" in joined and "PostgreSQL" in joined
    assert "Materialize" in joined and "v27.0.0" in joined
    assert "Monitoring" in joined
    # postgres line flags the downtime
    assert any("downtime" in line for line in plan)


def test_upgrade_preview_makes_no_changes():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    rc = main(["upgrade", "--preview", "--component", "materialize", "-v", "v27.0.0"],
              runner=r)
    assert rc == 0
    # no mutating operations happened
    assert not any(cl[:2] == ["helm", "upgrade"]
                   and "materialize/materialize-operator" in cl for cl in r.calls)
    assert not ran(r, "kubectl", "patch", "materialize")
    assert not ran(r, "kubectl", "apply", "-f", "sample-postgres.yaml")


def test_upgrade_component_postgres_only():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    rc = main(["upgrade", "--yes", "--component", "postgres"], runner=r)
    assert rc == 0
    # postgres updated (apply + rollout), operator/instance untouched
    assert ran(r, "kubectl", "apply", "-f", "sample-postgres.yaml")
    assert any(cl[:3] == ["kubectl", "rollout", "status"]
               and "deployment/postgres" in cl for cl in r.calls)
    assert not any(cl[:2] == ["helm", "upgrade"]
                   and "materialize/materialize-operator" in cl for cl in r.calls)
    assert not ran(r, "kubectl", "patch", "materialize")


def test_recreate_environment_aborts_on_foreign_cluster():
    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "x"}}}]), "")
        return _env_responder(argv)

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert main(["recreate-environment", "--yes"], runner=r) == 1
    assert not ran(r, "kubectl", "delete", "materialize")  # aborted before touching


def test_install_aborts_on_foreign_cluster_without_force():
    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "x"}}}]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    rc = main(["install", "-c", "foreign"], runner=r)
    assert rc == 1
    # never got as far as touching helm repos / manifests
    assert not ran(r, "kubectl", "apply", "-f", "sample-postgres.yaml")


_OP_VERSIONS = [{"version": v} for v in
                ["v26.33.0", "v26.31.0", "v26.30.1", "v26.29.0"]]


def _op_search_responder(argv):
    if argv[:3] == ["helm", "search", "repo"] and "--versions" in argv:
        return Result(0, json.dumps(_OP_VERSIONS), "")
    return None


def test_resolve_latest_version_picks_newest_stable():
    _, cfg = resolve(["upgrade", "-v", "latest"])
    assert cfg.version == "latest" and cfg.operator_version == "latest"
    r = Runner(dry_run=True, responder=_op_search_responder, binaries=ALL_BINS)
    resolve_latest_version(r, cfg)
    assert cfg.version == "v26.33.0"           # newest from the repo
    assert cfg.operator_version == "v26.33.0"  # tracked latest too


def test_resolve_latest_version_respects_explicit_operator_version():
    _, cfg = resolve(["upgrade", "-v", "latest", "-o", "v26.30.1"])
    r = Runner(dry_run=True, responder=_op_search_responder, binaries=ALL_BINS)
    resolve_latest_version(r, cfg)
    assert cfg.version == "v26.33.0"
    assert cfg.operator_version == "v26.30.1"  # explicit -o not overridden


def test_resolve_latest_version_noop_for_concrete_version():
    _, cfg = resolve(["upgrade", "-v", "v26.30.0"])
    r = Runner(dry_run=True, responder=_op_search_responder, binaries=ALL_BINS)
    resolve_latest_version(r, cfg)
    assert cfg.version == "v26.30.0"  # unchanged, no repo lookup needed


def test_operator_chart_version_falls_back_when_image_version_has_no_chart():
    # image version v26.30.0 exists but the operator chart repo only has v26.30.1
    _, cfg = resolve(["install", "-v", "v26.30.0"])
    r = Runner(dry_run=True, responder=_op_search_responder, binaries=ALL_BINS)
    ensure_operator_chart_version(r, cfg)
    assert cfg.operator_version == "v26.33.0"  # latest available


def test_operator_chart_version_kept_when_it_exists():
    _, cfg = resolve(["install", "-v", "v26.30.1"])
    r = Runner(dry_run=True, responder=_op_search_responder, binaries=ALL_BINS)
    ensure_operator_chart_version(r, cfg)
    assert cfg.operator_version == "v26.30.1"


def test_operator_chart_version_untouched_if_repo_unavailable():
    _, cfg = resolve(["install", "-v", "v26.30.0"])
    r = Runner(dry_run=True, binaries=ALL_BINS)  # no responder -> empty search
    ensure_operator_chart_version(r, cfg)
    assert cfg.operator_version == "v26.30.0"  # no data, don't guess


def test_rustfs_tenant_is_pvc_backed_with_buckets_and_creds():
    _, cfg = resolve(["install"])
    t = _rustfs_tenant(cfg)
    assert "kind: Tenant" in t
    assert "kind: Secret" in t
    # buckets auto-created by the operator
    for b in ("name: bucket", "name: persist", "name: thanos"):
        assert b in t
    # PVC-backed (durable), single-node
    assert "volumeClaimTemplate" in t
    assert "servers: 1" in t
    # creds meet the >=8 char minimum
    assert 'accesskey: "minioadmin"' in t
    # default rustfs resources
    assert 'cpu: "1"' in t


def test_rustfs_tenant_honours_config_resources(tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text('{"resources": {"rustfs": {"cpu": "4", "memory": "8Gi"}}}')
    _, cfg = resolve(["install", "--config", str(conf)])
    t = _rustfs_tenant(cfg)
    assert 'cpu: "4"' in t and 'memory: "8Gi"' in t


def test_mz_base_metrics_path_gated_on_version():
    assert _mz_base_metrics_path("v26.25.0") == "/metrics/public"
    assert _mz_base_metrics_path("v26.30.0") == "/metrics/public"
    assert _mz_base_metrics_path("v26.24.3") == "/metrics"  # pre-26.25
    assert _mz_base_metrics_path("v26.4.0") == "/metrics"
    assert _mz_base_metrics_path("weird") == "/metrics/public"  # default


def test_kubelet_cadvisor_manifests_include_node_ips_and_scrape_config():
    m = _kubelet_cadvisor_manifests(["192.168.0.2", "192.168.0.3"])
    assert "- ip: 192.168.0.2" in m and "- ip: 192.168.0.3" in m
    assert "path: /metrics/cadvisor" in m
    assert "kind: ServiceMonitor" in m
    assert "port: 10250" in m
    assert "insecureSkipVerify: true" in m
    # RBAC binds the scraper SA so the kubelet authorizes the token
    assert "name: alloy-gateway" in m
    assert "nodes/metrics" in m


def test_materialize_podmonitor_scrapes_all_paths_and_relabels_org():
    pm = _materialize_podmonitor("materialize-environment", "/metrics/public")
    assert "kind: PodMonitor" in pm
    assert "namespace: materialize-environment" in pm
    assert "materialize.cloud/app: environmentd" in pm
    # base path + the compute/frontier/storage/usage families
    for p in ("/metrics/public", "/metrics/mz_compute", "/metrics/mz_frontier",
              "/metrics/mz_storage", "/metrics/mz_usage"):
        assert f"path: {p}" in pm
    # org labels relabeled onto metrics (drives the dashboards' namespace var)
    assert "targetLabel: materialize_cloud_organization_namespace" in pm
    assert "targetLabel: materialize_cloud_organization_name" in pm


def test_install_switches_kubectl_context_before_prerequisites():
    # install on an existing cluster must point kubectl at that cluster before
    # any kubectl call, else it talks to whatever context is active.
    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "x"}}}]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    # foreign cluster → aborts right after prereqs, before any downloads
    main(["install", "-c", "mzk3-cluster"], runner=r)

    def idx(pred):
        return next((i for i, cl in enumerate(r.calls) if pred(cl)), None)

    merge = idx(lambda c: c[:3] == ["k3d", "kubeconfig", "merge"]
                and "mzk3-cluster" in c)
    cluster_info = idx(lambda c: c[:2] == ["kubectl", "cluster-info"])
    assert merge is not None, "context was never switched to the target cluster"
    assert cluster_info is not None
    assert merge < cluster_info
