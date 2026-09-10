"""Add-ons (--addon / --install-demo): config resolution, manifest/SQL builders
for the clean and seeded flavors, and the install/upgrade flows that deploy
them (dry-run Runner, no cluster)."""

import json
import re

import pytest

from mzk3 import addons
from mzk3.cli import _upgrade_plan, main
from mzk3.config import ADDONS, normalize_addons, resolve
from mzk3.runner import Result, Runner

ALL_BINS = {"kubectl", "helm", "curl", "k3d", "docker", "uuidgen"}


def ran(runner, *prefix):
    return any(call[: len(prefix)] == list(prefix) for call in runner.calls)


def cfg(*argv, env=None):
    return resolve(list(argv), env=env or {})[1]


# --- config resolution ---------------------------------------------------

def test_addon_flag_repeatable_and_comma_separated_in_canonical_order():
    c = cfg("install", "--addon", "iceberg,postgres", "--addon", "kafka")
    assert c.addons == ["postgres", "kafka", "iceberg"]  # canonical, deduped
    assert c.demo == []  # clean: nothing seeded


def test_install_demo_bare_means_all_and_implies_the_addons():
    c = cfg("install", "--install-demo")
    assert c.addons == list(ADDONS)
    assert c.demo == list(ADDONS)


def test_install_demo_with_names_seeds_only_those():
    c = cfg("install", "--install-demo", "mysql,iceberg", "--addon", "postgres")
    assert c.addons == ["postgres", "mysql", "iceberg"]  # demo implies addon
    assert c.demo == ["mysql", "iceberg"]
    # asking for both flavors of one add-on: the seeded one wins
    c = cfg("install", "--addon", "kafka", "--install-demo", "kafka")
    assert c.addons == ["kafka"] and c.demo == ["kafka"]


def test_addons_default_to_nothing():
    c = cfg("install")
    assert c.addons == [] and c.demo == []


def test_addons_from_env_and_config_file(tmp_path):
    c = cfg("install", env={"MZ_ADDONS": "kafka, mysql", "MZ_INSTALL_DEMO": "iceberg"})
    assert c.addons == ["mysql", "kafka", "iceberg"] and c.demo == ["iceberg"]
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"addons": ["postgres"], "install_demo": ["kafka"]}))
    # config file beats env; flag beats config file
    c = cfg("install", "--config", str(conf), env={"MZ_ADDONS": "iceberg"})
    assert c.addons == ["postgres", "kafka"] and c.demo == ["kafka"]
    c = cfg("install", "--config", str(conf), "--addon", "iceberg", "--install-demo", "mysql")
    assert c.addons == ["mysql", "iceberg"] and c.demo == ["mysql"]


def test_addons_reject_unknown_names():
    with pytest.raises(SystemExit):  # argparse error
        resolve(["install", "--addon", "mongodb"], env={})
    with pytest.raises(SystemExit):
        resolve(["install", "--install-demo", "mongodb"], env={})
    with pytest.raises(ValueError):
        normalize_addons(["postgres", "nope"])


def test_addon_resources_have_defaults_and_are_overridable(tmp_path):
    c = cfg("install")
    assert c.resources["addon-kafka"]["memory"] == "1Gi"
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"resources": {"addon-kafka": {"memory": "2Gi"}}}))
    c = cfg("install", "--config", str(conf))
    assert c.resources["addon-kafka"] == {"cpu": "1", "memory": "2Gi"}


# --- manifest builders ---------------------------------------------------

def _docs(text):
    """(kind, name) pairs of a multi-document YAML string, without a YAML
    dependency."""
    out = []
    for doc in text.split("\n---\n"):
        kind = name = None
        for line in doc.splitlines():
            if line.startswith("kind: "):
                kind = line.split(": ", 1)[1]
            elif line.startswith("  name: ") and name is None:
                name = line.split(": ", 1)[1]
        if kind:
            out.append((kind, name))
    return out


def test_postgres_addon_is_replication_ready_but_empty():
    m = addons.postgres_manifest(cfg("install"), seed=False)
    assert ("Namespace", "addons") in _docs(m)
    assert ("Deployment", "postgres") in _docs(m)
    assert ("PersistentVolumeClaim", "postgres-data") in _docs(m)
    assert "wal_level=logical" in m
    assert f"image: {addons.POSTGRES_IMAGE}" in m
    # replication user + publication are infrastructure, so always present...
    assert "CREATE ROLE materialize WITH LOGIN REPLICATION" in m
    assert "CREATE PUBLICATION mz_source FOR ALL TABLES" in m
    assert "value: app" in m  # the empty database
    # ...but no demo data
    assert "CREATE TABLE orders" not in m and "INSERT INTO" not in m
    assert f'{addons.DEMO_LABEL}: "false"' in m


