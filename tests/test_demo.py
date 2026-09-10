"""Demo components (--demo): config resolution, manifest/SQL builders, and the
install/upgrade flows that deploy them (dry-run Runner, no cluster)."""

import json

import pytest

from mzk3 import demo
from mzk3.cli import _upgrade_plan, main
from mzk3.config import DEMO_COMPONENTS, normalize_demo, resolve
from mzk3.runner import Result, Runner

ALL_BINS = {"kubectl", "helm", "curl", "k3d", "docker", "uuidgen"}


def ran(runner, *prefix):
    return any(call[: len(prefix)] == list(prefix) for call in runner.calls)


def cfg(*argv, env=None):
    return resolve(list(argv), env=env or {})[1]


# --- config resolution ---------------------------------------------------

def test_demo_flag_repeatable_and_comma_separated_in_canonical_order():
    c = cfg("install", "--demo", "iceberg,postgres", "--demo", "kafka")
    assert c.demo == ["postgres", "kafka", "iceberg"]  # canonical, deduped


def test_demo_all_expands_to_every_component():
    assert cfg("install", "--demo", "all").demo == list(DEMO_COMPONENTS)


def test_demo_defaults_to_nothing():
    assert cfg("install").demo == []


def test_demo_from_env_and_config_file(tmp_path):
    assert cfg("install", env={"MZ_DEMO": "kafka, mysql"}).demo == ["mysql", "kafka"]
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"demo": ["postgres"]}))
    # config file beats env; flag beats config file
    assert cfg("install", "--config", str(conf),
               env={"MZ_DEMO": "kafka"}).demo == ["postgres"]
    assert cfg("install", "--config", str(conf), "--demo", "iceberg").demo == ["iceberg"]


def test_demo_rejects_unknown_component():
    with pytest.raises(SystemExit):  # argparse error
        resolve(["install", "--demo", "mongodb"], env={})
    with pytest.raises(ValueError):
        normalize_demo(["postgres", "nope"])


def test_demo_resources_have_defaults_and_are_overridable(tmp_path):
    c = cfg("install")
    assert c.resources["demo-kafka"]["memory"] == "1Gi"
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"resources": {"demo-kafka": {"memory": "2Gi"}}}))
    c = cfg("install", "--config", str(conf))
    assert c.resources["demo-kafka"] == {"cpu": "1", "memory": "2Gi"}


# --- manifest builders ---------------------------------------------------

def _docs(text):
    """Split a multi-document YAML string into (kind, name) pairs cheaply,
    without a YAML dependency."""
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


def test_postgres_manifest_is_logical_replication_ready():
    m = demo.postgres_manifest(cfg("install"))
    assert ("Namespace", "demo") in _docs(m)
    assert ("Deployment", "postgres") in _docs(m)
    assert ("PersistentVolumeClaim", "postgres-data") in _docs(m)
    assert "wal_level=logical" in m
    assert f"image: {demo.POSTGRES_IMAGE}" in m
    # seeded schema + publication + replication user
    assert "CREATE PUBLICATION mz_source FOR ALL TABLES" in m
    assert "REPLICA IDENTITY FULL" in m
    assert "CREATE ROLE materialize WITH LOGIN REPLICATION" in m
    assert "CREATE TABLE orders" in m


def test_mysql_manifest_enables_gtid_binlog_replication():
    m = demo.mysql_manifest(cfg("install"))
    assert ("Deployment", "mysql") in _docs(m)
    for flag in ("--gtid-mode=ON", "--enforce-gtid-consistency=ON",
                 "--binlog-row-image=FULL", "--binlog-row-metadata=FULL"):
        assert flag in m
    assert "REPLICATION SLAVE, REPLICATION CLIENT" in m
    assert f"image: {demo.MYSQL_IMAGE}" in m


def test_kafka_manifest_advertises_cluster_dns_and_seeds_topic():
    m = demo.kafka_manifest(cfg("install"))
    kinds = _docs(m)
    assert ("Deployment", "redpanda") in kinds
    assert ("Job", "redpanda-seed") in kinds
    assert ("Service", "redpanda") in kinds
    assert "--advertise-kafka-addr=PLAINTEXT://redpanda.demo.svc.cluster.local:9092" in m
    assert "--schema-registry-addr=0.0.0.0:8081" in m
    assert "redpanda.enable_transactions=true" in m
    # seed job creates the topic once and produces keyed JSON
    assert f"topic create {demo.KAFKA_TOPIC}" in m
    assert '"customer_id":' in m
    # redpanda pins 75% of the container limit (1Gi default -> 768M)
    assert "--memory=768M" in m


def test_kafka_manifest_memory_tracks_resource_override(tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"resources": {"demo-kafka": {"memory": "2Gi"}}}))
    m = demo.kafka_manifest(cfg("install", "--config", str(conf)))
    assert "--memory=1536M" in m
    assert 'memory: "2Gi"' in m


