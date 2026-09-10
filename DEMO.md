# Running the demo

A guided tour of Materialize on a laptop: a self-managed instance on k3d plus
realistic upstream systems to connect it to and an Iceberg catalog to sink
into. Everything below was run end to end against Materialize v26.41.0.

This is the **seeded** flavor of mzk3's add-ons (`--install-demo`). The same
resources deploy clean, with no demo data, via `--addon NAME`; see the README's
"Add-ons" section for that.

What you get:

| Component | Role in the demo |
|-----------|------------------|
| PostgreSQL 16 | CDC source via logical replication (seeded `customers` / `orders`) |
| MySQL 8.4 | CDC source via GTID binlog replication (same seeded schema) |
| Redpanda | Kafka source (seeded `demo_orders` topic) and Kafka sink target, with a schema registry |
| Apache Polaris on RustFS | Iceberg REST catalog; Materialize sinks Iceberg tables into it |
| Materialize | The star: sources, incrementally maintained views, sinks |

Everything runs in one k3d cluster with fixed throwaway credentials. It is a
demo environment, not a production template.

## 1. Prerequisites

- Docker (Desktop or Engine) with roughly 6 CPUs and 12 GB available; the
  demo comfortably fits in the defaults of a modern laptop.
- `k3d`, `kubectl`, `helm` v3, `psql`, and `uv` (or `pip`).
- A Materialize **license key** file. Self-managed v26+ will not start without
  one. Free Community keys: https://materialize.com/self-managed/community-license/

```bash
brew install k3d kubectl helm libpq uv      # macOS; add libpq's bin to PATH for psql
uv tool install .                          # from this repo; provides `mzk3`
export MZ_LICENSE_KEY=/path/to/license.key  # or pass -l FILE to every install
```

## 2. Install

One command creates the cluster and deploys Materialize plus all four add-ons
with demo data. Expect 5 to 10 minutes on a first run (image pulls dominate).

```bash
mzk3 install --create-cluster --install-demo
```

Pick a subset with `--install-demo postgres,kafka` (repeatable). `install` is
idempotent, so you can add one later with the same command; `--addon NAME`
adds a clean one instead. Add `--install-dashboards` for Grafana.

When it finishes, the banner lists the endpoints and the SQL files it wrote
into the current directory:

```
demo-postgres.sql  demo-mysql.sql  demo-kafka.sql  demo-iceberg.sql
```

Check that everything is up:

```bash
mzk3 status
kubectl get pods -n addons
```

All add-on pods should be `Running` (the `redpanda-seed` job shows `Completed`).

## 3. Connect to Materialize

Port-forward the balancerd service and connect with any ordinary user name.
System roles such as `mz_system` are refused on this port.

```bash
MZ_BALANCER=$(kubectl -n materialize-environment get svc -o name | grep balancerd)
kubectl port-forward -n materialize-environment "$MZ_BALANCER" 6875:6875 &
psql -h localhost -p 6875 -U materialize materialize
```

For the web console:

```bash
MZ_CONSOLE=$(kubectl -n materialize-environment get svc -o name | grep console)
kubectl port-forward -n materialize-environment "$MZ_CONSOLE" 8080:8080 &
open http://localhost:8080
```

The SQL files are meant to be loaded whole (`psql -f`) or pasted statement by
statement while you narrate. Each one is self-contained.

## 4. PostgreSQL: change data capture

```bash
psql -h localhost -p 6875 -U materialize materialize -f demo-postgres.sql
```

This creates a secret, a Postgres connection, a source on the `mz_source`
publication, and two tables (`pg_customers`, `pg_orders`) that mirror the
upstream tables. Within a few seconds the initial snapshot lands:

```sql
SELECT c.name, count(*) AS orders, sum(o.amount) AS total
FROM pg_orders o JOIN pg_customers c ON c.id = o.customer_id
GROUP BY c.name;
```

Now change the upstream database from another terminal and re-run the query,
or leave `\watch 1` running in psql to see it update live:

```bash
kubectl exec -n addons deploy/postgres -- psql -U postgres -d app \
  -c "INSERT INTO orders (customer_id, amount, status) VALUES (2, 42.00, 'new');"
kubectl exec -n addons deploy/postgres -- psql -U postgres -d app \
  -c "UPDATE orders SET status = 'shipped' WHERE status = 'new';"
```

