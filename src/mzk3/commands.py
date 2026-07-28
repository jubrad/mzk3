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


# RustFS operator (github.com/rustfs/operator). Deployed from the release tag's
# in-repo helm chart. Operator requires S3 credentials >= 8 chars.
RUSTFS_OPERATOR_VERSION = "0.0.5"
RUSTFS_ACCESS_KEY = "minioadmin"
RUSTFS_SECRET_KEY = "minioadmin"
# The Tenant named `rustfs` exposes its S3 API on service `rustfs-io:9000`.
RUSTFS_S3_HOST = "rustfs-io.materialize.svc.cluster.local"


def rustfs_operator_tarball_url(version: str = RUSTFS_OPERATOR_VERSION) -> str:
    return f"https://codeload.github.com/rustfs/operator/tar.gz/refs/tags/{version}"


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

def environmentd_image_ref(cfg: Config) -> str:
    """The full environmentd image ref: an explicit override, else the stock
    `materialize/environmentd:<version>`."""
    return cfg.environmentd_image or f"materialize/environmentd:{cfg.version}"


def derive_operator_image(env_image: str) -> tuple[str, str]:
    """From an environmentd image ref derive the operator (orchestratord) image
    repo + tag, e.g. `jubrad/environmentd:TAG` -> (`jubrad/orchestratord`, TAG).
    The operator itself derives clusterd/balancerd from the environmentd ref."""
    repo, _, tag = env_image.rpartition(":")
    prefix = repo.rsplit("/", 1)[0] if "/" in repo else ""
    return (f"{prefix}/orchestratord" if prefix else "orchestratord"), tag


def _operator_chart(cfg: Config) -> list[str]:
    """Chart reference for the operator: a local checkout's chart (no --version)
    or the published chart pinned to the operator version."""
    if cfg.local_repo:
        return [f"{cfg.local_repo}/misc/helm-charts/operator"]
    return ["materialize/materialize-operator", "--version", cfg.operator_version]


def _operator_image_sets(cfg: Config) -> list[str]:
    if not cfg.environmentd_image:
        return []
    repo, tag = derive_operator_image(cfg.environmentd_image)
    return ["--set", f"operator.image.repository={repo}",
            "--set", f"operator.image.tag={tag}"]


_OPERATOR_SETS = [
    "--set", "observability.enabled=true",
    "--set", "observability.podMetrics.enabled=true",
    "--set", "observability.prometheus.scrapeAnnotations.enabled=true",
    "--set", "operator.cloudProvider.region=k3s",
]


def operator_install(cfg: Config) -> list[str]:
    return (
        ["helm", "upgrade", "--install", cfg.release_name]
        + _operator_chart(cfg)
        + [f"--namespace={cfg.namespace}", "--create-namespace"]
        + _OPERATOR_SETS + _operator_image_sets(cfg)
        + ["-f", cfg.values_file, "--wait"]
    )


def operator_upgrade(cfg: Config) -> list[str]:
    return (
        ["helm", "upgrade", cfg.release_name]
        + _operator_chart(cfg)
        + ["-n", cfg.namespace]
        + _OPERATOR_SETS + _operator_image_sets(cfg)
        + ["-f", cfg.values_file, "--wait"]
    )


# --- kubectl: materialize instance ---------------------------------------

def patch_instance_image(name: str, instance_ns: str, image_ref: str) -> list[str]:
    payload = {"spec": {"environmentdImageRef": image_ref}}
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
    # forcePromote skips waiting for the new generation to rehydrate before
    # promoting it. Without it the 0dt cutover can hang indefinitely on a small
    # single-node dev instance (operator loops "still initializing"), so the
    # upgrade never actually takes effect. For this dev tool we always promote.
    spec: dict[str, str] = {"requestRollout": uuid, "forcePromote": uuid}
    if force:
        spec["forceRollout"] = uuid
    return [
        "kubectl", "patch", "materialize", name,
        "-n", instance_ns,
        "--type", "merge",
        "-p", json.dumps({"spec": spec}),
    ]