def test_postgres_demo_flavor_adds_seeded_schema():
    m = addons.postgres_manifest(cfg("install"), seed=True)
    assert "CREATE TABLE orders" in m and "INSERT INTO customers" in m
    assert "REPLICA IDENTITY FULL" in m
    assert f'{addons.DEMO_LABEL}: "true"' in m


def test_mysql_addon_enables_gtid_replication_without_data():
    m = addons.mysql_manifest(cfg("install"), seed=False)
    assert ("Deployment", "mysql") in _docs(m)
    for flag in ("--gtid-mode=ON", "--enforce-gtid-consistency=ON",
                 "--binlog-row-image=FULL", "--binlog-row-metadata=FULL"):
        assert flag in m
    assert "REPLICATION SLAVE, REPLICATION CLIENT" in m
    assert f"image: {addons.MYSQL_IMAGE}" in m
    assert "CREATE TABLE orders" not in m and "INSERT INTO" not in m
    seeded = addons.mysql_manifest(cfg("install"), seed=True)
    assert "CREATE TABLE orders" in seeded and "USE app;" in seeded


def test_kafka_addon_has_no_seed_job_unless_demo():
    clean = addons.kafka_manifest(cfg("install"), seed=False)
    kinds = _docs(clean)
    assert ("Deployment", "redpanda") in kinds
    assert ("Service", "redpanda") in kinds
    assert ("Job", addons.SEED_JOB) not in kinds
    assert "demo_orders" not in clean
    assert "--advertise-kafka-addr=PLAINTEXT://redpanda.addons.svc.cluster.local:9092" in clean
    assert "--schema-registry-addr=0.0.0.0:8081" in clean
    assert "redpanda.enable_transactions=true" in clean
    # redpanda pins 75% of the container limit (1Gi default -> 768M)
    assert "--memory=768M" in clean

    seeded = addons.kafka_manifest(cfg("install"), seed=True)
    assert ("Job", addons.SEED_JOB) in _docs(seeded)
    assert f"topic create {addons.KAFKA_TOPIC}" in seeded
    assert '"customer_id":' in seeded


def test_kafka_manifest_memory_tracks_resource_override(tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"resources": {"addon-kafka": {"memory": "2Gi"}}}))
    m = addons.kafka_manifest(cfg("install", "--config", str(conf)))
    assert "--memory=1536M" in m
    assert 'memory: "2Gi"' in m


def test_memory_mb_parses_kubernetes_quantities():
    assert addons.memory_mb("1Gi") == 1024
    assert addons.memory_mb("1536Mi") == 1536
    assert addons.memory_mb("512Mi") == 512
    assert addons.memory_mb("2G") == 1907
    with pytest.raises(ValueError):
        addons.memory_mb("lots")


def test_iceberg_manifest_runs_polaris_on_rustfs_with_durable_metastore():
    m = addons.iceberg_manifest(cfg("install"))
    kinds = _docs(m)
    assert ("Deployment", "iceberg-catalog") in kinds
    assert ("Deployment", "iceberg-catalog-db") in kinds
    assert ("PersistentVolumeClaim", "iceberg-catalog-db-data") in kinds
    assert ("ConfigMap", "iceberg-catalog-config") in kinds
    assert f"image: {addons.POLARIS_IMAGE}" in m
    assert f"image: {addons.POLARIS_ADMIN_IMAGE}" in m
    # JDBC persistence + realm bootstrap, tolerant of "already bootstrapped"
    assert "relational-jdbc" in m
    assert "bootstrap --realm POLARIS --credential POLARIS,root,root" in m
    assert "[ $rc -eq 3 ]" in m
    # http S3 endpoint needs the insecure-storage feature flag
    assert 'polaris.features."ALLOW_INSECURE_STORAGE_TYPES"=true' in m
    assert "mountPath: /deployments/config" in m
    # warehouse bucket on the in-cluster RustFS gets created up front
    assert "mc mb --ignore-existing r/iceberg" in m
    assert "rustfs-io.materialize.svc.cluster.local:9000" in m
    # the catalog/namespace the setup sidecar maintains carry neutral names
    assert "catalogs/lakehouse" in m and '{"namespace":["app"]}' in m
    # nothing to seed: both flavors deploy the same thing bar the label
    assert m.replace('"false"', '"true"') == addons.iceberg_manifest(cfg("install"), seed=True)


def test_polaris_catalog_payload_hands_clients_rustfs_credentials():
    p = addons.polaris_catalog_payload()
    assert p["name"] == addons.ICEBERG_CATALOG == "lakehouse"
    props = p["properties"]
    assert props["default-base-location"] == "s3://iceberg/"
    assert props["s3.endpoint"] == addons.RUSTFS_S3_URL
    assert props["s3.path-style-access"] == "true"
    assert props["s3.access-key-id"] == "minioadmin"
    sc = p["storageConfigInfo"]
    assert sc["storageType"] == "S3" and sc["pathStyleAccess"] is True
    assert sc["allowedLocations"] == ["s3://iceberg/"]
    # and it round-trips into the setup sidecar verbatim
    assert json.dumps(p, separators=(",", ":")) in addons.iceberg_manifest(cfg("install"))


