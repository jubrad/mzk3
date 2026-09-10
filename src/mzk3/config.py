"""Configuration: resolve defaults < environment variables < command-line flags."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_VERSION = "v26.40.0"

MZK3_LABEL = "created-by=mzk3"

# Per-component resource requests/limits. Overridable via the JSON config file's
# `resources` object (deep-merged over these defaults).
DEFAULT_RESOURCES: dict[str, dict[str, str]] = {
    "environmentd": {"cpu": "2", "memory": "4Gi"},
    # 1Gi was far too low: RustFS got OOMKilled under persist load, and kills
    # mid-write corrupted the persist rollup (environmentd then crash-loops
    # decoding it). OOM here is catastrophic, so default to generous headroom;
    # override down via the config file on smaller machines.
    "rustfs": {"cpu": "1", "memory": "8Gi"},
    # Optional demo components (--demo). Small: they hold a handful of rows.
    "demo-postgres": {"cpu": "500m", "memory": "512Mi"},
    "demo-mysql": {"cpu": "1", "memory": "1Gi"},
    "demo-kafka": {"cpu": "1", "memory": "1Gi"},
    # Polaris is a JVM; its Postgres metastore gets its own small container.
    "demo-iceberg": {"cpu": "1", "memory": "1536Mi"},
    "demo-iceberg-db": {"cpu": "250m", "memory": "256Mi"},
}

# Optional demo infrastructure that can be deployed alongside Materialize so a
# fresh install has realistic upstream systems (and an Iceberg catalog) to point
# sources and sinks at. See demo.py.
DEMO_COMPONENTS = ("postgres", "mysql", "kafka", "iceberg")

# Commands that actually do work, paired with a one-line description.
_COMMAND_HELP = {
    "install": "Install Materialize (first-time setup)",
    "upgrade": "Upgrade an existing Materialize installation",
    "create-cluster": "Create a new k3d cluster for Materialize",
    "destroy-environment": "Tear down the Materialize environment (instance + its "
                           "state), keeping the cluster/operator/backends",
    "recreate-environment": "Destroy then recreate the Materialize environment",
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
    MZ_DEMO              Demo components to deploy, comma-separated
                         (postgres,mysql,kafka,iceberg or "all")
    MZ_CONFIG            Path to a JSON config file

Config file (--config / MZ_CONFIG):
    A JSON file of settings and per-component resource limits. Precedence is
    flags > config file > environment > defaults. Example:
      {{
        "version": "v26.30.0",
        "install_dashboards": true,
        "demo": ["postgres", "kafka"],
        "resources": {{
          "environmentd": {{"cpu": "2", "memory": "4Gi"}},
          "rustfs":       {{"cpu": "1", "memory": "1Gi"}}
        }}
      }}

License key:
    Self-managed Materialize v26+ requires a license key; install and
    recreate-environment refuse to run without one (-l FILE, MZ_LICENSE_KEY,
    or "license_key" in the config file). Free Community keys:
    https://materialize.com/self-managed/community-license/

Examples:
    mzk3 create-cluster                     # create a k3d cluster
    mzk3 create-cluster -c my-cluster       # ...with a custom name
    mzk3 install --create-cluster -l ./license.key   # create cluster + install
    mzk3 install -c my-cluster -l ./license.key      # on an existing mzk3 cluster
    mzk3 install -v {DEFAULT_VERSION} -l ./license.key
    mzk3 install -l ./license.key --install-dashboards  # + Prometheus/Grafana
    mzk3 install -l ./license.key --demo postgres --demo kafka  # + demo sources
    mzk3 install -l ./license.key --demo all  # postgres, mysql, kafka + Iceberg
    mzk3 install -l ./license.key --force   # install on any cluster (skip safety check)
    mzk3 upgrade -v v26.5.0 --yes           # upgrade, no prompt
    mzk3 upgrade -v v26.5.0 --force         # force a rollout
    mzk3 upgrade --component demo           # re-apply the demo manifests
    mzk3 reset --yes                        # destroy + recreate, no prompt
    mzk3 status
    mzk3 list-versions

Demo components (--demo / MZ_DEMO / config "demo"):
    postgres   PostgreSQL 16 with logical replication + a seeded schema
    mysql      MySQL 8.4 with GTID binlog replication + a seeded schema
    kafka      Redpanda (Kafka API + schema registry) with a seeded topic
    iceberg    Apache Polaris Iceberg REST catalog, warehouse on RustFS
    all        everything above
    Each writes a ready-to-run demo-<name>.sql with the matching Materialize
    CREATE CONNECTION / SOURCE / SINK statements.

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
    keep_state: bool = False
    preview: bool = False
    local_repo: str | None = None
    environmentd_image: str | None = None
    resources: dict[str, dict[str, str]] = field(default_factory=dict)
    components: list[str] = field(default_factory=list)
    demo: list[str] = field(default_factory=list)


UPGRADE_COMPONENTS = ("materialize", "monitoring", "rustfs", "postgres", "demo")


def normalize_demo(values) -> list[str]:
    """Flatten/validate demo component names from any layer (flag, config file,
    env). Accepts nested lists and comma-separated strings, expands `all`,
    dedupes, and returns them in canonical DEMO_COMPONENTS order."""
    names: list[str] = []

    def walk(v):
        if v is None:
            return
        if isinstance(v, str):
            names.extend(x.strip() for x in v.split(",") if x.strip())
        else:
            for item in v:
                walk(item)

    walk(values)
    if "all" in names:
        return list(DEMO_COMPONENTS)
    unknown = sorted(set(names) - set(DEMO_COMPONENTS))
    if unknown:
        raise ValueError(
            f"unknown demo component(s): {', '.join(unknown)} "
            f"(choose from {', '.join(DEMO_COMPONENTS)}, all)")
    return [c for c in DEMO_COMPONENTS if c in names]


def _demo_arg(value: str) -> list[str]:
    """argparse `type` for --demo: validates and splits `a,b` into a list."""
    try:
        return normalize_demo(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


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
                   help="Materialize version to install/upgrade to, or 'latest' "
                        f"for the newest stable release (default: {DEFAULT_VERSION})")
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
    p.add_argument("--demo", dest="demo", action="extend", type=_demo_arg,
                   default=None, metavar="NAME",
                   help="deploy demo infrastructure alongside Materialize: "
                        "postgres, mysql, kafka (Redpanda), iceberg (Polaris "
                        "catalog on RustFS), or all (repeatable / "
                        "comma-separated)")
    p.add_argument("--create-cluster", dest="create_cluster",
                   action="store_true", default=None,
                   help="create the k3d cluster before installing (install only)")
    p.add_argument("--keep-state", dest="keep_state", action="store_true",
                   default=None,
                   help="destroy/recreate-environment: keep persist + metadata "
                        "instead of wiping them")
    p.add_argument("--component", dest="components", action="append",
                   choices=UPGRADE_COMPONENTS, default=None,
                   help="upgrade: limit to these components (repeatable; "
                        "default: all installed)")
    p.add_argument("--preview", dest="preview", action="store_true", default=None,
                   help="upgrade: print the plan and exit without making changes")
    p.add_argument("--local-repo", dest="local_repo", default=None, metavar="PATH",
                   help="use a local materialize checkout for the operator helm "
                        "chart and testing manifests instead of the published ones")
    p.add_argument("--environmentd-image", dest="environmentd_image", default=None,
                   metavar="REF",
                   help="full environmentd image ref (e.g. myrepo/environmentd:tag); "
                        "clusterd/balancerd/operator images are derived from it")
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
        keep_state=pick_bool(ns.keep_state, "keep_state", None),
        preview=pick_bool(ns.preview, "preview", None),
        local_repo=pick(ns.local_repo, "local_repo", None, None) or None,
        environmentd_image=pick(ns.environmentd_image, "environmentd_image",
                                None, None) or None,
        resources=_merge_resources(fileconf.get("resources")),
        components=(ns.components if ns.components is not None
                    else (fileconf.get("components") or [])),
        demo=_resolve_demo(ns.demo, fileconf.get("demo"), env.get("MZ_DEMO")),
    )
    return ns.command, cfg


def _resolve_demo(flag, fileval, envval) -> list[str]:
    """flag > config file > env, like every other setting; each layer is a
    complete list (they don't accumulate)."""
    if flag is not None:
        return normalize_demo(flag)
    if fileval is not None:
        return normalize_demo(fileval)
    if envval:
        return normalize_demo(envval)
    return []


def _env_bool(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "false") == "true"
