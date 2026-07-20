"""Live end-to-end test against a real k3d cluster.

Marked `integration` — the default `pytest` run skips these. Run explicitly:

    uv run pytest -m integration

Requires docker/k3d/kubectl/helm on PATH and network access. Creates a
dedicated throwaway cluster and always deletes it on teardown.
"""

import os
import shutil
import subprocess

import pytest

from mzk3.cli import main

CLUSTER = "mzk3-it"
INSTANCE_NS = "materialize-environment"

pytestmark = pytest.mark.integration


def _have_tools() -> bool:
    return all(shutil.which(b) for b in ("k3d", "kubectl", "helm", "docker"))


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Create the cluster, install Materialize, tear the cluster down after."""
    if not _have_tools():
        pytest.skip("k3d/kubectl/helm/docker not all available")

    # Downloads land in cwd (sample-*.yaml, monitoring assets); isolate them.
    workdir = tmp_path_factory.mktemp("mzk3-it")
    prev = os.getcwd()
    os.chdir(workdir)

    # Clean any leftover cluster from a previous aborted run.
    subprocess.run(["k3d", "cluster", "delete", CLUSTER],
                   capture_output=True, check=False)
    try:
        rc = main(["install", "--create-cluster", "-c", CLUSTER])
        yield rc
    finally:
        os.chdir(prev)
        subprocess.run(["k3d", "cluster", "delete", CLUSTER],
                       capture_output=True, check=False)


def _kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True)


def test_install_exits_clean(installed):
    assert installed == 0


def test_cluster_is_labeled_by_mzk3(installed):
    out = subprocess.run(
        ["docker", "inspect", f"k3d-{CLUSTER}-server-0"],
        capture_output=True, text=True,
    ).stdout
    assert '"created-by": "mzk3"' in out


def test_backends_are_available(installed):
    for dep in ("postgres", "minio"):
        res = _kubectl("wait", "--for=condition=available", "--timeout=120s",
                       f"deployment/{dep}", "-n", "materialize")
        assert res.returncode == 0, res.stderr


def test_environmentd_becomes_ready(installed):
    # The instance rollout is the real proof the install worked end to end.
    # environmentd is a StatefulSet; its pods carry materialize.cloud/app.
    res = _kubectl(
        "wait", "--for=condition=Ready", "--timeout=600s",
        "pod", "-l", "materialize.cloud/app=environmentd",
        "-n", INSTANCE_NS,
    )
    assert res.returncode == 0, res.stderr

    pods = _kubectl("get", "pods", "-n", INSTANCE_NS, "-o",
                    "jsonpath={.items[*].metadata.name}").stdout
    assert "environmentd" in pods


def test_status_command_runs_against_live_cluster(installed):
    assert main(["status", "-c", CLUSTER]) == 0