def test_addon_images_are_pinned_to_exact_versions():
    for ref in (addons.POSTGRES_IMAGE, addons.MYSQL_IMAGE, addons.REDPANDA_IMAGE,
                addons.POLARIS_IMAGE, addons.POLARIS_ADMIN_IMAGE, addons.CURL_IMAGE,
                addons.MC_IMAGE):
        tag = ref.rsplit(":", 1)[1]
        assert tag and tag != "latest", ref
        assert any(ch.isdigit() for ch in tag), ref  # a version, not a channel
    # major-only tags like postgres:16 still float; require a patch release
    assert addons.POSTGRES_IMAGE.count(".") >= 1
    assert addons.MYSQL_IMAGE.count(".") >= 2


_CREATE = re.compile(
    r"CREATE (?:SINK|MATERIALIZED VIEW|TABLE|VIEW|SOURCE|CONNECTION|SECRET) IF NOT EXISTS (\w+)")


def test_every_addon_has_both_flavors_of_manifest_and_sql():
    c = cfg("install")
    for comp in ADDONS:
        for seed in (False, True):
            m = addons.MANIFESTS[comp](c, seed)
            assert m.startswith("apiVersion: v1\nkind: Namespace")
            assert f'{addons.DEMO_LABEL}: "{"true" if seed else "false"}"' in m
        for sql in (addons.ADDON_SQL[comp](), addons.DEMO_SQL[comp]()):
            assert "CREATE CONNECTION IF NOT EXISTS" in sql
            assert addons.HOSTS[comp] in sql
            assert "addons.svc.cluster.local" in sql
            names = _CREATE.findall(sql)
            assert len(names) == len(set(names)), names  # sinks share the namespace with views
        assert addons.endpoint_lines(comp)
        assert addons.sql_path(comp, False) == f"addon-{comp}.sql"
        assert addons.sql_path(comp, True) == f"demo-{comp}.sql"


def test_addon_sql_is_connection_only_and_demo_sql_builds_on_it():
    # clean flavor: connections plus commented templates, no demo objects
    for comp in ADDONS:
        sql = addons.ADDON_SQL[comp]()
        live = [l for l in sql.splitlines() if l and not l.startswith("--")]
        assert not any(l.startswith(("CREATE SOURCE", "CREATE TABLE", "CREATE SINK",
                                     "CREATE MATERIALIZED VIEW", "INSERT")) for l in live)
    pg = addons.postgres_demo_sql()
    assert "FROM POSTGRES CONNECTION demo_pg (PUBLICATION 'mz_source')" in pg
    assert "FROM SOURCE demo_pg_src (REFERENCE public.orders)" in pg
    assert "DATABASE 'app'" in pg
    my = addons.mysql_demo_sql()
    assert "FROM MYSQL CONNECTION demo_mysql;" in my
    assert "(REFERENCE app.orders)" in my
    kf = addons.kafka_demo_sql()
    assert "KEY FORMAT TEXT VALUE FORMAT JSON" in kf and "ENVELOPE UPSERT" in kf
    assert "TO CONFLUENT SCHEMA REGISTRY" in kf
    ib = addons.iceberg_demo_sql()
    assert "CATALOG TYPE = 'rest'" in ib
    assert "CREDENTIAL = 'root:root'" in ib
    assert "WAREHOUSE = 'lakehouse'" in ib and "NAMESPACE = 'app'" in ib
    assert "MODE APPEND" in ib and "MODE UPSERT" in ib


# --- orchestration -------------------------------------------------------

def _deployments(text):
    def responder(argv):
        if argv[:3] == ["kubectl", "get", "deployment"] and "addons" in argv:
            return Result(0, text, "") if text else Result(1, "", "not found")
        return None
    return responder


def test_present_maps_deployments_to_addons_with_their_flavor():
    r = Runner(dry_run=True, responder=_deployments(
        "redpanda=false\niceberg-catalog-db=true\niceberg-catalog=true\n"), binaries=ALL_BINS)
    assert addons.present(r) == {"kafka": False, "iceberg": True}
    assert addons.present(Runner(dry_run=True, binaries=ALL_BINS)) == {}
    # the label query escapes the dot in the label key for jsonpath
    get = next(cl for cl in r.calls if cl[:3] == ["kubectl", "get", "deployment"])
    assert "mzk3\\.io/demo" in get[-1]


