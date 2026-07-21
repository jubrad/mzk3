"""Orchestration tests: drive whole commands through a dry-run Runner.

No real cluster, no network. The Runner records argv and returns canned output
via `responder`, so we assert on the *sequence* of commands each flow issues.
"""

import json

from mzk3.cli import is_mzk3_cluster, list_mzk3_clusters, main
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
    rc = main(["upgrade", "-v", "v27.0.0", "--yes"], runner=r)
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
