# Research notes (sourced)

The evidence base for `docs/design.md`. Section numbers are referenced from the
design as `[RN §N]`. Method note: gathered via a parallel multi-agent web
research pass; `WebFetch` was blocked in that environment, so claims were
extracted from search results over the canonical pages and cross-checked across
≥2 sources. URLs below are authoritative — verify exact syntax against the
**version-pinned** docs for your fleet's lowest PG major before relying on it.

---

## §1 Sequences & identity

- `setval(seq, n)` sets `is_called=true`, so the next `nextval` returns
  `n+1` — correct when `n = MAX(id)`. Empty-table-safe reset:
  `setval(pg_get_serial_sequence('sch.tbl','id'), COALESCE((SELECT MAX(id) …),1),
  (SELECT MAX(id) IS NOT NULL …))`.
- `pg_get_serial_sequence(table, col)` resolves **both** SERIAL and identity
  sequences since PG 10; returns NULL if the column owns none.
- Discover ownership in bulk via `pg_depend`: sequence→column, `deptype='a'`
  (serial / `OWNED BY`) or `'i'` (identity).
- `GENERATED ALWAYS AS IDENTITY` rejects explicit inserts unless
  `OVERRIDING SYSTEM VALUE`; `BY DEFAULT` accepts them.
- `ALTER SEQUENCE … RESTART` is transactional and locks concurrent nextval;
  `setval` does not — prefer `setval` for the reset.

Sources: postgresql.org/docs/current/functions-sequence.html ·
…/sql-altersequence.html · …/ddl-identity-columns.html · …/sql-insert.html ·
…/catalog-pg-depend.html · …/sql-createsequence.html ·
commit applying pg_get_serial_sequence to identity columns
(postgresql.org/message-id/E1dsvNA-0006AA-Jh@gemulon.postgresql.org) ·
pgpedia.info/p/pg_get_serial_sequence.html

## §2 The constraint, and what it rules out

Client-only + no superuser + no server install ⇒ available: `COPY STDIN/STDOUT`
(table privileges only), exported snapshots (source-local), client-driven
parallelism, pgcopydb, pg_dump/pg_restore (file-staged). Disqualified:
`postgres_fdw`/`dblink` (CREATE EXTENSION), logical replication
(`wal_level=logical` restart + `pg_hba.conf` + superuser/`pg_create_subscription`),
`session_replication_role` (superuser or granted `SET ON PARAMETER`),
`wal_level=minimal`/`max_wal_size` (server-wide restart/reload).

## §3 Data plane

- **COPY**: `COPY … TO STDOUT` / `FROM STDIN` transmits over the client
  connection (no server file); needs **only ordinary table privileges**, no
  superuser. Server-file `COPY` needs superuser or `pg_read_server_files` etc.
  `binary` format is faster but **not portable** across PG majors/architectures;
  `text`/`csv` are portable. `COPY` is "almost always faster than INSERT"
  (§14.4). psql `\copy` is a client wrapper around `COPY … STDIN/STDOUT`.
- **pg_dump/pg_restore**: client tools; parallel `-j` needs **directory** format
  (dump) / custom|directory (restore); `--snapshot` pins a synchronized
  snapshot. No built-in source→target streaming — file-staged. pgcopydb exists
  to remove that intermediate file.
- **Bulk load (§14.4 "Populating a Database")**: use COPY; one transaction;
  drop & recreate indexes; drop & recreate FKs; raise `maintenance_work_mem`
  (**session-settable, any user** — speeds post-load CREATE INDEX / FK VALIDATE);
  `ANALYZE` after. Out of reach client-only: `max_wal_size` (reload),
  `wal_level=minimal`/archiving (restart, superuser). `UNLOGGED` then
  `SET LOGGED` rewrites + WAL-logs the whole table (erases the saving), lost on
  crash, not replicated.

Sources: postgresql.org/docs/current/sql-copy.html · …/app-pgdump.html ·
…/app-pgrestore.html · …/populate.html ·
crunchydata.com/blog/postgresl-unlogged-tables ·
cybertec-postgresql.com/en/adjusting-maintenance_work_mem/ ·
postgresqlco.nf params (max_wal_size context=sighup, wal_level context=postmaster).

## §4 Consistency — exported snapshot is source-local