def test_install_applies_manifests_waits_and_seeds_kafka_only_for_demo():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    addons.install(r, cfg("install"), ["kafka", "iceberg"], demo={"kafka"})
    assert ran(r, "kubectl", "apply", "-f", "addon-kafka.yaml")
    assert ran(r, "kubectl", "apply", "-f", "addon-iceberg.yaml")
    # old seed job removed before apply (Job specs are immutable)
    delete_i = r.calls.index(["kubectl", "delete", "job", addons.SEED_JOB, "-n", "addons",
                              "--ignore-not-found"])
    apply_i = r.calls.index(["kubectl", "apply", "-f", "addon-kafka.yaml"])
    assert delete_i < apply_i
    # waits on every deployment of each add-on, then the seed job
    for dep in ("redpanda", "iceberg-catalog-db", "iceberg-catalog"):
        assert any(cl[:3] == ["kubectl", "rollout", "status"] and f"deployment/{dep}" in cl
                   for cl in r.calls)
    assert any(cl[:2] == ["kubectl", "wait"] and f"job/{addons.SEED_JOB}" in cl
               for cl in r.calls)

    clean = Runner(dry_run=True, binaries=ALL_BINS)
    addons.install(clean, cfg("install"), ["kafka"])
    assert not any(cl[:2] == ["kubectl", "wait"] and f"job/{addons.SEED_JOB}" in cl
                   for cl in clean.calls)


def _mzk3_cluster(argv):
    if argv[:2] == ["docker", "inspect"]:
        return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "mzk3"}}}]), "")
    return None


def test_install_command_deploys_requested_addons(license_file):
    r = Runner(dry_run=True, responder=_mzk3_cluster, binaries=ALL_BINS)
    assert main(["install", "-l", license_file, "--addon", "postgres",
                 "--install-demo", "kafka"], runner=r) == 0
    assert ran(r, "kubectl", "apply", "-f", "addon-postgres.yaml")
    assert ran(r, "kubectl", "apply", "-f", "addon-kafka.yaml")
    assert not ran(r, "kubectl", "apply", "-f", "addon-mysql.yaml")
    assert not ran(r, "kubectl", "apply", "-f", "addon-iceberg.yaml")


def test_install_without_addons_touches_nothing_in_addons_namespace(license_file):
    r = Runner(dry_run=True, responder=_mzk3_cluster, binaries=ALL_BINS)
    assert main(["install", "-l", license_file], runner=r) == 0
    assert not any("addons" in cl for cl in r.calls if cl[:2] == ["kubectl", "apply"])


def test_upgrade_addons_keeps_flavors_and_adds_requested(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}
    real_install = addons.install

    def spy(runner, cfg, components, demo=frozenset()):
        seen["components"], seen["demo"] = components, set(demo)
        real_install(runner, cfg, components, demo)

    monkeypatch.setattr(addons, "install", spy)
    r = Runner(dry_run=True, responder=_deployments("postgres=true\nredpanda=false\n"),
               binaries=ALL_BINS)
    rc = main(["upgrade", "--yes", "--component", "addons", "--addon", "iceberg"], runner=r)
    assert rc == 0
    # deployed ones re-applied with their own flavor, new one added clean
    assert seen["components"] == ["postgres", "kafka", "iceberg"]
    assert seen["demo"] == {"postgres"}
    assert ran(r, "kubectl", "apply", "-f", "addon-iceberg.yaml")
    # nothing else upgraded
    assert not ran(r, "kubectl", "apply", "-f", "sample-postgres.yaml")
    assert not any(cl[:2] == ["helm", "upgrade"] for cl in r.calls)


def test_upgrade_default_set_includes_addons_only_when_deployed():
    def nothing_installed_except_addons(deployments):
        def responder(argv):
            if argv[:3] == ["kubectl", "get", "deployment"] and "addons" in argv:
                return Result(0, deployments, "") if deployments else Result(1, "", "")
            if argv[:2] in (["helm", "status"], ["helm", "list"], ["kubectl", "get"]):
                return Result(1, "", "")
            return None
        return responder

    r = Runner(dry_run=True, responder=nothing_installed_except_addons("mysql=false\n"),
               binaries=ALL_BINS)
    assert main(["upgrade", "--preview"], runner=r) == 0  # add-ons count as installed
    r2 = Runner(dry_run=True, responder=nothing_installed_except_addons(""), binaries=ALL_BINS)
    assert main(["upgrade", "--preview"], runner=r2) == 1  # nothing installed at all


def test_upgrade_plan_mentions_addons():
    plan = _upgrade_plan(cfg("upgrade", "--addon", "kafka"), ["addons"])
    assert len(plan) == 1 and "Add-ons" in plan[0] and "kafka" in plan[0]


def test_status_shows_addon_pods():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    assert main(["status"], runner=r) == 0
    assert ran(r, "kubectl", "get", "pods", "-n", "addons")
