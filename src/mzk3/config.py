"""Configuration: resolve defaults < environment variables < command-line flags."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_VERSION = "v26.4.0"

MZK3_LABEL = "created-by=mzk3"

# Per-component resource requests/limits. Overridable via the JSON config file's
# `resources` object (deep-merged over these defaults).
DEFAULT_RESOURCES: dict[str, dict[str, str]] = {
    "environmentd": {"cpu": "2", "memory": "4Gi"},
    "rustfs": {"cpu": "1", "memory": "1Gi"},
}

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
    MZ_CONFIG            Path to a JSON config file

Config file (--config / MZ_CONFIG):
    A JSON file of settings and per-component resource limits. Precedence is
    flags > config file > environment > defaults. Example:
      {{
        "version": "v26.30.0",
        "install_dashboards": true,
        "resources": {{
          "environmentd": {{"cpu": "2", "memory": "4Gi"}},
          "rustfs":       {{"cpu": "1", "memory": "1Gi"}}
        }}
      }}

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
    resources: dict[str, dict[str, str]] = field(default_factory=dict)


def _merge_resources(override: Mapping | None) -> dict[str, dict[str, str]]:
    """Deep-merge a config file's `resources` over DEFAULT_RESOURCES."""
    out = {comp: dict(res) for comp, res in DEFAULT_RESOURCES.items()}
    for comp, res in (override or {}).items():
        out.setdefault(comp, {}).update(res or {})
    return out


def build_parser() -> argparse.ArgumentParser:
    """Argument parser. All flag defaults are None so `resolve` can tell an
    explicit flag from an unset one and layer flags > config file > env >
    hard-coded defaults."""
    p = argparse.ArgumentParser(
        prog="mzk3",
        description="Deploy and manage Materialize on a K3s/k3d Kubernetes cluster.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,  # add our own so we can group it with the flags nicely
    )
    p.add_argument("command", nargs="?", choices=COMMANDS, default="help",
                   metavar="COMMAND",
                   help="one of the commands listed below (omit to show this help)")
    p.add_argument("--config", dest="config", default=None, metavar="FILE",
                   help="JSON config file of settings/resource limits "
                        "(overrides env; overridden by explicit flags)")
    p.add_argument("-v", "--version", dest="version", default=None,
                   metavar="VERSION",
                   help="Materialize version to install/upgrade to "
                        f"(default: {DEFAULT_VERSION})")
    p.add_argument("-o", "--operator-version", dest="operator_version",
                   default=None, metavar="VER",
                   help="operator chart version (default: same as --version)")
    p.add_argument("-l", "--license-key", dest="license_key_file", default=None,
                   metavar="FILE",
                   help="path to a file containing the Materialize license key")
    p.add_argument("-c", "--cluster", dest="cluster_name", default=None,
                   metavar="NAME", help="k3d cluster name (default: mzk3-cluster)")
    p.add_argument("-n", "--namespace", dest="namespace", default=None,
                   metavar="NS", help="operator namespace (default: materialize)")
    p.add_argument("-r", "--release-name", dest="release_name", default=None,
                   metavar="NAME",
                   help="Helm release name (default: my-materialize-operator)")
    p.add_argument("-i", "--instance-ns", dest="instance_ns", default=None,
                   metavar="NS",
                   help="Materialize instance namespace "
                        "(default: materialize-environment)")
    p.add_argument("-f", "--values-file", dest="values_file", default=None,
                   metavar="FILE",
                   help="Helm values file (default: sample-values-k3s.yaml)")
    p.add_argument("-y", "--yes", dest="skip_confirm", action="store_true",
                   default=None, help="Skip confirmation prompts")
    p.add_argument("--force", dest="force", action="store_true", default=None,
                   help="force operation (upgrade: forceRollout; "
                        "install: bypass the mzk3-cluster safety check)")
    p.add_argument("--install-dashboards", dest="install_dashboards",
                   action="store_true", default=None,
                   help="install the Prometheus + Grafana monitoring stack")
    p.add_argument("--create-cluster", dest="create_cluster",
                   action="store_true", default=None,
                   help="create the k3d cluster before installing (install only)")
    p.add_argument("-h", "--help", action="help",
                   help="show this help message and exit")
    return p


def _load_config_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def resolve(argv: list[str], env: Mapping[str, str] | None = None) -> tuple[str, Config]:
    """Parse argv into (command, Config).

    Precedence per setting: explicit flag > config file > environment > default.
    """
    env = os.environ if env is None else env
    ns = build_parser().parse_args(argv)

    conf_path = ns.config or (env.get("MZ_CONFIG") or None)
    fileconf = _load_config_file(conf_path) if conf_path else {}

    def pick(flag, key, envvar, default):
        if flag is not None:
            return flag
        if fileconf.get(key) is not None:
            return fileconf[key]
        if envvar and env.get(envvar):
            return env[envvar]
        return default

    def pick_bool(flag, key, envvar, default=False):
        if flag:  # store_true: True if given, else None
            return True
        if fileconf.get(key) is not None:
            return bool(fileconf[key])
        if envvar:
            return _env_bool(env, envvar)
        return default

    version = pick(ns.version, "version", "MZ_VERSION", DEFAULT_VERSION)
    operator_version = pick(ns.operator_version, "operator_version",
                            "MZ_OPERATOR_VERSION", None) or version

    cfg = Config(
        version=version,
        operator_version=operator_version,
        license_key_file=pick(ns.license_key_file, "license_key",
                              "MZ_LICENSE_KEY", None) or None,
        namespace=pick(ns.namespace, "namespace", "MZ_NAMESPACE", "materialize"),
        release_name=pick(ns.release_name, "release_name", "MZ_RELEASE_NAME",
                          "my-materialize-operator"),
        instance_ns=pick(ns.instance_ns, "instance_ns", "MZ_INSTANCE_NS",
                         "materialize-environment"),
        values_file=pick(ns.values_file, "values_file", "MZ_VALUES_FILE",
                         "sample-values-k3s.yaml"),
        cluster_name=pick(ns.cluster_name, "cluster_name", "K3D_CLUSTER_NAME",
                          "mzk3-cluster"),
        skip_confirm=pick_bool(ns.skip_confirm, "skip_confirm", "MZ_SKIP_CONFIRM"),
        force=pick_bool(ns.force, "force", None),
        install_dashboards=pick_bool(ns.install_dashboards, "install_dashboards",
                                     "MZ_INSTALL_DASHBOARDS"),
        create_cluster=pick_bool(ns.create_cluster, "create_cluster", None),
        resources=_merge_resources(fileconf.get("resources")),
    )
    return ns.command, cfg


def _env_bool(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "false") == "true"
