"""Optional add-ons deployed next to Materialize (`--addon`, `--install-demo`).

A fresh install has nothing to connect to. These add-ons give an environment
realistic upstream systems -- and an Iceberg catalog to sink into -- so people
can exercise sources/sinks the way they would against their own stack:

- postgres  PostgreSQL 16 ready for logical replication (wal_level=logical, a
            `materialize` replication user, an `mz_source` publication FOR ALL
            TABLES, an empty `app` database).
- mysql     MySQL 8.4 ready for GTID binlog replication (binlog_row_metadata=
            FULL, a `materialize` replication user, an empty `app` database).
- kafka     Redpanda (Kafka API on 9092, schema registry on 8081, HTTP proxy on
            8082), topics auto-created.
- iceberg   Apache Polaris, an Iceberg REST catalog, with its warehouse on the
            in-cluster RustFS (S3) in a dedicated `iceberg` bucket: catalog
            `lakehouse`, namespace `app`. Polaris persists to its own small
            Postgres so tables survive restarts. Materialize's Iceberg sink
            talks to it as a `rest` catalog.

`--addon NAME` deploys them **clean**: bring your own schema and topics, and use
the generated `addon-<name>.sql` for the matching `CREATE CONNECTION`.
`--install-demo [NAME]` deploys the same add-ons **seeded** with demo data
(customers/orders tables, a keyed JSON `demo_orders` topic) and writes a
`demo-<name>.sql` walkthrough of sources, views and sinks (see DEMO.md). Which
flavor a deployed add-on has is recorded in a label so `upgrade` re-applies it
unchanged.

Everything lives in the `addons` namespace, uses fixed throwaway credentials,
and is PVC-backed so it survives pod restarts. The manifest/SQL builders are
pure (string in, string out) so they're unit testable; `install` is the only
function that touches the cluster.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import commands as c
from . import log
from .config import ADDONS, DEFAULT_RESOURCES, Config
from .runner import Runner

NAMESPACE = "addons"
# Label on each add-on Deployment recording whether it was seeded with demo
# data, so re-applies (upgrade) keep the same flavor.
DEMO_LABEL = "mzk3.io/demo"

POSTGRES_IMAGE = "postgres:16.15"
MYSQL_IMAGE = "mysql:8.4.11"
REDPANDA_IMAGE = "docker.redpanda.com/redpandadata/redpanda:v25.3.17"
# Same Polaris release Materialize's own Iceberg sink tests run against.
POLARIS_VERSION = "1.7.0"
POLARIS_IMAGE = f"apache/polaris:{POLARIS_VERSION}"
POLARIS_ADMIN_IMAGE = f"apache/polaris-admin-tool:{POLARIS_VERSION}"
CURL_IMAGE = "curlimages/curl:8.14.1"
MC_IMAGE = c.MC_IMAGE

# Throwaway credentials. Materialize connects with the `materialize` user.
DB_NAME = "app"
PG_ADMIN_PASSWORD = "postgres"
MYSQL_ROOT_PASSWORD = "mysql"
MZ_USER = "materialize"
MZ_PASSWORD = "materialize"

KAFKA_TOPIC = "demo_orders"  # seeded by --install-demo only

# Polaris: realm/root credential (client_id:client_secret), the catalog
# (Materialize's WAREHOUSE) and the namespace sinks write into.
POLARIS_REALM = "POLARIS"
POLARIS_CREDENTIAL = "root:root"
ICEBERG_CATALOG = "lakehouse"
ICEBERG_NAMESPACE = "app"
ICEBERG_BUCKET = "iceberg"
RUSTFS_S3_URL = f"http://{c.RUSTFS_S3_HOST}:9000"

# Kubernetes Deployments each add-on consists of (readiness is waited on each),
# and the one whose presence means "this add-on is installed".
DEPLOYMENTS: dict[str, list[str]] = {
    "postgres": ["postgres"],
    "mysql": ["mysql"],
    "kafka": ["redpanda"],
    "iceberg": ["iceberg-catalog-db", "iceberg-catalog"],
}
PRIMARY_DEPLOYMENT = {comp: deps[-1] for comp, deps in DEPLOYMENTS.items()}

# In-cluster hostnames Materialize uses to reach each add-on.
HOSTS = {
    "postgres": f"postgres.{NAMESPACE}.svc.cluster.local",
    "mysql": f"mysql.{NAMESPACE}.svc.cluster.local",
    "kafka": f"redpanda.{NAMESPACE}.svc.cluster.local",
    "iceberg": f"iceberg-catalog.{NAMESPACE}.svc.cluster.local",
}


# --- helpers -------------------------------------------------------------

def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def _res(cfg: Config, key: str) -> tuple[str, str]:
    """(cpu, memory) for an add-on: config-file override or the default."""
    r = {**DEFAULT_RESOURCES.get(key, {}), **cfg.resources.get(key, {})}
    return r["cpu"], r["memory"]


def _resources_yaml(cpu: str, memory: str) -> str:
    return (f'resources:\n'
            f'  requests:\n    cpu: "{cpu}"\n    memory: "{memory}"\n'
            f'  limits:\n    cpu: "{cpu}"\n    memory: "{memory}"')


def memory_mb(quantity: str) -> int:
    """A Kubernetes memory quantity ("1Gi", "1536Mi", "2G", "800M") in MiB."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]i?)?\s*", quantity)
    if not m:
        raise ValueError(f"unsupported memory quantity: {quantity!r}")
    num = float(m.group(1))
    unit = m.group(2) or ""
    scale = {
        "": 1 / 2**20, "K": 1e3 / 2**20, "Ki": 1 / 2**10,
        "M": 1e6 / 2**20, "Mi": 1, "G": 1e9 / 2**20, "Gi": 2**10,
        "T": 1e12 / 2**20, "Ti": 2**20,
    }[unit]
    return int(num * scale)


def _flavor(seed: bool) -> str:
    return "true" if seed else "false"


_NAMESPACE_YAML = f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {NAMESPACE}
"""


def _pvc(name: str, size: str) -> str:
    return f"""\
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}
  namespace: {NAMESPACE}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: {size}
