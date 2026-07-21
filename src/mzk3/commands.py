"""Pure argv builders for helm / kubectl / k3d and the download URLs.

None of these touch the system; they assemble the command lines that Runner
later executes. Keeping them pure is what makes them testable.
"""

from __future__ import annotations

import json

from .config import MZK3_LABEL, Config

_RAW = "https://raw.githubusercontent.com/MaterializeInc/materialize"

HELM_REPO = "https://materializeinc.github.io/materialize"

# Upstream monitoring stack (github.com/MaterializeInc/materialize-monitoring).
# Not published to a helm repo yet, so we install from the release tag tarball;
# subchart dependencies are vendored in the tag, so no registry access needed.
MONITORING_VERSION = "v0.6.0"


def monitoring_tarball_url(version: str = MONITORING_VERSION) -> str:
    return (
        "https://codeload.github.com/MaterializeInc/materialize-monitoring/"
        f"tar.gz/refs/tags/materialize-monitoring/{version}"
    )


# --- download URLs -------------------------------------------------------

def operator_values_url(cfg: Config) -> str:
    return f"{_RAW}/{cfg.operator_version}/misc/helm-charts/operator/values.yaml"


def postgres_manifest_url(cfg: Config) -> str:
    return f"{_RAW}/{cfg.version}/misc/helm-charts/testing/postgres.yaml"


def materialize_manifest_url(cfg: Config) -> str:
    return f"{_RAW}/{cfg.version}/misc/helm-charts/testing/materialize.yaml"


# --- k3d -----------------------------------------------------------------

def k3d_create(cluster_name: str) -> list[str]:
    return [
        "k3d", "cluster", "create", cluster_name,
        "--runtime-label", f"{MZK3_LABEL}@server:*",
    ]


# --- helm: operator ------------------------------------------------------

def operator_install(cfg: Config) -> list[str]:
    return [
        "helm", "upgrade", "--install", cfg.release_name,
        "materialize/materialize-operator",
        f"--namespace={cfg.namespace}",
        "--create-namespace",
        "--version", cfg.operator_version,
        "--set", "observability.enabled=true",
        "--set", "observability.podMetrics.enabled=true",
        "--set", "observability.prometheus.scrapeAnnotations.enabled=true",
        "--set", "operator.cloudProvider.region=k3s",
        "-f", cfg.values_file,
        "--wait",
    ]


def operator_upgrade(cfg: Config) -> list[str]:
    argv = [
        "helm", "upgrade", cfg.release_name,
        "materialize/materialize-operator",
        "-n", cfg.namespace,
        "--version", cfg.operator_version,
        "--set", "observability.enabled=true",
        "--set", "observability.podMetrics.enabled=true",
        "--set", "observability.prometheus.scrapeAnnotations.enabled=true",
        "--set", "operator.cloudProvider.region=k3s",
    ]
    argv += ["-f", cfg.values_file, "--wait"]
    return argv


# --- kubectl: materialize instance ---------------------------------------

def patch_instance_image(name: str, instance_ns: str, version: str) -> list[str]:
    payload = {"spec": {"environmentdImageRef": f"materialize/environmentd:{version}"}}
    return [
        "kubectl", "patch", "materialize", name,
        "-n", instance_ns,
        "--type", "merge",
        "-p", json.dumps(payload),
    ]


_ENVIRONMENTD_SELECTOR = "materialize.cloud/app=environmentd"


def get_environmentd_pods(instance_ns: str) -> list[str]:
    """List environmentd pod names (used to poll for the pod appearing)."""
    return [
        "kubectl", "get", "pod",
        "-l", _ENVIRONMENTD_SELECTOR,
        "-n", instance_ns,
        "-o", "name",
    ]


def wait_environmentd(instance_ns: str, timeout: str = "600s") -> list[str]:
    """Wait for the environmentd pod(s) to become Ready.

    environmentd is a StatefulSet, so we wait on pod readiness by the
    operator's `materialize.cloud/app` label rather than a Deployment's
    `available` condition.
    """
    return [
        "kubectl", "wait", "--for=condition=Ready", "pod",
        "-l", _ENVIRONMENTD_SELECTOR,
        "-n", instance_ns,
        f"--timeout={timeout}",
    ]


def patch_rollout(name: str, instance_ns: str, uuid: str, *, force: bool) -> list[str]:
    spec: dict[str, str] = {"requestRollout": uuid}
    if force:
        spec["forceRollout"] = uuid
    return [
        "kubectl", "patch", "materialize", name,
        "-n", instance_ns,
        "--type", "merge",
        "-p", json.dumps({"spec": spec}),
    ]
