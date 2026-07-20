"""Configuration: resolve defaults < environment variables < command-line flags."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_VERSION = "v26.4.0"

MZK3_LABEL = "created-by=mzk3"

# Commands that actually do work, paired with a one-line description.
_COMMAND_HELP = {
    "install": "Install Materialize (first-time setup)",
    "upgrade": "Upgrade an existing Materialize installation",
    "create-cluster": "Create a new k3d cluster for Materialize",
    "reset": "Destroy and recreate the k3d cluster (WARNING: destroys all data)",
    "list-versions": "List available Materialize operator versions",
    "status": "Show current deployment status",
    "help": "Show this help message",
}

COMMANDS = tuple(_COMMAND_HELP)

_COMMANDS_BLOCK = "\n".join(
    f"    {name:<15} {desc}" for name, desc in _COMMAND_HELP.items()
)

_EPILOG = f"""\
Commands:
{_COMMANDS_BLOCK}

Environment variables (used as defaults; flags override):
    MZ_VERSION           Materialize version
    MZ_OPERATOR_VERSION  Operator version (default: same as MZ_VERSION)
    MZ_LICENSE_KEY       Path to license key file
    K3D_CLUSTER_NAME     k3d cluster name
    MZ_NAMESPACE         Operator namespace
    MZ_RELEASE_NAME      Helm release name
    MZ_INSTANCE_NS       Materialize instance namespace
    MZ_VALUES_FILE       Path to values file
    MZ_SKIP_CONFIRM      Skip confirmation prompts ("true"/"false")
    MZ_INSTALL_DASHBOARDS  Install monitoring stack ("true"/"false")

Examples:
    mzk3 create-cluster                     # create a k3d cluster
    mzk3 create-cluster -c my-cluster       # ...with a custom name
    mzk3 install --create-cluster           # create cluster and install together
    mzk3 install -c my-cluster              # install on an existing mzk3 cluster
    mzk3 install -v {DEFAULT_VERSION} -l ./license.key
    mzk3 install --install-dashboards       # include Prometheus + Grafana
    mzk3 install --force                    # install on any cluster (skip safety check)
    mzk3 upgrade -v v26.5.0 --yes           # upgrade, no prompt
    mzk3 upgrade -v v26.5.0 --force         # force a rollout
    mzk3 reset --yes                        # destroy + recreate, no prompt
    mzk3 status
    mzk3 list-versions

Cluster safety:
    install only installs on clusters it created (identified by a
    '{MZK3_LABEL}' label). Use --create-cluster, or run create-cluster first.
    Use --force to bypass this check and install on any cluster.
"""


@dataclass
class Config:
    version: str
    operator_version: str
    license_key_file: str | None
    namespace: str
    release_name: str
    instance_ns: str
    values_file: str
    cluster_name: str
    skip_confirm: bool
    force: bool
    install_dashboards: bool
    create_cluster: bool
    storage_backend: str


def _env_bool(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "false") == "true"


def build_parser(env: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    """Argument parser whose defaults are seeded from the environment.

    Precedence falls out naturally: unset flags fall back to the env-derived
    default, which itself falls back to the hard-coded default.
    """
    env = os.environ if env is None else env

    p = argparse.ArgumentParser(
        prog="mzk3",
        description="Deploy and manage Materialize on a K3s/k3d Kubernetes cluster.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,  # add our own so we can group it with the flags nicely
    )
    p.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        default="help",
        metavar="COMMAND",
        help="one of the commands listed below (omit to show this help)",
    )
    p.add_argument("-v", "--version", dest="version",
                   default=env.get("MZ_VERSION", DEFAULT_VERSION),
                   metavar="VERSION",
                   help="Materialize version to install/upgrade to "
                        f"(default: {DEFAULT_VERSION})")
    # None marker so we can fall back to `version` after parsing.
    p.add_argument("-o", "--operator-version", dest="operator_version",
                   default=env.get("MZ_OPERATOR_VERSION"),
                   metavar="VER",
                   help="operator chart version (default: same as --version)")
    p.add_argument("-l", "--license-key", dest="license_key_file",
                   default=env.get("MZ_LICENSE_KEY") or None,
                   metavar="FILE",
                   help="path to a file containing the Materialize license key")
    p.add_argument("-c", "--cluster", dest="cluster_name",
                   default=env.get("K3D_CLUSTER_NAME", "mzk3-cluster"),
                   metavar="NAME",
                   help="k3d cluster name (default: mzk3-cluster)")
    p.add_argument("-n", "--namespace", dest="namespace",
                   default=env.get("MZ_NAMESPACE", "materialize"),
                   metavar="NS",
                   help="operator namespace (default: materialize)")
    p.add_argument("-r", "--release-name", dest="release_name",
                   default=env.get("MZ_RELEASE_NAME", "my-materialize-operator"),
                   metavar="NAME",
                   help="Helm release name (default: my-materialize-operator)")
    p.add_argument("-i", "--instance-ns", dest="instance_ns",
                   default=env.get("MZ_INSTANCE_NS", "materialize-environment"),
                   metavar="NS",
                   help="Materialize instance namespace "
                        "(default: materialize-environment)")
    p.add_argument("-f", "--values-file", dest="values_file",
                   default=env.get("MZ_VALUES_FILE", "sample-values-k3s.yaml"),
                   metavar="FILE",
                   help="Helm values file (default: sample-values-k3s.yaml)")
    p.add_argument("-y", "--yes", dest="skip_confirm", action="store_true",
                   default=_env_bool(env, "MZ_SKIP_CONFIRM"),
                   help="Skip confirmation prompts")
    p.add_argument("--force", dest="force", action="store_true", default=False,
                   help="force operation (upgrade: forceRollout; "
                        "install: bypass the mzk3-cluster safety check)")
    p.add_argument("--install-dashboards", dest="install_dashboards",
                   action="store_true", default=_env_bool(env, "MZ_INSTALL_DASHBOARDS"),
                   help="install the Prometheus + Grafana monitoring stack")
    p.add_argument("--create-cluster", dest="create_cluster",
                   action="store_true", default=False,
                   help="create the k3d cluster before installing (install only)")
    p.add_argument("--storage-backend", dest="storage_backend",
                   choices=("minio", "rustfs"),
                   default=env.get("MZ_STORAGE_BACKEND", "minio"),
                   help="S3-compatible blob storage backend (default: minio)")
    p.add_argument("-h", "--help", action="help",
                   help="show this help message and exit")
    return p


def resolve(argv: list[str], env: Mapping[str, str] | None = None) -> tuple[str, Config]:
    """Parse argv into (command, Config), applying defaults < env < flags."""
    ns = build_parser(env).parse_args(argv)

    # Operator version tracks the materialize version unless set explicitly
    # (by flag or MZ_OPERATOR_VERSION env var).
    operator_version = ns.operator_version or ns.version

    cfg = Config(
        version=ns.version,
        operator_version=operator_version,
        license_key_file=ns.license_key_file,
        namespace=ns.namespace,
        release_name=ns.release_name,
        instance_ns=ns.instance_ns,
        values_file=ns.values_file,
        cluster_name=ns.cluster_name,
        skip_confirm=ns.skip_confirm,
        force=ns.force,
        install_dashboards=ns.install_dashboards,
        create_cluster=ns.create_cluster,
        storage_backend=ns.storage_backend,
    )
    return ns.command, cfg