"""


def _service(name: str, ports: list[tuple[str, int]]) -> str:
    port_lines = "\n".join(
        f"  - name: {pname}\n    port: {port}\n    targetPort: {port}"
        for pname, port in ports)
    return f"""\
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {NAMESPACE}
spec:
  selector:
    app: {name}
  ports:
{port_lines}
"""


# --- seeded demo schema (shared by postgres + mysql) ---------------------

_CUSTOMERS = [
    ("Ada Lovelace", "ada@example.com"),
    ("Grace Hopper", "grace@example.com"),
    ("Alan Turing", "alan@example.com"),
]
_ORDERS = [  # (customer_id, amount, status)
    (1, "120.00", "shipped"),
    (1, "35.50", "new"),
    (2, "799.99", "paid"),
    (3, "12.25", "new"),
    (3, "64.00", "cancelled"),
]


def _seed_rows() -> tuple[str, str]:
    customers = ", ".join(f"('{n}', '{e}')" for n, e in _CUSTOMERS)
    orders = ", ".join(f"({cid}, {amt}, '{st}')" for cid, amt, st in _ORDERS)
    return customers, orders


# --- postgres ------------------------------------------------------------

def _postgres_init_sql(seed: bool) -> str:
    setup = f"""\
-- Runs once, when the data directory is first initialised.
-- Replication user for Materialize (needs REPLICATION for logical decoding).
CREATE ROLE {MZ_USER} WITH LOGIN REPLICATION PASSWORD '{MZ_PASSWORD}';
GRANT CONNECT ON DATABASE {DB_NAME} TO {MZ_USER};
GRANT USAGE ON SCHEMA public TO {MZ_USER};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {MZ_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {MZ_USER};
-- Publication for Materialize; covers tables created later too.
CREATE PUBLICATION mz_source FOR ALL TABLES;
"""
    if not seed:
        return setup
    customers, orders = _seed_rows()
    return setup + f"""
-- Demo data.
CREATE TABLE customers (
  id         serial PRIMARY KEY,
  name       text NOT NULL,
  email      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders (
  id          serial PRIMARY KEY,
  customer_id int NOT NULL REFERENCES customers(id),
  amount      numeric(10,2) NOT NULL,
  status      text NOT NULL DEFAULT 'new',
  created_at  timestamptz NOT NULL DEFAULT now()
);
-- Materialize needs the full old row on UPDATE/DELETE.
ALTER TABLE customers REPLICA IDENTITY FULL;
ALTER TABLE orders REPLICA IDENTITY FULL;

INSERT INTO customers (name, email) VALUES {customers};
INSERT INTO orders (customer_id, amount, status) VALUES {orders};
"""


def postgres_manifest(cfg: Config, seed: bool = False) -> str:
    cpu, mem = _res(cfg, "addon-postgres")
    return f"""\
{_NAMESPACE_YAML}---
apiVersion: v1
kind: ConfigMap
metadata:
  name: postgres-init
  namespace: {NAMESPACE}
data:
  01-init.sql: |
{_indent(_postgres_init_sql(seed), 4)}
{_pvc("postgres-data", "5Gi")}---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: {NAMESPACE}
  labels:
    {DEMO_LABEL}: "{_flavor(seed)}"
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
      - name: postgres
        image: {POSTGRES_IMAGE}
        args:
        - postgres
        - -c
        - wal_level=logical
        - -c
        - max_wal_senders=10
        - -c
        - max_replication_slots=10
        env:
        - name: POSTGRES_USER
          value: postgres
        - name: POSTGRES_PASSWORD
          value: "{PG_ADMIN_PASSWORD}"
        - name: POSTGRES_DB
          value: {DB_NAME}
        - name: PGDATA
          value: /var/lib/postgresql/data/pgdata
        ports:
        - containerPort: 5432
          name: postgres
        readinessProbe:
          exec:
            command: ["pg_isready", "-U", "postgres", "-d", "{DB_NAME}"]
          initialDelaySeconds: 5
          periodSeconds: 5
        {_indent(_resources_yaml(cpu, mem), 8).lstrip()}
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
        - name: init
          mountPath: /docker-entrypoint-initdb.d
          readOnly: true
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: postgres-data
      - name: init
        configMap:
          name: postgres-init
{_service("postgres", [("postgres", 5432)])}"""


def _psql_hint(sql_file: str) -> str:
    return (f"--   psql -h localhost -p 6875 -U materialize materialize -f {sql_file}\n"
            "-- (port-forward balancerd first; see README \"Accessing Materialize\")")


def postgres_addon_sql() -> str:
    host = HOSTS["postgres"]
    return f"""\
-- Materialize -> PostgreSQL add-on (namespace {NAMESPACE}), via logical replication.
-- The server has wal_level=logical, a `{MZ_USER}` replication user, an empty
-- `{DB_NAME}` database and an `mz_source` publication FOR ALL TABLES. Load your
-- own schema first, e.g.:
--   kubectl exec -n {NAMESPACE} deploy/postgres -- psql -U postgres -d {DB_NAME} -c "CREATE TABLE ..."
--   kubectl port-forward -n {NAMESPACE} svc/postgres 5432:5432   # then pg_restore / psql -f
-- and give each table REPLICA IDENTITY FULL. Then run this against Materialize:
{_psql_hint("addon-postgres.sql")}

CREATE SECRET IF NOT EXISTS pg_password AS '{MZ_PASSWORD}';

CREATE CONNECTION IF NOT EXISTS pg_conn TO POSTGRES (
    HOST '{host}',
    PORT 5432,
    USER '{MZ_USER}',
    PASSWORD SECRET pg_password,
    SSL MODE 'disable',
    DATABASE '{DB_NAME}'
);

-- CREATE SOURCE pg_src FROM POSTGRES CONNECTION pg_conn (PUBLICATION 'mz_source');
-- CREATE TABLE my_table FROM SOURCE pg_src (REFERENCE public.my_table);
"""


def postgres_demo_sql() -> str:
    host = HOSTS["postgres"]
    return f"""\
-- Materialize -> demo PostgreSQL (namespace {NAMESPACE}), via logical replication.
-- Run against Materialize, e.g.:
{_psql_hint("demo-postgres.sql")}

CREATE SECRET IF NOT EXISTS demo_pg_password AS '{MZ_PASSWORD}';

CREATE CONNECTION IF NOT EXISTS demo_pg TO POSTGRES (
    HOST '{host}',
    PORT 5432,
    USER '{MZ_USER}',
    PASSWORD SECRET demo_pg_password,
    SSL MODE 'disable',
    DATABASE '{DB_NAME}'
);

CREATE SOURCE IF NOT EXISTS demo_pg_src
  FROM POSTGRES CONNECTION demo_pg (PUBLICATION 'mz_source');

CREATE TABLE IF NOT EXISTS pg_customers FROM SOURCE demo_pg_src (REFERENCE public.customers);
CREATE TABLE IF NOT EXISTS pg_orders    FROM SOURCE demo_pg_src (REFERENCE public.orders);

-- Try it:
--   SELECT c.name, count(*) AS orders, sum(o.amount) AS total
--   FROM pg_orders o JOIN pg_customers c ON c.id = o.customer_id
--   GROUP BY c.name;
--
-- Then change the upstream data and watch the results move:
--   kubectl exec -n {NAMESPACE} deploy/postgres -- psql -U postgres -d {DB_NAME} \\
--     -c "INSERT INTO orders (customer_id, amount, status) VALUES (2, 42.00, 'new');"
"""


# --- mysql ---------------------------------------------------------------

def _mysql_init_sql(seed: bool) -> str:
    setup = f"""\
-- Runs once, when the data directory is first initialised.
-- Replication user for Materialize (privileges per the Materialize MySQL docs).
CREATE USER '{MZ_USER}'@'%' IDENTIFIED BY '{MZ_PASSWORD}';
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT, LOCK TABLES
  ON *.* TO '{MZ_USER}'@'%';
FLUSH PRIVILEGES;
"""
    if not seed:
        return setup
    customers, orders = _seed_rows()
    return setup + f"""
-- Demo data.
USE {DB_NAME};
CREATE TABLE customers (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(255) NOT NULL,
  email      VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE orders (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  customer_id INT NOT NULL,
  amount      DECIMAL(10,2) NOT NULL,
  status      VARCHAR(32) NOT NULL DEFAULT 'new',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES customers(id)
);
INSERT INTO customers (name, email) VALUES {customers};
INSERT INTO orders (customer_id, amount, status) VALUES {orders};
"""


def mysql_manifest(cfg: Config, seed: bool = False) -> str:
    cpu, mem = _res(cfg, "addon-mysql")
    return f"""\
{_NAMESPACE_YAML}---
apiVersion: v1
kind: ConfigMap
metadata:
  name: mysql-init
  namespace: {NAMESPACE}
data:
  01-init.sql: |
{_indent(_mysql_init_sql(seed), 4)}
{_pvc("mysql-data", "5Gi")}---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql
  namespace: {NAMESPACE}
  labels:
    {DEMO_LABEL}: "{_flavor(seed)}"
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: mysql
  template:
    metadata:
      labels:
        app: mysql
    spec:
      containers:
      - name: mysql
        image: {MYSQL_IMAGE}
        # GTID-based binlog replication, as the Materialize MySQL source requires.
        # (log_bin, binlog_format=ROW and binlog_row_image=FULL are 8.x defaults;
        # binlog_row_metadata=FULL is needed for CREATE TABLE ... FROM SOURCE.)
        args:
        - --server-id=1
        - --gtid-mode=ON
        - --enforce-gtid-consistency=ON
        - --binlog-row-image=FULL
        - --binlog-row-metadata=FULL
        - --binlog-expire-logs-seconds=604800
        env:
        - name: MYSQL_ROOT_PASSWORD
          value: "{MYSQL_ROOT_PASSWORD}"
        - name: MYSQL_DATABASE
          value: {DB_NAME}
        ports:
        - containerPort: 3306
          name: mysql
        readinessProbe:
          exec:
            command:
            - sh
            - -c
            - mysqladmin ping -h127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD"
          initialDelaySeconds: 15
          periodSeconds: 10
          failureThreshold: 30
        {_indent(_resources_yaml(cpu, mem), 8).lstrip()}
        volumeMounts:
        - name: data
          mountPath: /var/lib/mysql
        - name: init
          mountPath: /docker-entrypoint-initdb.d
          readOnly: true
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: mysql-data
      - name: init
        configMap:
          name: mysql-init
{_service("mysql", [("mysql", 3306)])}"""


def mysql_addon_sql() -> str:
    host = HOSTS["mysql"]
    return f"""\
-- Materialize -> MySQL add-on (namespace {NAMESPACE}), via GTID binlog replication.
-- The server has gtid_mode=ON and binlog_row_metadata=FULL, a `{MZ_USER}`
-- replication user and an empty `{DB_NAME}` database. Load your own schema first, e.g.:
--   kubectl exec -n {NAMESPACE} deploy/mysql -- mysql -uroot -p{MYSQL_ROOT_PASSWORD} {DB_NAME} -e "CREATE TABLE ..."
--   kubectl port-forward -n {NAMESPACE} svc/mysql 3306:3306   # then mysql < dump.sql
-- Then run this against Materialize:
{_psql_hint("addon-mysql.sql")}

CREATE SECRET IF NOT EXISTS mysql_password AS '{MZ_PASSWORD}';

CREATE CONNECTION IF NOT EXISTS mysql_conn TO MYSQL (
    HOST '{host}',
    PORT 3306,
    USER '{MZ_USER}',
    PASSWORD SECRET mysql_password,
    SSL MODE 'disabled'
);

-- CREATE SOURCE mysql_src FROM MYSQL CONNECTION mysql_conn;
-- CREATE TABLE my_table FROM SOURCE mysql_src (REFERENCE {DB_NAME}.my_table);
"""


def mysql_demo_sql() -> str:
    host = HOSTS["mysql"]
    return f"""\
-- Materialize -> demo MySQL (namespace {NAMESPACE}), via GTID binlog replication.
-- Run against Materialize, e.g.:
{_psql_hint("demo-mysql.sql")}

CREATE SECRET IF NOT EXISTS demo_mysql_password AS '{MZ_PASSWORD}';

CREATE CONNECTION IF NOT EXISTS demo_mysql TO MYSQL (
    HOST '{host}',
    PORT 3306,
    USER '{MZ_USER}',
    PASSWORD SECRET demo_mysql_password,
    SSL MODE 'disabled'
);

CREATE SOURCE IF NOT EXISTS demo_mysql_src FROM MYSQL CONNECTION demo_mysql;

CREATE TABLE IF NOT EXISTS mysql_customers FROM SOURCE demo_mysql_src (REFERENCE {DB_NAME}.customers);
CREATE TABLE IF NOT EXISTS mysql_orders    FROM SOURCE demo_mysql_src (REFERENCE {DB_NAME}.orders);

-- Try it:
--   SELECT c.name, count(*) AS orders, sum(o.amount) AS total
--   FROM mysql_orders o JOIN mysql_customers c ON c.id = o.customer_id
--   GROUP BY c.name;
--
-- Then change the upstream data and watch the results move:
--   kubectl exec -n {NAMESPACE} deploy/mysql -- mysql -uroot -p{MYSQL_ROOT_PASSWORD} {DB_NAME} \\
--     -e "INSERT INTO orders (customer_id, amount, status) VALUES (2, 42.00, 'new');"
"""


# --- kafka (redpanda) ----------------------------------------------------

def _kafka_seed_records() -> list[tuple[str, str]]:
    """(key, json value) records seeded into the demo topic; keyed by order id
    so an UPSERT-envelope source treats later records for a key as updates."""
    recs = []
    for i, (cid, amt, st) in enumerate(_ORDERS, start=1):
        value = json.dumps({"id": i, "customer_id": cid, "amount": float(amt),
                            "status": st}, separators=(",", ":"))
        recs.append((str(i), value))
    return recs


def _kafka_seed_script() -> str:
    broker = f"{HOSTS['kafka']}:9092"
    lines = "\n".join(f"  '{k} {v}' \\" for k, v in _kafka_seed_records())
    return f"""\
set -e
RPK="rpk -X brokers={broker}"
until $RPK cluster info >/dev/null 2>&1; do echo "waiting for redpanda..."; sleep 3; done
# Seed only when the topic is new, so re-running the job doesn't duplicate data.
if $RPK topic create {KAFKA_TOPIC} -p 1 -r 1; then
  printf '%s\\n' \\
{lines}
  | $RPK topic produce {KAFKA_TOPIC} -f '%k %v\\n'
  echo "seeded topic {KAFKA_TOPIC}"
else
  echo "topic {KAFKA_TOPIC} already exists; leaving it alone"
fi
"""


SEED_JOB = "redpanda-seed"


def _kafka_seed_job() -> str:
    return f"""\
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {SEED_JOB}
  namespace: {NAMESPACE}
spec:
  backoffLimit: 10
  template:
    spec:
      restartPolicy: OnFailure
      containers:
      - name: seed
        image: {REDPANDA_IMAGE}
        command: ["/bin/bash", "-c"]
        args:
        - |
{_indent(_kafka_seed_script(), 10)}
"""


def kafka_manifest(cfg: Config, seed: bool = False) -> str:
    cpu, mem = _res(cfg, "addon-kafka")
    host = HOSTS["kafka"]
    # Leave the container headroom over what Redpanda pins for itself.
    rp_mem = max(256, int(memory_mb(mem) * 0.75))
    return f"""\
{_NAMESPACE_YAML}{_pvc("redpanda-data", "5Gi")}---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redpanda
  namespace: {NAMESPACE}
  labels:
    {DEMO_LABEL}: "{_flavor(seed)}"
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: redpanda
  template:
    metadata:
      labels:
        app: redpanda
    spec:
      containers:
      - name: redpanda
        image: {REDPANDA_IMAGE}
        args:
        - redpanda
        - start
        - --mode=dev-container
        - --smp=1
        - --memory={rp_mem}M
        - --reserve-memory=0M
        - --overprovisioned
        - --node-id=0
        - --check=false
        - --kafka-addr=PLAINTEXT://0.0.0.0:9092
        - --advertise-kafka-addr=PLAINTEXT://{host}:9092
        - --pandaproxy-addr=0.0.0.0:8082
        - --advertise-pandaproxy-addr={host}:8082
        - --schema-registry-addr=0.0.0.0:8081
        - --rpc-addr=0.0.0.0:33145
        - --advertise-rpc-addr={host}:33145
        - --set
        - redpanda.auto_create_topics_enabled=true
        - --set
        - redpanda.enable_transactions=true
        - --set
        - redpanda.enable_idempotence=true
        ports:
        - containerPort: 9092
          name: kafka
        - containerPort: 8081
          name: schema-registry
        - containerPort: 8082
          name: pandaproxy
        - containerPort: 9644
          name: admin
        readinessProbe:
          exec:
            command: ["rpk", "cluster", "info"]
          initialDelaySeconds: 10
          periodSeconds: 5
        {_indent(_resources_yaml(cpu, mem), 8).lstrip()}
        volumeMounts:
        - name: data
          mountPath: /var/lib/redpanda/data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: redpanda-data
{_service("redpanda", [("kafka", 9092), ("schema-registry", 8081),
                       ("pandaproxy", 8082), ("admin", 9644)])}{_kafka_seed_job() if seed else ""}"""


def kafka_addon_sql() -> str:
    host = HOSTS["kafka"]
    return f"""\
-- Materialize <-> Redpanda add-on (namespace {NAMESPACE}): Kafka API on 9092
-- (PLAINTEXT, topics auto-created), Confluent-compatible schema registry on 8081.
-- Produce your own data, e.g.:
--   kubectl exec -n {NAMESPACE} deploy/redpanda -- sh -c 'echo "k1 {{\\"hello\\":\\"world\\"}}" | rpk topic produce my_topic -f "%k %v\\n"'
-- Then run this against Materialize:
{_psql_hint("addon-kafka.sql")}

CREATE CONNECTION IF NOT EXISTS kafka_conn TO KAFKA (
    BROKER '{host}:9092',
    SECURITY PROTOCOL = 'PLAINTEXT'
);

CREATE CONNECTION IF NOT EXISTS csr_conn TO CONFLUENT SCHEMA REGISTRY (
    URL 'http://{host}:8081'
);

-- CREATE SOURCE my_src FROM KAFKA CONNECTION kafka_conn (TOPIC 'my_topic');
-- CREATE TABLE my_table FROM SOURCE my_src KEY FORMAT TEXT VALUE FORMAT JSON ENVELOPE UPSERT;
-- CREATE SINK my_sink FROM my_view INTO KAFKA CONNECTION kafka_conn (TOPIC 'my_out')
--   KEY (id) FORMAT JSON ENVELOPE UPSERT;
"""


def kafka_demo_sql() -> str:
    host = HOSTS["kafka"]
    return f"""\
-- Materialize <-> demo Redpanda (namespace {NAMESPACE}): Kafka API on 9092,
-- Confluent-compatible schema registry on 8081. Run against Materialize, e.g.:
{_psql_hint("demo-kafka.sql")}

CREATE CONNECTION IF NOT EXISTS demo_kafka TO KAFKA (
    BROKER '{host}:9092',
    SECURITY PROTOCOL = 'PLAINTEXT'
);

CREATE CONNECTION IF NOT EXISTS demo_csr TO CONFLUENT SCHEMA REGISTRY (
    URL 'http://{host}:8081'
);

-- The seeded topic holds keyed JSON records (key = order id); read it with
-- upsert semantics so later records for the same key update the row.
CREATE SOURCE IF NOT EXISTS demo_orders_src
  FROM KAFKA CONNECTION demo_kafka (TOPIC '{KAFKA_TOPIC}');

CREATE TABLE IF NOT EXISTS kafka_orders_raw FROM SOURCE demo_orders_src
  KEY FORMAT TEXT VALUE FORMAT JSON
  ENVELOPE UPSERT;

CREATE VIEW IF NOT EXISTS kafka_orders AS
  SELECT (data->>'id')::int          AS id,
         (data->>'customer_id')::int AS customer_id,
         (data->>'amount')::numeric  AS amount,
         data->>'status'             AS status
  FROM kafka_orders_raw;

-- Try it:
--   SELECT * FROM kafka_orders ORDER BY id;
--
-- Produce another record (or overwrite one by reusing its key):
--   kubectl exec -n {NAMESPACE} deploy/redpanda -- sh -c \\
--     'echo "6 {{\\"id\\":6,\\"customer_id\\":1,\\"amount\\":9.99,\\"status\\":\\"new\\"}}" \\
--      | rpk topic produce {KAFKA_TOPIC} -f "%k %v\\n"'

-- Sink results back out to Redpanda:
CREATE MATERIALIZED VIEW IF NOT EXISTS orders_by_status AS
  SELECT status, count(*) AS orders, sum(amount) AS total
  FROM kafka_orders GROUP BY status;

CREATE SINK IF NOT EXISTS orders_by_status_sink
  FROM orders_by_status
  INTO KAFKA CONNECTION demo_kafka (TOPIC 'orders_by_status')
  KEY (status)
  FORMAT JSON ENVELOPE UPSERT;

--   kubectl exec -n {NAMESPACE} deploy/redpanda -- rpk topic consume orders_by_status
"""


# --- iceberg (polaris on rustfs) ----------------------------------------

def polaris_catalog_payload() -> dict:
    """Polaris management-API body creating the catalog on RustFS.

    Catalog `properties` are handed to clients verbatim on loadTable, which is
    how Materialize's Iceberg sink learns the S3 endpoint + static credentials
    for the warehouse (the same mechanism Materialize's own tests rely on).
    """
    base = f"s3://{ICEBERG_BUCKET}/"
    return {
        "name": ICEBERG_CATALOG,
        "type": "INTERNAL",
        "properties": {
            "default-base-location": base,
            "s3.endpoint": RUSTFS_S3_URL,
            "s3.path-style-access": "true",
            "s3.region": "us-east-1",
            "s3.access-key-id": c.RUSTFS_ACCESS_KEY,
            "s3.secret-access-key": c.RUSTFS_SECRET_KEY,
        },
        "storageConfigInfo": {
            "storageType": "S3",
            # Prefixes, not globs; default-base-location must sit inside one.
            "allowedLocations": [base],
            "endpoint": RUSTFS_S3_URL,
            "endpointInternal": RUSTFS_S3_URL,
            "pathStyleAccess": True,
        },
    }


def _polaris_setup_script() -> str:
    """Sidecar loop: once Polaris is up, make sure the catalog and namespace
    exist (idempotent, so it also heals after a restart)."""
    client_id, client_secret = POLARIS_CREDENTIAL.split(":", 1)
    payload = json.dumps(polaris_catalog_payload(), separators=(",", ":"))
    return f"""\
API=http://localhost:8181/api
AUTH="grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}&scope=PRINCIPAL_ROLE:ALL"
PAYLOAD='{payload}'
token() {{
  curl -sf -X POST "$API/catalog/v1/oauth/tokens" -d "$AUTH" \\
    | sed -n 's/.*"access_token":"\\([^"]*\\)".*/\\1/p'
}}
status() {{ curl -s -o /dev/null -w '%{{http_code}}' -H "Authorization: Bearer $T" "$1"; }}
while true; do
  T=$(token) || T=""
  if [ -z "$T" ]; then echo "waiting for polaris..."; sleep 5; continue; fi
  if [ "$(status "$API/management/v1/catalogs/{ICEBERG_CATALOG}")" = "404" ]; then
    echo "creating catalog {ICEBERG_CATALOG} (warehouse s3://{ICEBERG_BUCKET}/ on rustfs)"
    curl -sS --fail-with-body -X POST -H "Authorization: Bearer $T" \\
      -H 'Content-Type: application/json' "$API/management/v1/catalogs" \\
      --data-binary "$PAYLOAD" && echo
  fi
  if [ "$(status "$API/catalog/v1/{ICEBERG_CATALOG}/namespaces/{ICEBERG_NAMESPACE}")" = "404" ]; then
    echo "creating namespace {ICEBERG_NAMESPACE}"
    curl -sS --fail-with-body -X POST -H "Authorization: Bearer $T" \\
      -H 'Content-Type: application/json' "$API/catalog/v1/{ICEBERG_CATALOG}/namespaces" \\
      --data-binary '{{"namespace":["{ICEBERG_NAMESPACE}"]}}' && echo
  fi
  sleep 60
done
"""


# Feature flags need quoted keys, which Kubernetes env var names can't carry, so
# they go in Quarkus' application.properties (mounted where the Polaris image
# and its Helm chart expect it). Everything else is plain env below.
_POLARIS_PROPERTIES = """\
# Allow the plain-http S3 endpoint of the in-cluster RustFS.
polaris.features."ALLOW_INSECURE_STORAGE_TYPES"=true
polaris.features."SUPPORTED_CATALOG_STORAGE_TYPES"=["FILE","S3","GCS","AZURE"]
polaris.readiness.ignore-severe-issues=true
"""


def iceberg_manifest(cfg: Config, seed: bool = False) -> str:
    # Nothing to seed here: demo data for Iceberg lives on the Materialize side
    # (demo-iceberg.sql). The flavor label is still recorded for consistency.
    cpu, mem = _res(cfg, "addon-iceberg")
    db_cpu, db_mem = _res(cfg, "addon-iceberg-db")
    jdbc_url = f"jdbc:postgresql://iceberg-catalog-db.{NAMESPACE}.svc.cluster.local:5432/polaris"
    realm, root_id, root_secret = POLARIS_REALM, *POLARIS_CREDENTIAL.split(":", 1)
    datasource_env = f"""\
- name: POLARIS_PERSISTENCE_TYPE
  value: relational-jdbc
- name: QUARKUS_DATASOURCE_JDBC_URL
  value: "{jdbc_url}"
- name: QUARKUS_DATASOURCE_USERNAME
  value: polaris
- name: QUARKUS_DATASOURCE_PASSWORD
  value: polaris
- name: POLARIS_PERSISTENCE_RELATIONAL_JDBC_MAX_RETRIES
  value: "5"
- name: POLARIS_PERSISTENCE_RELATIONAL_JDBC_INITIAL_DELAY_IN_MS
  value: "100"
- name: POLARIS_PERSISTENCE_RELATIONAL_JDBC_MAX_DURATION_IN_MS
  value: "5000"
- name: POLARIS_BOOTSTRAP_CREDENTIALS
  value: "{realm},{root_id},{root_secret}\""""
    bootstrap_cmd = (
        f"/opt/jboss/container/java/run/run-java.sh bootstrap "
        f"--realm {realm} --credential {realm},{root_id},{root_secret}; "
        # 3 = "realm already bootstrapped" (a normal restart); anything else is real.
        "rc=$?; if [ $rc -eq 0 ] || [ $rc -eq 3 ]; then exit 0; fi; exit $rc")
    ensure_bucket = (
        f"until mc alias set r {RUSTFS_S3_URL} {c.RUSTFS_ACCESS_KEY} "
        f"{c.RUSTFS_SECRET_KEY}; do echo 'waiting for rustfs...'; sleep 5; done; "
        f"mc mb --ignore-existing r/{ICEBERG_BUCKET}")
    return f"""\
{_NAMESPACE_YAML}{_pvc("iceberg-catalog-db-data", "2Gi")}---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: iceberg-catalog-db
  namespace: {NAMESPACE}
  labels:
    {DEMO_LABEL}: "{_flavor(seed)}"
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: iceberg-catalog-db
  template:
    metadata:
      labels:
        app: iceberg-catalog-db
    spec:
      containers:
      - name: postgres
        image: {POSTGRES_IMAGE}
        env:
        - name: POSTGRES_USER
          value: polaris
        - name: POSTGRES_PASSWORD
          value: polaris
        - name: POSTGRES_DB
          value: polaris
        - name: PGDATA
          value: /var/lib/postgresql/data/pgdata
        ports:
        - containerPort: 5432
          name: postgres
        readinessProbe:
          exec:
            command: ["pg_isready", "-U", "polaris", "-d", "polaris"]
          initialDelaySeconds: 5
          periodSeconds: 5
        {_indent(_resources_yaml(db_cpu, db_mem), 8).lstrip()}
        volumeMounts:
        - name: data
          mountPath: /var/lib/postgresql/data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: iceberg-catalog-db-data
{_service("iceberg-catalog-db", [("postgres", 5432)])}---
apiVersion: v1
kind: ConfigMap
metadata:
  name: iceberg-catalog-config
  namespace: {NAMESPACE}
data:
  application.properties: |
{_indent(_POLARIS_PROPERTIES, 4)}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: iceberg-catalog
  namespace: {NAMESPACE}
  labels:
    {DEMO_LABEL}: "{_flavor(seed)}"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: iceberg-catalog
  template:
    metadata:
      labels:
        app: iceberg-catalog
    spec:
      initContainers:
      # The warehouse bucket in RustFS (the S3 store persist already uses).
      - name: ensure-bucket
        image: {MC_IMAGE}
        command: ["/bin/sh", "-c"]
        args: ["{ensure_bucket}"]
      - name: wait-for-db
        image: {POSTGRES_IMAGE}
        command: ["/bin/sh", "-c"]
        args: ["until pg_isready -h iceberg-catalog-db -p 5432 -U polaris; do echo 'waiting for db...'; sleep 2; done"]
      # Creates the realm + root principal in the metastore (idempotent).
      - name: bootstrap
        image: {POLARIS_ADMIN_IMAGE}
        command: ["/bin/sh", "-c"]
        args: ["{bootstrap_cmd}"]
        env:
{_indent(datasource_env, 8)}
      containers:
      - name: polaris
        image: {POLARIS_IMAGE}
        env:
{_indent(datasource_env, 8)}
        # Polaris' own S3 client (used for storage validation / credential vending).
        - name: AWS_REGION
          value: us-east-1
        - name: AWS_ACCESS_KEY_ID
          value: "{c.RUSTFS_ACCESS_KEY}"
        - name: AWS_SECRET_ACCESS_KEY
          value: "{c.RUSTFS_SECRET_KEY}"
        - name: JAVA_MAX_MEM_RATIO
          value: "60"
        ports:
        - containerPort: 8181
          name: api
        - containerPort: 8182
          name: management
        readinessProbe:
          httpGet:
            path: /q/health/ready
            port: 8182
          initialDelaySeconds: 15
          periodSeconds: 5
          failureThreshold: 60
        livenessProbe:
          httpGet:
            path: /q/health/live
            port: 8182
          initialDelaySeconds: 90
          periodSeconds: 10
          failureThreshold: 6
        {_indent(_resources_yaml(cpu, mem), 8).lstrip()}
        volumeMounts:
        - name: config
          mountPath: /deployments/config
          readOnly: true
      # Keeps the catalog + namespace present (creates them once Polaris is
      # up; a no-op afterwards).
      - name: setup
        image: {CURL_IMAGE}
        command: ["/bin/sh", "-c"]
        args:
        - |
{_indent(_polaris_setup_script(), 10)}
        resources:
          requests:
            cpu: "10m"
            memory: "16Mi"
          limits:
            memory: "64Mi"
      volumes:
      - name: config
        configMap:
          name: iceberg-catalog-config
{_service("iceberg-catalog", [("api", 8181), ("management", 8182)])}"""


def _iceberg_inspect_hints() -> str:
    return f"""\
-- Inspect the catalog (tables in the `{ICEBERG_NAMESPACE}` namespace):
--   kubectl port-forward -n {NAMESPACE} svc/iceberg-catalog 8181:8181
--   TOKEN=$(curl -s -X POST localhost:8181/api/catalog/v1/oauth/tokens \\
--     -d 'grant_type=client_credentials&client_id=root&client_secret=root&scope=PRINCIPAL_ROLE:ALL' \\
--     | sed 's/.*"access_token":"\\([^"]*\\)".*/\\1/')
--   curl -s -H "Authorization: Bearer $TOKEN" \\
--     localhost:8181/api/catalog/v1/{ICEBERG_CATALOG}/namespaces/{ICEBERG_NAMESPACE}/tables
-- ...and the Parquet/metadata files in RustFS:
--   kubectl run -n materialize mc --rm -i --restart=Never --image={MC_IMAGE} --command -- sh -c \\
--     'mc alias set r {RUSTFS_S3_URL} {c.RUSTFS_ACCESS_KEY} {c.RUSTFS_SECRET_KEY} && mc ls -r r/{ICEBERG_BUCKET}/'"""


def iceberg_addon_sql() -> str:
    host = HOSTS["iceberg"]
    return f"""\
-- Materialize -> Iceberg tables via the Polaris REST catalog add-on (namespace
-- {NAMESPACE}): catalog/warehouse `{ICEBERG_CATALOG}`, namespace `{ICEBERG_NAMESPACE}`,
-- data files in the RustFS bucket `{ICEBERG_BUCKET}`. Iceberg sinks are in public
-- preview; REST catalogs need a recent Materialize (validated with v26.41.0).
{_psql_hint("addon-iceberg.sql")}

CREATE CONNECTION IF NOT EXISTS iceberg_conn TO ICEBERG CATALOG (
    CATALOG TYPE = 'rest',
    URL = 'http://{host}:8181/api/catalog',
    CREDENTIAL = '{POLARIS_CREDENTIAL}',
    WAREHOUSE = '{ICEBERG_CATALOG}',
    SCOPE = 'PRINCIPAL_ROLE:ALL'
);

-- CREATE SINK my_sink FROM my_view
--   INTO ICEBERG CATALOG CONNECTION iceberg_conn (NAMESPACE = '{ICEBERG_NAMESPACE}', TABLE = 'my_table')
--   KEY (id) MODE UPSERT WITH (COMMIT INTERVAL = '1m');

{_iceberg_inspect_hints()}
"""


def iceberg_demo_sql() -> str:
    host = HOSTS["iceberg"]
    return f"""\
-- Materialize -> Iceberg tables via the demo Polaris REST catalog (namespace
-- {NAMESPACE}), warehouse on the in-cluster RustFS bucket `{ICEBERG_BUCKET}`.
-- Iceberg sinks are in public preview; REST catalogs like Polaris need a recent
-- Materialize (validated with v26.41.0, mzk3's default). Run against Materialize:
{_psql_hint("demo-iceberg.sql")}

CREATE CONNECTION IF NOT EXISTS demo_iceberg TO ICEBERG CATALOG (
    CATALOG TYPE = 'rest',
    URL = 'http://{host}:8181/api/catalog',
    CREDENTIAL = '{POLARIS_CREDENTIAL}',
    WAREHOUSE = '{ICEBERG_CATALOG}',
    SCOPE = 'PRINCIPAL_ROLE:ALL'
);

-- Something to sink. Any table, source, or materialized view works.
CREATE TABLE IF NOT EXISTS iceberg_demo_orders (
    id int, customer_id int, amount numeric(10,2), status text
);
INSERT INTO iceberg_demo_orders VALUES
  (1, 1, 120.00, 'shipped'), (2, 1, 35.50, 'new'), (3, 2, 799.99, 'paid'),
  (4, 3, 12.25, 'new'), (5, 3, 64.00, 'cancelled');

-- Append mode: every change becomes a row (+ _mz_diff / _mz_timestamp columns).
CREATE SINK IF NOT EXISTS iceberg_orders_log
  FROM iceberg_demo_orders
  INTO ICEBERG CATALOG CONNECTION demo_iceberg (
    NAMESPACE = '{ICEBERG_NAMESPACE}',
    TABLE = 'orders_log'
  )
  MODE APPEND
  WITH (COMMIT INTERVAL = '10s');

-- Upsert mode: the Iceberg table mirrors the current state, keyed on a column
-- Materialize can prove unique (here the GROUP BY key).
CREATE MATERIALIZED VIEW IF NOT EXISTS iceberg_orders_by_status AS
  SELECT status, count(*) AS orders, sum(amount) AS total
  FROM iceberg_demo_orders GROUP BY status;

CREATE SINK IF NOT EXISTS iceberg_orders_by_status_sink
  FROM iceberg_orders_by_status
  INTO ICEBERG CATALOG CONNECTION demo_iceberg (
    NAMESPACE = '{ICEBERG_NAMESPACE}',
    TABLE = 'orders_by_status'
  )
  KEY (status)
  MODE UPSERT
  WITH (COMMIT INTERVAL = '10s');

-- Watch it work:
--   SELECT name, status, error FROM mz_internal.mz_sink_statuses WHERE name LIKE 'iceberg_%';
--   UPDATE iceberg_demo_orders SET status = 'shipped' WHERE id = 2;
--
{_iceberg_inspect_hints()}
"""


# --- registry ------------------------------------------------------------

MANIFESTS = {
    "postgres": postgres_manifest,
    "mysql": mysql_manifest,
    "kafka": kafka_manifest,
    "iceberg": iceberg_manifest,
}
ADDON_SQL = {
    "postgres": postgres_addon_sql,
    "mysql": mysql_addon_sql,
    "kafka": kafka_addon_sql,
    "iceberg": iceberg_addon_sql,
}
DEMO_SQL = {
    "postgres": postgres_demo_sql,
    "mysql": mysql_demo_sql,
    "kafka": kafka_demo_sql,
    "iceberg": iceberg_demo_sql,
}


def manifest_path(component: str) -> str:
    return f"addon-{component}.yaml"


def sql_path(component: str, seed: bool) -> str:
    return f"{'demo' if seed else 'addon'}-{component}.sql"


def endpoint_lines(component: str) -> list[str]:
    """Human-readable connection details for the install banner."""
    h = HOSTS[component]
    return {
        "postgres": [f"PostgreSQL:  {h}:5432  db={DB_NAME}  user={MZ_USER}/{MZ_PASSWORD}"
                     f"  (admin: postgres/{PG_ADMIN_PASSWORD})"],
        "mysql": [f"MySQL:       {h}:3306  db={DB_NAME}  user={MZ_USER}/{MZ_PASSWORD}"
                  f"  (root/{MYSQL_ROOT_PASSWORD})"],
        "kafka": [f"Kafka:       {h}:9092 (PLAINTEXT)",
                  f"Schema reg.: http://{h}:8081"],
        "iceberg": [f"Iceberg:     http://{h}:8181/api/catalog  credential "
                    f"{POLARIS_CREDENTIAL}  warehouse={ICEBERG_CATALOG}  "
                    f"namespace={ICEBERG_NAMESPACE}",
                    f"             warehouse data: s3://{ICEBERG_BUCKET}/ on RustFS"],
    }[component]


# --- orchestration -------------------------------------------------------

def present(runner: Runner) -> dict[str, bool]:
    """Deployed add-ons -> whether they were seeded with demo data."""
    res = runner.run(
        ["kubectl", "get", "deployment", "-n", NAMESPACE, "-o",
         "jsonpath={range .items[*]}{.metadata.name}={.metadata.labels."
         + DEMO_LABEL.replace(".", "\\.") + "}{\"\\n\"}{end}"],
        check=False, capture=True)
    if not res.ok:
        return {}
    flavors: dict[str, str] = {}
    for line in res.stdout.split():
        name, _, flavor = line.partition("=")
        flavors[name] = flavor
    return {comp: flavors[dep] == "true"
            for comp, dep in PRIMARY_DEPLOYMENT.items() if dep in flavors}


def install(runner: Runner, cfg: Config, components: list[str],
            demo: set[str] = frozenset()) -> None:
    """Apply the manifests for `components` (seeded where in `demo`), wait for
    them, and write the matching SQL files."""
    components = [comp for comp in ADDONS if comp in components]
    if not components:
        return
    desc = ", ".join(f"{comp} (demo data)" if comp in demo else comp
                     for comp in components)
    log.step(f"Deploying add-ons: {desc}...")
    for comp in components:
        seed = comp in demo
        path = manifest_path(comp)
        if not runner.dry_run:
            Path(path).write_text(MANIFESTS[comp](cfg, seed))
        if comp == "kafka":
            # Job specs are immutable; drop any old seed job so re-applying
            # (re-)runs it when seeding (the script only seeds a brand-new
            # topic) and leaves nothing behind when not.
            runner.run(["kubectl", "delete", "job", SEED_JOB, "-n", NAMESPACE,
                        "--ignore-not-found"], check=False)
        runner.run(["kubectl", "apply", "-f", path])

    log.info("Waiting for add-ons to be ready...")
    for comp in components:
        for dep in DEPLOYMENTS[comp]:
            if not runner.run(["kubectl", "rollout", "status", f"deployment/{dep}",
                               "-n", NAMESPACE, "--timeout=600s"], check=False).ok:
                log.warn(f"{dep} not ready yet. Check: kubectl get pods -n {NAMESPACE}")
    if "kafka" in components and "kafka" in demo:
        if not runner.run(["kubectl", "wait", "--for=condition=complete",
                           f"job/{SEED_JOB}", "-n", NAMESPACE, "--timeout=300s"],
                          check=False).ok:
            log.warn("Topic seed job did not finish. Check: kubectl logs -n "
                     f"{NAMESPACE} job/{SEED_JOB}")

    files = []
    for comp in components:
        seed = comp in demo
        files.append(sql_path(comp, seed))
        if not runner.dry_run:
            Path(files[-1]).write_text((DEMO_SQL if seed else ADDON_SQL)[comp]())
    log.info(f"Add-ons deployed: {desc}")
    log.info("Materialize statements to connect to them: " + ", ".join(files))