Talking points: the source is a real logical replication slot (`wal_level =
logical`, `REPLICA IDENTITY FULL`), exactly what a customer would configure on
their own Postgres. `SELECT * FROM mz_internal.mz_source_statuses` shows the
source `running`.

## 5. MySQL: change data capture

```bash
psql -h localhost -p 6875 -U materialize materialize -f demo-mysql.sql
```

Same shape as Postgres: a MySQL connection, a source, and `mysql_customers` /
`mysql_orders`. The upstream is configured the way Materialize's docs ask for
(`gtid_mode=ON`, `binlog_row_metadata=FULL`, a `materialize` replication user).

```bash
kubectl exec -n addons deploy/mysql -- mysql -uroot -pmysql app \
  -e "INSERT INTO orders (customer_id, amount, status) VALUES (2, 42.00, 'new');"
```

```sql
SELECT status, count(*), sum(amount) FROM mysql_orders GROUP BY status;
```

## 6. Kafka (Redpanda): stream in, stream out

```bash
psql -h localhost -p 6875 -U materialize materialize -f demo-kafka.sql
```

This creates Kafka and schema-registry connections, a source on the seeded
`demo_orders` topic (keyed JSON, read with `ENVELOPE UPSERT`), a parsed view
`kafka_orders`, a materialized view `orders_by_status`, and a **sink** that
writes that view back to Redpanda as upsert JSON.

Produce a record with a new key (an insert) and then reuse a key (an update):

```bash
kubectl exec -n addons deploy/redpanda -- sh -c \
  'echo "6 {\"id\":6,\"customer_id\":1,\"amount\":9.99,\"status\":\"new\"}" \
   | rpk topic produce demo_orders -f "%k %v\n"'
kubectl exec -n addons deploy/redpanda -- sh -c \
  'echo "6 {\"id\":6,\"customer_id\":1,\"amount\":9.99,\"status\":\"paid\"}" \
   | rpk topic produce demo_orders -f "%k %v\n"'
```

```sql
SELECT * FROM kafka_orders ORDER BY id;
SELECT * FROM orders_by_status;
```

And read the sink's output topic to show the round trip:

```bash
kubectl exec -n addons deploy/redpanda -- rpk topic consume orders_by_status -n 4 -f '%k -> %v\n'
```

## 7. Iceberg: sink to a lakehouse table

```bash
psql -h localhost -p 6875 -U materialize materialize -f demo-iceberg.sql
```

This creates an Iceberg catalog connection to Polaris (`CATALOG TYPE = 'rest'`,
warehouse `lakehouse`), a small input table, and two sinks into the `app`
namespace:

- `iceberg_orders_log`: **append** mode, every change becomes a row with
  `_mz_diff` / `_mz_timestamp` columns (table `orders_log`).
- `iceberg_orders_by_status_sink`: **upsert** mode from a `GROUP BY` view,
  keyed on `status` (table `orders_by_status`).

Both commit every 10 seconds. Check they are running:

```sql
SELECT name, status, error FROM mz_internal.mz_sink_statuses WHERE type = 'iceberg';
UPDATE iceberg_demo_orders SET status = 'shipped' WHERE id = 2;
```

Show the tables in the catalog:

```bash
kubectl port-forward -n addons svc/iceberg-catalog 8181:8181 &
TOKEN=$(curl -s -X POST localhost:8181/api/catalog/v1/oauth/tokens \
  -d 'grant_type=client_credentials&client_id=root&client_secret=root&scope=PRINCIPAL_ROLE:ALL' \
  | sed 's/.*"access_token":"\([^"]*\)".*/\1/')
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:8181/api/catalog/v1/lakehouse/namespaces/app/tables
```

And the Parquet data, delete files, and snapshots in object storage:

```bash
kubectl run -n materialize mc --rm -i --restart=Never --image=minio/mc --command -- sh -c \
  'mc alias set r http://rustfs-io.materialize.svc.cluster.local:9000 minioadmin minioadmin \
   && mc ls -r r/iceberg/app/'
```

Talking points: Polaris is the same REST catalog Materialize's own Iceberg
tests run against, and RustFS stands in for S3. Any Iceberg reader (Spark,
Trino, DuckDB, pyiceberg) pointed at `http://iceberg-catalog.addons.svc.cluster.local:8181/api/catalog`
with credential `root:root` (warehouse `lakehouse`) can query the tables.