def test_memory_mb_parses_kubernetes_quantities():
    assert demo.memory_mb("1Gi") == 1024
    assert demo.memory_mb("1536Mi") == 1536
    assert demo.memory_mb("512Mi") == 512
    assert demo.memory_mb("2G") == 1907
    with pytest.raises(ValueError):
        demo.memory_mb("lots")


def test_iceberg_manifest_runs_polaris_on_rustfs_with_durable_metastore():
    m = demo.iceberg_manifest(cfg("install"))
    kinds = _docs(m)
    assert ("Deployment", "iceberg-catalog") in kinds
    assert ("Deployment", "iceberg-catalog-db") in kinds
    assert ("PersistentVolumeClaim", "iceberg-catalog-db-data") in kinds
    assert ("ConfigMap", "iceberg-catalog-config") in kinds
    assert f"image: {demo.POLARIS_IMAGE}" in m
    assert f"image: {demo.POLARIS_ADMIN_IMAGE}" in m
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


def test_polaris_catalog_payload_hands_clients_rustfs_credentials():
    p = demo.polaris_catalog_payload()
    assert p["name"] == demo.ICEBERG_CATALOG
    props = p["properties"]
    assert props["default-base-location"] == "s3://iceberg/"
    assert props["s3.endpoint"] == demo.RUSTFS_S3_URL
    assert props["s3.path-style-access"] == "true"
    assert props["s3.access-key-id"] == "minioadmin"
    sc = p["storageConfigInfo"]
    assert sc["storageType"] == "S3" and sc["pathStyleAccess"] is True
    assert sc["allowedLocations"] == ["s3://iceberg/"]
    # and it round-trips into the setup sidecar verbatim
    assert json.dumps(p, separators=(",", ":")) in demo.iceberg_manifest(cfg("install"))


def test_demo_images_are_pinned_to_exact_versions():
    for ref in (demo.POSTGRES_IMAGE, demo.MYSQL_IMAGE, demo.REDPANDA_IMAGE,
                demo.POLARIS_IMAGE, demo.POLARIS_ADMIN_IMAGE, demo.CURL_IMAGE,
                demo.MC_IMAGE):
        tag = ref.rsplit(":", 1)[1]
        assert tag and tag != "latest", ref
        assert any(ch.isdigit() for ch in tag), ref  # a version, not a channel
    # major-only tags like postgres:16 still float; require a patch release
    assert demo.POSTGRES_IMAGE.count(".") >= 1
    assert demo.MYSQL_IMAGE.count(".") >= 2


def test_every_component_has_manifest_sql_and_endpoints():
    c = cfg("install")
    for comp in DEMO_COMPONENTS:
        assert demo.MANIFESTS[comp](c).startswith("apiVersion: v1\nkind: Namespace")
        sql = demo.SQL[comp]()
        assert "CREATE CONNECTION IF NOT EXISTS" in sql
        assert demo.HOSTS[comp] in sql
        assert demo.endpoint_lines(comp)


def test_sql_uses_current_source_syntax_and_iceberg_rest_catalog():
    pg = demo.postgres_sql()
    assert "FROM POSTGRES CONNECTION demo_pg (PUBLICATION 'mz_source')" in pg
    assert "FROM SOURCE demo_pg_src (REFERENCE public.orders)" in pg
    my = demo.mysql_sql()
    assert "FROM MYSQL CONNECTION demo_mysql;" in my
    assert "(REFERENCE demo.orders)" in my
    kf = demo.kafka_sql()
    assert "KEY FORMAT TEXT VALUE FORMAT JSON" in kf and "ENVELOPE UPSERT" in kf
    assert "TO CONFLUENT SCHEMA REGISTRY" in kf
    ib = demo.iceberg_sql()
    assert "CATALOG TYPE = 'rest'" in ib
    assert "CREDENTIAL = 'root:root'" in ib
    assert f"WAREHOUSE = '{demo.ICEBERG_CATALOG}'" in ib
    assert "MODE APPEND" in ib and "MODE UPSERT" in ib
    # sinks share the object namespace with views: names must not collide
    import re
    names = re.findall(r"CREATE (?:SINK|MATERIALIZED VIEW|TABLE|VIEW|SOURCE|CONNECTION|SECRET) IF NOT EXISTS (\w+)", ib)
    assert len(names) == len(set(names)), names
    for sql in (pg, my, kf):
        names = re.findall(r"CREATE (?:SINK|MATERIALIZED VIEW|TABLE|VIEW|SOURCE|CONNECTION|SECRET) IF NOT EXISTS (\w+)", sql)
        assert len(names) == len(set(names)), names


# --- orchestration -------------------------------------------------------