- `pg_export_snapshot()` (in `REPEATABLE READ`/`SERIALIZABLE`) returns a text id;
  another session imports it with `SET TRANSACTION SNAPSHOT '<id>'` **as the
  first statement** of a `REPEATABLE READ`/`SERIALIZABLE` txn. Both then see one
  identical view. Valid **only while the exporting txn stays open**.
- **Cannot span hosts.** A snapshot encodes one cluster's XID state
  (xmin/xmax/xip) from that cluster's global counter, protected by that
  cluster's vacuum horizon. A different server has an unrelated XID space.
  PostgreSQL even forbids cross-*database* import within one cluster (Tom Lane:
  the importer's xmin wouldn't hold back vacuum in another DB). Cross-*host* is
  impossible a fortiori. ⇒ no global SCN; consistency is per-source-cluster.
- This is the mechanism `pg_dump -j` and pgcopydb use to keep parallel workers
  consistent (all share one exported source snapshot).

Sources: postgresql.org/docs/current/functions-admin.html ·
…/sql-set-transaction.html · …/transaction-iso.html · …/transaction-id.html ·
hackers thread "Synchronized snapshots versus multiple databases"
(postgresql.org/message-id/10919.1319211397@sss.pgh.pa.us).

## §5 Oracle → PostgreSQL landmines (future heterogeneous work; out of scope here)

- **5.1 case-folding:** Oracle folds unquoted → UPPER; PG → lower. `FOO`/`foo`/
  `"foo"` are the same object in PG; `"FOO"` differs. Policy: normalise to
  unquoted-lowercase; quote only when illegal.
- **5.2 DDL extraction:** no `pg_get_tabledef`. Use `pg_dump --schema-only -t`
  (client binary) or compose from catalogs + `pg_get_indexdef`/
  `pg_get_constraintdef`. Community `pgddl`/`ddlx` are installed objects (avoid).
- **5.3 types:** Oracle `DATE` **has a time component** → map to PG
  `timestamp(0)`, never `date` (silent truncation). `NUMBER`→`numeric` (mapping
  to `bigint` fails on decimals). `CLOB`→`text`, `BLOB`/`RAW`→`bytea`,
  `LONG`→`text`. `VARCHAR2` byte-vs-char length can overflow PG `varchar(n)`.
- **5.4 NULL vs '':** Oracle `''` **is** NULL; PG treats them as distinct.
- **5.5 no flashback:** PG has **no `AS OF SCN`/`AS OF TIMESTAMP`** for arbitrary
  past points — only the current MVCC snapshot. No `TIMESTAMP_TO_SCN`.
- **5.6 schemas:** Oracle schema==user; PG schema is a namespace via
  `search_path` — always schema-qualify.
- **5.7 error model:** PG uses 5-char **SQLSTATE** (Appendix A), not ORA-codes.
  Scoped-idempotency analogue: tolerate specific SQLSTATEs per operation —
  `42P07` duplicate_table, `42710` duplicate_object, `23505` unique_violation,
  `23503` fk_violation, `42P01`/`42703` undefined table/column. Prefer
  `IF NOT EXISTS` / `ON CONFLICT`. **`ADD CONSTRAINT` has no `IF NOT EXISTS`** —
  pre-check `pg_constraint` or catch `42710`.