## 8. Putting it together

Sources from different systems join like ordinary tables. The seeded data
shares customer ids, so Postgres customers join Kafka orders directly:

```sql
SELECT c.name, o.status, o.amount
FROM kafka_orders o JOIN pg_customers c ON c.id = o.customer_id
ORDER BY o.id;

CREATE MATERIALIZED VIEW customer_activity AS
SELECT c.name,
       count(o.id)   AS kafka_orders,
       sum(o.amount) AS kafka_total
FROM pg_customers c LEFT JOIN kafka_orders o ON o.customer_id = c.id
GROUP BY c.name;
```

Produce another Kafka record or rename a customer in Postgres and watch
`customer_activity` change. Sink it to Kafka or Iceberg with the same
`CREATE SINK` shapes used above.

## 9. Resetting

| Goal | Command |
|------|---------|
| Drop everything you created in Materialize, keep the upstreams | `DROP SOURCE demo_pg_src CASCADE;` etc., or `mzk3 destroy-environment --yes` then `mzk3 recreate-environment --yes` |
| Re-apply the add-on manifests (pinned images, same flavors) | `mzk3 upgrade --component addons` |
| Re-seed a demo database from scratch | `kubectl delete -n addons deploy/postgres pvc/postgres-data` then `mzk3 upgrade --component addons` (same for `mysql`/`mysql-data`) |
| Re-seed the Kafka topic | `kubectl exec -n addons deploy/redpanda -- rpk topic delete demo_orders` then `mzk3 upgrade --component addons` |
| Swap a seeded add-on for a clean one | delete its deployment + PVC as above, then `mzk3 upgrade --component addons --addon NAME` |
| Tear down the whole cluster | `k3d cluster delete mzk3-cluster` |

The add-on PostgreSQL, MySQL, Redpanda, and Polaris metastore are all PVC-backed,
so data and Iceberg table metadata survive pod restarts and laptop reboots.

## 10. Troubleshooting

- **A source is not `running`.** `SELECT name, status, error FROM
  mz_internal.mz_source_statuses;` shows the reason. Upstream pods:
  `kubectl get pods -n addons` and `kubectl logs -n addons deploy/<name>`.
- **Nothing in `materialize-environment`.** The instance needs a valid
  license key; `kubectl logs -n materialize deploy/my-materialize-operator`
  shows the rejection. Re-run install with `-l FILE`.
- **Iceberg sink stuck.** `kubectl logs -n addons deploy/iceberg-catalog -c
  polaris` for catalog errors and `-c setup` for the catalog/namespace
  bootstrap. The sink needs the `lakehouse` catalog and `app` namespace to
  exist; the `setup` sidecar recreates them if missing.
- **Seed job did not run.** `kubectl logs -n addons job/redpanda-seed`. It only
  seeds a brand-new topic.
- **environmentd or a clusterd pod shows a restart after the laptop slept.**
  Expected. When the machine suspends, persist read leases expire and the
  process halts itself on purpose (`halting process: Listen ... advanced past
  the listen frontier` in the previous container's log), then comes back
  within a minute with sources and sinks intact. Reconnect psql and carry on.
- **Slow laptop.** Resource requests are in the README's config-file section
  (`resources.addon-*`); Redpanda and Polaris are the heaviest.

## Reference

| Component | In-cluster endpoint | Credentials |
|-----------|---------------------|-------------|
| PostgreSQL | `postgres.addons.svc.cluster.local:5432`, db `app` | `materialize` / `materialize` (admin `postgres` / `postgres`) |
| MySQL | `mysql.addons.svc.cluster.local:3306`, db `app` | `materialize` / `materialize` (`root` / `mysql`) |
| Redpanda | `redpanda.addons.svc.cluster.local:9092` (PLAINTEXT), schema registry `:8081` | none |
| Polaris | `http://iceberg-catalog.addons.svc.cluster.local:8181/api/catalog`, warehouse `lakehouse`, namespace `app` | client credential `root:root`, scope `PRINCIPAL_ROLE:ALL` |
| RustFS (S3) | `http://rustfs-io.materialize.svc.cluster.local:9000`, bucket `iceberg` | `minioadmin` / `minioadmin` |
| Materialize | balancerd `:6875` (SQL), console `:8080` | any user name, no password |