def test_present_maps_deployments_to_components():
    def responder(argv):
        if argv[:3] == ["kubectl", "get", "deployment"] and "demo" in argv:
            return Result(0, "deployment.apps/redpanda\ndeployment.apps/iceberg-catalog-db\n"
                             "deployment.apps/iceberg-catalog\n", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert demo.present(r) == ["kafka", "iceberg"]
    assert demo.present(Runner(dry_run=True, binaries=ALL_BINS)) == []


def test_install_applies_manifests_waits_and_reseeds_kafka():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    demo.install(r, cfg("install"), ["kafka", "iceberg"])
    assert ran(r, "kubectl", "apply", "-f", "demo-kafka.yaml")
    assert ran(r, "kubectl", "apply", "-f", "demo-iceberg.yaml")
    # old seed job removed before apply (Job specs are immutable)
    delete_i = r.calls.index(["kubectl", "delete", "job", "redpanda-seed", "-n", "demo",
                              "--ignore-not-found"])
    apply_i = r.calls.index(["kubectl", "apply", "-f", "demo-kafka.yaml"])
    assert delete_i < apply_i
    # waits on every deployment of each component, then the seed job
    for dep in ("redpanda", "iceberg-catalog-db", "iceberg-catalog"):
        assert any(cl[:3] == ["kubectl", "rollout", "status"] and f"deployment/{dep}" in cl
                   for cl in r.calls)
    assert any(cl[:2] == ["kubectl", "wait"] and "job/redpanda-seed" in cl for cl in r.calls)


def test_install_command_deploys_requested_demo_components(license_file):
    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "mzk3"}}}]), "")
        if argv[:3] == ["kubectl", "get", "nodes"]:
            return Result(0, "node Ready", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert main(["install", "-l", license_file, "--demo", "postgres,kafka"], runner=r) == 0
    assert ran(r, "kubectl", "apply", "-f", "demo-postgres.yaml")
    assert ran(r, "kubectl", "apply", "-f", "demo-kafka.yaml")
    assert not ran(r, "kubectl", "apply", "-f", "demo-mysql.yaml")
    assert not ran(r, "kubectl", "apply", "-f", "demo-iceberg.yaml")


def test_install_without_demo_touches_nothing_in_demo_namespace(license_file):
    def responder(argv):
        if argv[:2] == ["docker", "inspect"]:
            return Result(0, json.dumps([{"Config": {"Labels": {"created-by": "mzk3"}}}]), "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    assert main(["install", "-l", license_file], runner=r) == 0
    assert not any("demo" in cl for cl in r.calls if cl[:2] == ["kubectl", "apply"])


def test_upgrade_demo_reapplies_present_plus_requested():
    def responder(argv):
        if argv[:3] == ["kubectl", "get", "deployment"] and "demo" in argv:
            return Result(0, "deployment.apps/postgres\n", "")
        return None

    r = Runner(dry_run=True, responder=responder, binaries=ALL_BINS)
    rc = main(["upgrade", "--yes", "--component", "demo", "--demo", "iceberg"], runner=r)
    assert rc == 0
    assert ran(r, "kubectl", "apply", "-f", "demo-postgres.yaml")  # already deployed
    assert ran(r, "kubectl", "apply", "-f", "demo-iceberg.yaml")   # newly requested
    assert not ran(r, "kubectl", "apply", "-f", "demo-kafka.yaml")
    # nothing else upgraded
    assert not ran(r, "kubectl", "apply", "-f", "sample-postgres.yaml")
    assert not any(cl[:2] == ["helm", "upgrade"] for cl in r.calls)


def test_upgrade_default_set_includes_demo_only_when_deployed():
    def nothing_installed_except_demo(demo_deployments):
        # every other "is X installed?" probe fails; only the demo namespace
        # (optionally) has deployments.
        def responder(argv):
            if argv[:3] == ["kubectl", "get", "deployment"] and "demo" in argv:
                return Result(0, demo_deployments, "") if demo_deployments else Result(1, "", "")
            if argv[:2] in (["helm", "status"], ["helm", "list"], ["kubectl", "get"]):
                return Result(1, "", "")
            return None
        return responder

    r = Runner(dry_run=True, responder=nothing_installed_except_demo("deployment.apps/mysql\n"),
               binaries=ALL_BINS)
    assert main(["upgrade", "--preview"], runner=r) == 0  # demo counts as installed
    r2 = Runner(dry_run=True, responder=nothing_installed_except_demo(""), binaries=ALL_BINS)
    assert main(["upgrade", "--preview"], runner=r2) == 1  # nothing installed at all


def test_upgrade_plan_mentions_demo():
    plan = _upgrade_plan(cfg("upgrade", "--demo", "kafka"), ["demo"])
    assert len(plan) == 1 and "Demo" in plan[0] and "kafka" in plan[0]


def test_status_shows_demo_pods():
    r = Runner(dry_run=True, binaries=ALL_BINS)
    assert main(["status"], runner=r) == 0
    assert ran(r, "kubectl", "get", "pods", "-n", "demo")