Sources: postgresql.org/docs/current/sql-syntax-lexical.html ·
…/ddl-schemas.html · …/functions-info.html · …/datatype-datetime.html ·
…/errcodes-appendix.html · …/ddl-generated-columns.html ·
cybertec-postgresql.com/en/mapping-oracle-datatypes-to-postgresql/ ·
ora2pg darold.net/documentation.html (+ issue #767 NUMBER→bigint copy fail) ·
aws empty-string migration · franckpachot.medium.com (no flashback).

## §6 Client-driven parallelism & CTID chunking

- No install-free server scheduler (no `DBMS_PARALLEL_EXECUTE` equivalent;
  can't add `pg_cron`). Parallelism = N libpq connections from the client.
- **Inter-table:** one worker per table (as `pg_dump -j`). **Intra-table:**
  split a large table. `ctid` = physical `(block,tuple)`; chunk by block range
  `WHERE ctid >= '(lo,0)'::tid AND ctid < '(hi,0)'::tid` (half-open;
  open-ended last chunk). Even by **bytes**, no index needed; NOT stable
  (changes on UPDATE/VACUUM) — fine as a transient one-shot boundary only.
- **Key-range** alternative: `WHERE id BETWEEN …` on a unique int column —
  stable, index-driven, but needs the column and can skew on gaps.
- pgcopydb is the reference impl: `--split-tables-larger-than` (byte threshold),
  prefers a unique int column else falls back to CTID; `--table-jobs`/
  `--index-jobs`; all workers `SET TRANSACTION SNAPSHOT` to one exported
  source snapshot. `pg_restore -j` guidance: `jobs+1` connections, start ≈ CPU
  cores, mind `max_connections`.

Sources: postgresql.org/docs/current/ddl-system-columns.html (ctid) ·
pgcopydb.readthedocs.io/en/latest/concurrency.html · …/resume.html ·
…/ref/pgcopydb_clone.html · github.com/dimitri/pgcopydb (issue #872) ·
blog.peerdb.io/how-can-we-make-pgdump-and-pgrestore-5-times-faster ·
postgresql.org/docs/current/app-pgrestore.html.

## §7 Foreign keys & load order

- Topo-sort via `pg_constraint` (`contype='f'`, `conrelid`=child,
  `confrelid`=parent); parents first; detect cycles.
- **`NOT VALID` + `VALIDATE CONSTRAINT`**: add FK without scanning existing rows
  (cheap, short lock); validate later under `SHARE UPDATE EXCLUSIVE` (not
  `ACCESS EXCLUSIVE`) — concurrent DML allowed. Owner privilege; **no superuser**.
- **Deferrable** (`UNIQUE`/`PK`/`REFERENCES`/`EXCLUDE` only):
  `SET CONSTRAINTS ALL DEFERRED` defers to COMMIT — solves cyclic/self-ref
  ordering within one txn; still checks every row. `NOT DEFERRABLE` unaffected.
- **`session_replication_role = replica`** disables FK/system triggers but is
  **superuser-only by default**; PG15+ allows
  `GRANT SET ON PARAMETER session_replication_role TO role`. Treat as
  unavailable; detect-and-fall-back to drop/recreate. `pg_dump --disable-triggers`
  likewise "must be done as superuser."
- §14.4: dropping FKs for the load and re-adding once is the recommended fast
  path (bulk check ≫ row-by-row; in-place FK queue can overflow memory on
  millions of rows).

Sources: postgresql.org/docs/current/sql-altertable.html ·
…/sql-set-constraints.html · …/sql-createtable.html · …/explicit-locking.html ·
…/runtime-config-client.html (session_replication_role privilege) ·
…/populate.html · …/catalog-pg-constraint.html · …/app-pgdump.html ·
stormatics.tech/blogs/the-hidden-bottleneck-in-postgresql-restores-and-its-solution.

## §8 Agent CLI design, credentials, comparables, sourcing

- **Agent-operable CLI:** machine-readable result on stdout, diagnostics on
  stderr; `--json`; structured errors with codes; **stable, small exit-code
  table**; idempotent-by-default; Terraform-style **plan/apply** with a durable
  plan/state + stateless CLI. (clig.dev; Arcjet "Designing a CLI for AI agents";
  openstatus; HashiCorp Terraform.)
- **Credentials (libpq-native, no new store):** `~/.pgpass`
  (`host:port:db:user:password`, wildcards, **chmod 0600**); connection service
  file `~/.pg_service.conf` (`[name] …`, selected via `PGSERVICE`/`service=`).
  Split topology (service) from secret (.pgpass).
- **Comparables:** pgcopydb (best fit, client-side, resumable); pgloader (COPY
  streaming, error-resilient, migration-centric); pg_dump|psql pipe (simplest);
  Bytebase/AWS DMS/Debezium (services or server-side — disqualified by no-install).
- **Finding good PG sources:** version-pinned postgresql.org/docs/<N>; mailing
  lists (pgsql-general/-hackers/-performance); the tool's GitHub Issues/
  CHANGELOG; Planet PostgreSQL aggregator; vendor engineering blogs (Crunchy,
  EDB, Citus/Microsoft, Percona, Cybertec, pganalyze, PeerDB, depesz). Judge by
  version applicability and author/venue; prefer primary docs over tutorials.

Sources: postgresql.org/docs/current/libpq-pgpass.html · …/libpq-pgservice.html ·
clig.dev · blog.arcjet.com/designing-a-cli-for-ai-agents/ ·
openstatus.dev/blog/building-cli-for-human-and-agents ·
developer.hashicorp.com/terraform/cli/commands/apply ·
github.com/dimitri/pgcopydb · pgloader.readthedocs.io · planet.postgresql.org ·
lists.postgresql.org.
