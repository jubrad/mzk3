"""Live end-to-end test against a real k3d cluster.

Marked `integration` — the default `pytest` run skips these. Run explicitly:

    uv run pytest --run-integration

Requires docker/k3d/kubectl/helm on PATH and network access. Creates a
throwaway cluster, always deleted on teardown.
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

    # Downloads land in cwd (sample-*.yaml, manifests); isolate them.
    workdir = tmp_path_factory.mktemp("mzk3-it")
    prev = os.getcwd()
    os.chdir(workdir)

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


def test_rustfs_is_the_only_storage_backend(installed):
    assert _kubectl("get", "deployment", "rustfs", "-n", "materialize").returncode == 0
    # MinIO should no longer be deployed at all
    assert _kubectl("get", "deployment", "minio", "-n", "materialize").returncode != 0


def test_rustfs_is_available(installed):
    res = _kubectl("wait", "--for=condition=available", "--timeout=180s",
                   "deployment/rustfs", "-n", "materialize")
    assert res.returncode == 0, res.stderr


def test_environmentd_becomes_ready(installed):
    # environmentd is a StatefulSet; its pods carry materialize.cloud/app.
    res = _kubectl("wait", "--for=condition=Ready", "--timeout=600s",
                   "pod", "-l", "materialize.cloud/app=environmentd",
                   "-n", INSTANCE_NS)
    assert res.returncode == 0, res.stderr

    pods = _kubectl("get", "pods", "-n", INSTANCE_NS, "-o",
                    "jsonpath={.items[*].metadata.name}").stdout
    assert "environmentd" in pods


def test_persist_objects_written_to_rustfs(installed):
    """Prove RustFS is really serving persist: the bucket has objects."""
    host = "rustfs.materialize.svc.cluster.local"
    pod = "mccheck"
    _kubectl("delete", "pod", pod, "--ignore-not-found")
    try:
        _kubectl(
            "run", pod, "--image=minio/mc", "--restart=Never", "--command", "--",
            "sh", "-c",
            f"mc alias set b http://{host}:9000 minio minio123 >/dev/null 2>&1 && "
            f"echo COUNT=$(mc ls -r b/bucket | wc -l)",
        )
        subprocess.run(["kubectl", "wait", "--for=jsonpath={.status.phase}=Succeeded",
                        f"pod/{pod}", "--timeout=60s"], capture_output=True)
        logs = _kubectl("logs", pod).stdout
    finally:
        _kubectl("delete", "pod", pod, "--ignore-not-found")

    count = next((int(line.split("=")[1]) for line in logs.splitlines()
                  if line.startswith("COUNT=")), 0)
    assert count > 0, f"no persist objects found in rustfs bucket; logs:\n{logs}"


def test_status_command_runs_against_live_cluster(installed):
    assert main(["status", "-c", CLUSTER]) == 0
