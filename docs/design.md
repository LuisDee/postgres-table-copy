# Design: client-side, fleet-scale PostgreSQL table copy

Status: **draft / proposed contract.** This is the document subsequent stages
execute against. Implementation lands in small, reviewable stages — waves →
phases → stages (§6). Do not collapse stages. The research this design rests on,
with sources, is in `docs/research-notes.md`; section references like `[RN §2]`
point there.

**Companion specs (this contract is split for clarity):**

- `docs/performance.md` — bottleneck model, ranked lightning levers, benchmark
  targets. *Speed gate.*
- `docs/validation.md` — validation modes, failure-paths, atomic stage-swap.
- `docs/security.md` — TLS/credential/least-privilege defaults, the RLS landmine.
  *Security gate.*
- `docs/architecture-review.md` — the standing adversarial punch-list.
- `docs/schemas/*.schema.json` — canonical data contracts (envelope, plan,
  job-state). Code and schemas move in lock-step.

Delivery follows `spec → plan → TDD → adversarial review` with security & speed
as standing gates (see `CLAUDE.md`).

---

## 1. Goal

A **client-side, cross-host, cross-schema, parallel multi-table PostgreSQL copy
CLI, designed for agents to drive**, operated across a fleet of ~100
self-managed Postgres databases.

It is a concept-port of `oracle-schema-refresh`: keep the phased engine, the
agent-facing CLI, the "one consistent read per job" discipline, and the
cut-based delivery; rebuild the data plane for PostgreSQL.

### Constraints

- **Install nothing on the database servers.** No extensions (`postgres_fdw`,
  `dblink`, `pglogical`), no host shell, no `postgresql.conf` / `pg_hba.conf`
  edits, no server restart, **no assumed superuser**. Client-side libpq/SQL
  only. This is the dominant constraint; it filters every mechanism below.
- **Lightweight.** Thin Python orchestrator; the data plane is libpq `COPY`
  streaming of opaque buffers. `psycopg` (v3) only.
- **Stateless CLI, durable job state.** Killing the CLI, moving machines, or a
  network blip must not lose a job. State is durable and resumable (§3.6).
- **Agent-friendly.** JSON I/O on every command, stable exit codes, planned
  side effects shown before execution, polling-friendly status.

### Out of scope (see §10 for the full list)

- Heterogeneous sources (Oracle, MySQL → PG). PostgreSQL ↔ PostgreSQL only.
- Anything requiring server-side install, superuser, or a restart as a hard
  dependency.
- Continuous replication / CDC. One-shot full copy per job.

---

## 2. The constraint, and what it rules in and out

`[RN §2]` Everything follows from "no install, no superuser, client-only":

| Mechanism | Verdict | Why |
|---|---|---|
| `COPY … TO STDOUT` / `FROM STDIN` | ✅ **core data plane** | Streams over the wire; **needs only table privileges** |
| Exported snapshot (`pg_export_snapshot` + `SET TRANSACTION SNAPSHOT`) | ✅ source consistency | The "one SCN" analogue — but **source-cluster-local only** (§3.2) |
| Client-driven parallelism (N conns, CTID / key ranges) | ✅ | No server scheduler needed |
| `pgcopydb` | ✅ (ship the client binary) | Purely client-side; no server component |
| `pg_dump -Fd -j` / `pg_restore -j` | ✅ but file-staged | Client-side; writes an intermediate directory archive |
| `postgres_fdw` / `dblink` | ❌ disqualified | Require `CREATE EXTENSION` on the server |
| Logical replication (pub/sub) | ❌ disqualified | Needs `wal_level=logical` (**restart**), `pg_hba.conf`, superuser/`pg_create_subscription` |
| `session_replication_role = replica` | ⚠️ don't rely on | Needs superuser **or** granted `SET ON PARAMETER` (PG15+) |
| `wal_level=minimal` / `max_wal_size` load tuning | ❌ at server level | Restart/reload GUCs, server-wide |
| `UNLOGGED` then `SET LOGGED` | ⚠️ rarely | `SET LOGGED` rewrites + WAL-logs the whole table, erasing the saving; lost on crash; not replicated |

---

## 3. Architectural decisions

### 3.1 Client-streamed data plane (the rule inversion)

`[RN §3]` Oracle moves data server-side; the parent project's rule #5 is "no row
data through the client." **Client-only PostgreSQL cannot satisfy that** — the
only cross-host pipe is:

```
COPY (SELECT <explicit cols> FROM src.tbl) TO STDOUT (FORMAT binary)   -- source conn
   → raw CopyData buffers →
COPY tgt.tbl (<explicit cols>) FROM STDIN (FORMAT binary)              -- target conn
```

Decision: **stream opaque `CopyData` buffers** between a source and target
connection. Never parse, deserialise, transform, or materialise rows as Python
objects. The bytes transit the client; the *semantics* of "don't process rows
in Python" are preserved.

- **Format:** default `FORMAT binary` for speed/correctness when source and
  target majors match; fall back to `FORMAT text`/`csv` when versions differ,
  because binary is not portable across PG majors/architectures `[RN §3]`.
  The chooser records the format decision in the plan.
- **Same-server schema→schema copy** can still go through the client `COPY`
  pipe; an intra-server `INSERT … SELECT` is a later optimisation (one
  connection, no client round-trip) but is **not** the default because it
  doesn't generalise to cross-host.
- **Explicit column lists** from `information_schema.columns` /
  `pg_attribute`, never `COPY (SELECT *)`. Exclude `GENERATED … STORED`
  columns (PG rejects inserts into them). For identity columns see §3.7.

### 3.2 Consistency model — source-local snapshot

`[RN §4]` A PostgreSQL exported snapshot encodes one cluster's XID state and is
valid **only within the same database of the same cluster** — it cannot be
imported on another host (Tom Lane's vacuum-horizon rationale on
pgsql-hackers). **There is no global SCN.**

Decision — the job consistency contract:

1. On the **source**, open one `REPEATABLE READ` transaction, call
   `pg_export_snapshot()`, keep it open for the whole job. This is "one SCN per
   job," source-side.
2. Every parallel **source reader** connection runs
   `BEGIN ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '<id>';`
   before any query, so all readers across all tables/chunks see one identical
   image (exactly how `pg_dump -j` and pgcopydb stay consistent).
3. The **target** is a separate consistency domain. We guarantee "the target
   receives a consistent snapshot *of the source*," not "source and target
   share a point in time." Cross-host atomicity is orchestrated client-side via
   **stage-then-swap** (§3.4).

Corollary `[RN §5.5]`: there is **no `AS OF SCN`/past-point flashback** in PG —
only the *current* MVCC snapshot. If the job didn't capture the snapshot at
start, that point is gone. Design no path that assumes re-reading an earlier
point later (e.g. a "resume against the original snapshot hours later" is
physically impossible; resume re-snapshots and re-validates — §3.6).

### 3.3 Endpoint registry over libpq-native config

`[RN §8]` An `Endpoint` is one named, reusable connection target. To scale to
~100 DBs without inventing a secret store (rule #9):

- **Topology** in `~/.pg_service.conf` (`[name] host=… port=… dbname=…`),
  referenced by service name; the registry maps endpoint → service.
- **Secrets** in `~/.pgpass` (`host:port:db:user:password`, `chmod 0600`).
- The endpoint registry (`~/.pgcopy/endpoints.yaml`, override `$PGCOPY_REGISTRY`
  / `--registry`) stores **only non-secret** fields (name, service, default
  schema, role). Passwords never enter YAML or SQL text.

`pgcopy endpoints add|list|remove|test`. `test` opens a connection, reports
server version, `current_user`, and whether the role can create tables in the
target schema (the privilege the engine actually needs).

### 3.4 Strategy interface

One copy mechanism does not fit all table sizes. A `Strategy` moves one table's
data; the chooser picks per table at plan time.

```python
class Strategy(Protocol):
    name: str
    def applicable(self, profile: TableProfile) -> bool: ...
    def plan(self, src: Endpoint, tgt: Endpoint, table: TableProfile,
             budget: ParallelBudget) -> list[WorkUnit]: ...
    def execute(self, ctx: CopyContext, units: list[WorkUnit]) -> StrategyResult: ...
    def merge(self, ctx, units) -> None: ...    # stage→swap; may be no-op
    def cleanup(self, ctx, units) -> None: ...
```

| Strategy | When picked | Cut |
|---|---|---|
| `pgcopydb_delegate` | **default** for same-name jobs (src schema == tgt schema, no per-table remap): whole-schema, large, or multi-table clones; shell out to pgcopydb | 1 |
| `direct_copy` | the **remap path** — selective table(s) copied into a *different* or *populated* target schema/name; single `COPY` stream | 1 |
| `ctid_chunked` | remap path, large table, no unique int key; split by CTID block range | 2 |
| `key_range_chunked` | remap path, large table with a unique int key; `WHERE id BETWEEN …` | 2 |

**Routing (the build-vs-buy boundary made operational, §7):** pgcopydb already
does consistent, parallel, resumable, CTID/integer-split bulk movement of
*same-named* objects (verified: it auto-splits a >threshold table on its integer
key under `--split-tables-larger-than`). It has **no rename/remap** — it clones
objects under identical names. So the picker delegates same-name work to
pgcopydb from Wave 1, and the bespoke `COPY` engine owns only what pgcopydb
structurally cannot: **table-with-remap into an existing/populated target**,
plus independent verification (§3.10) and fleet/agent orchestration. The
intra-table chunking strategies (Wave 2) exist **solely to parallelise the remap
path** — they are not a general-purpose splitter competing with pgcopydb.

`[RN §6]` Chunking splits a table across N connections. CTID ranges
(`WHERE ctid >= '(lo,0)'::tid AND ctid < '(hi,0)'::tid`, half-open, open-ended
last chunk) give even **byte** distribution from a physical scan and need no
index; integer key ranges are stable and index-driven but need a unique int
column. Threshold to split is a **byte size** (resolve §9 q2), mirroring
pgcopydb's `--split-tables-larger-than`. All chunk readers share the §3.2
snapshot.

### 3.5 Parallelism — client-driven only

`[RN §6]` PostgreSQL has no install-free server-side job scheduler
(`DBMS_PARALLEL_EXECUTE` has no equivalent; we can't add `pg_cron`). Parallelism
is **N libpq connections from the client**, two levels:

- **Inter-table:** one worker per small table (like `pg_dump -j`).
- **Intra-table:** chunk large tables (§3.4), each chunk a worker.

Budget surfaces:
- `--max-parallel N` — global cap on concurrent connections (default 8).
  Remember the connection arithmetic: peak = leader + active workers; must stay
  under target `max_connections` and any PgBouncer pool. `plan` reports
  projected peak connection count.
- `--max-chunks-per-table N` — prevents one table starving the pool.

Index builds are **deferred until after data load** and parallelised
(`CREATE INDEX` per worker), as pgcopydb does.

### 3.6 Job state + resumability

Stateless CLI; durable state. Unlike the parent (which parks state in
target-side tables), default to a **client-side durable job file** under
`~/.pgcopy/jobs/<job_id>.json` — across a fleet of 100, this avoids creating
control tables in every target. (`--state-table` may opt into target-side state
later if a job must survive loss of the client host.)

State records: job_id, source/target endpoints, exported snapshot id, per-table
status (`PLANNED|RUNNING|DONE|FAILED`), strategy, chunk boundaries, row/hash
results, error SQLSTATE + message. `--resume JOB_ID` re-snapshots the source
(the old snapshot is gone — §3.2), recomputes from durable per-table status,
and re-runs incomplete tables. Resume is idempotent: staging uses a
deterministic, discoverable prefix so re-runs reclaim orphans (§3.13 cleanup).

`job_id` format: `j_<YYYYMMDD>_<HHMMSS>_<rand4>` — sorts chronologically,
human-readable. (Carries over the parent's resolved q3.)

### 3.7 Sequences & identity

`[RN §1]` After a load that carried literal id values, the backing sequence is
stale → next insert collides. Reset per identity/serial column with the
empty-table-safe form:

```sql
SELECT setval(
  pg_get_serial_sequence('sch.tbl','id'),
  COALESCE((SELECT MAX(id) FROM sch.tbl), 1),
  (SELECT MAX(id) IS NOT NULL FROM sch.tbl)   -- is_called: false when empty
);
```

`pg_get_serial_sequence` resolves **both** SERIAL and identity sequences
(since PG 10). Discover owned sequences via `pg_get_serial_sequence` per column
(simple) or a `pg_depend` join covering `deptype IN ('a','i')` (bulk;
`'a'`=serial/OWNED BY, `'i'`=identity). To carry explicit ids into a
`GENERATED ALWAYS AS IDENTITY` target, the `COPY`/`INSERT` uses
`OVERRIDING SYSTEM VALUE`; `BY DEFAULT` accepts values freely.

### 3.8 Foreign keys & load order

`[RN §7]` Two concerns: avoid per-row FK cost, and order tables correctly.

- **Ordering:** topologically sort tables by FK edges from `pg_constraint`
  (`contype='f'`, `conrelid`=child, `confrelid`=parent); load parents first;
  Kahn's algorithm with cycle detection.
- **Default FK strategy (no superuser):** drop inbound FKs, load, then re-add as
  `ADD CONSTRAINT … NOT VALID` + `VALIDATE CONSTRAINT` (the validate scan takes
  only `SHARE UPDATE EXCLUSIVE`, not `ACCESS EXCLUSIVE`). Owner privilege
  suffices.
- **Cyclic / self-referential FKs:** if the constraints are `DEFERRABLE`, wrap
  the load in one transaction with `SET CONSTRAINTS ALL DEFERRED`; otherwise
  drop-and-recreate.
- **Do NOT depend on `session_replication_role = replica`** to skip FK triggers
  — it needs superuser or a pre-granted `SET ON PARAMETER` (rule #3).
  Detect-and-fall-back to drop/recreate.

### 3.9 DDL extraction (no `DBMS_METADATA.GET_DDL`)

`[RN §5.2 / RN-direct §4]` PostgreSQL has **no built-in full-table DDL
function**. Options:

- **Default — shell out to `pg_dump --schema-only -t sch.tbl`** (a client
  binary; allowed). Run a `pg_dump` ≥ the server major. Parse output to split
  (a) bare table + columns + defaults + PK from (b) secondary indexes + FKs, so
  they apply in the §3.8 order. Mind `-t` quoting (case-folding, rule #6).
- **Targeted pieces at runtime** via built-ins that *do* exist:
  `pg_get_indexdef`, `pg_get_constraintdef`, `pg_get_serial_sequence`.
- **Avoid** community DDL functions (`pgddl`/`ddlx`) — they're installed
  objects, violating the no-install rule.

### 3.10 Validation — the differentiator

`[RN §1 parent-gap]` The parent project specced `rows`/`hash`/`sample` but
shipped only row-count. We **ship all three**, both sides reading the source
snapshot where applicable:

| Mode | What | Cost |
|---|---|---|
| `rows` (default) | `COUNT(*)` source-snapshot vs target | cheap |
| `hash` | `md5(string_agg(t::text, '' ORDER BY pk))` or per-chunk `sum(hashtext(...))` | full scan |
| `sample` | hash over every Nth row / a few chunks | light |

Mismatch marks the table `FAILED` regardless of whether the load raised. This
independent check is something `pgcopydb` does not provide and is the main
reason to build rather than purely delegate.

### 3.11 Bulk-load tuning (only what a normal user owns)

`[RN §3 bulk-load]` Levers we actually control client-side, no superuser:
`COPY` (not INSERT) + single transaction + drop/recreate indexes & FKs +
`SET maintenance_work_mem` (session-settable; speeds post-load `CREATE INDEX`
and FK `VALIDATE`) + post-load `ANALYZE`. Optionally `SET synchronous_commit =
off` in-session (small crash-window risk). **Out of reach** (surface as
"operator may pre-tune," never tool actions): `max_wal_size`,
`wal_level=minimal`, archiving, `max_wal_senders`.

### 3.12 Semantic policy

`[RN §5]` Fixed once, repo-wide:
- **Identifier case:** normalise to unquoted-lowercase; quote only when illegal
  as lowercase (rule #6). Never round-trip arbitrary case.
- **Schema-qualify everything**; never rely on `search_path`. Map source schema
  → target schema explicitly.
- **Type fidelity** matters even PG→PG when crossing majors/options; preserve
  exact column types from the source catalog. (Cross-engine mapping — Oracle
  `DATE`→`timestamp`, `NUMBER`→`numeric`, empty-string≡NULL — is documented in
  `docs/research-notes.md §5` for future heterogeneous work but is out of scope
  here, §10.)
- **Generated columns:** exclude `STORED` from column lists; PG recomputes.

### 3.13 Error handling — scoped SQLSTATE (rule #4)

`[RN §5.7]` Carry the parent's "scope ignore-codes per operation" rule across
to SQLSTATEs: `42P07` duplicate_table, `42710` duplicate_object, `23505`
unique_violation, `23503` fk_violation, `42P01`/`42703` undefined table/column.
Prefer `CREATE … IF NOT EXISTS`, `DROP … IF EXISTS`, `INSERT … ON CONFLICT`.
Gotcha: `ADD CONSTRAINT` has **no `IF NOT EXISTS`** — pre-check `pg_constraint`
or catch `42710` explicitly. Tolerate each SQLSTATE only around the statement
that legitimately produces it; never a global union.

`cleanup JOB_ID` drops staging tables/schemas by the deterministic prefix and
removes the job's durable state — fixing the parent's leaked-staging gap.

---

## 4. CLI contract

`[RN §8]` Two-layer surface, agent-first.

```text
pgcopy endpoints add NAME --service SVC [--schema S] [--role R]
pgcopy endpoints list [--json]
pgcopy endpoints test NAME [--json]

pgcopy plan  --from SRC --to DST
             [--tables t1,t2 | --tables-file F | --schema S]
             [--include-fk-parents]              # default true
             [--strategy auto|direct|ctid|keyrange|pgcopydb]
             [--max-parallel N] [--max-chunks-per-table N]
             [--format binary|text]              # default binary if majors match
             --json
pgcopy run    --plan FILE | --from SRC --to DST --tables …
             [--background] [--resume JOB_ID] --json
pgcopy status JOB_ID [--watch] [--json]
pgcopy verify JOB_ID [--mode rows|hash|sample] [--json]
pgcopy cancel JOB_ID
pgcopy cleanup JOB_ID
pgcopy wait   JOB_ID [--timeout T]

pgcopy copy   --from SRC --to DST --tables …      # plan→run→wait→verify
             [--wait] [--json]
```

### 4.1 JSON envelope (every command with `--json`)

```json
{ "ok": true, "command": "plan", "job_id": "j_20260615_140312_a1b2",
  "data": { }, "error": null, "error_category": null }
```

`error_category` ∈ `CONFIG | AUTH | TRANSIENT | DATA | INTERNAL`. The
`error_category` is derived from the failing SQLSTATE class where applicable
(e.g. `23xxx`→`DATA`, `28xxx`/`42501`→`AUTH`,
`08xxx`/`40xxx`/`53xxx`/`55xxx`/`57xxx`→`TRANSIENT`). **Class `55` is
load-bearing, not optional:** `lock_timeout` aborts the stage-swap with
`55P03 lock_not_available`, and the cutover (`validation.md §4`) *depends on*
that being retryable — so `55xx`→`TRANSIENT` (exit 2), never `INTERNAL`.
(Code: `src/postgres_table_copy/errors.py` `_CLASS_CATEGORY` must include `"55"`;
add a unit test asserting `55P03 → TRANSIENT → exit 2` when that code lands.)

### 4.2 Exit codes

| 0 success | 1 user/config | 2 transient | 3 data error | 4 internal bug |

---

## 5. Module layout (target shape)

```text
src/postgres_table_copy/
  endpoints/        registry.py (service/.pgpass), endpoint.py, privileges.py
  introspect/       fk.py (pg_constraint, topo), profile.py (size/relpages/LOB/
                    generated/identity), ddl.py (pg_dump shell + pg_get_*def),
                    columns.py, sequences.py (pg_depend / pg_get_serial_sequence)
  snapshot.py       export/import one source snapshot per job
  copy/             stream.py (COPY STDOUT→STDIN, opaque buffers), format.py
  strategy/         picker.py, direct_copy.py, ctid_chunked.py,
                    key_range_chunked.py, pgcopydb_delegate.py
  load/             fk.py (drop/NOT VALID/VALIDATE, deferrable), bulk.py
                    (maintenance_work_mem, analyze), stage_swap.py
  verify/           rows.py, hash.py, sample.py
  state/            job.py (durable client-side file), resume.py
  job.py            top-level phase orchestration over many tables
  cli.py            `pgcopy` entry point + subcommands, JSON I/O
  errors.py         SQLSTATE → error_category, scoped-ignore helpers
```

---

## 6. Roadmap — waves → phases → stages

This is a multi-stage build, not a single drop. **Waves** are themes;
**phases** are coherent capabilities within a wave; **stages** are the
one-commit cuts that each land green tests via TDD and never break earlier
stages. Don't pull work forward across waves. Each stage carries an explicit
*exit criterion*.

### Wave 0 — contract & registry ✅ done (`8359d36`)
The day-0 contracts everything builds on; no table data touched.
- **Stage 0.1 — errors & envelope.** `errors.py` (exit codes, `ErrorCategory`,
  exception hierarchy, SQLSTATE→category, `tolerate(*sqlstates)`); JSON envelope.
- **Stage 0.2 — endpoint registry & CLI.** `Endpoint` (topology only; secrets in
  `.pgpass`), `EndpointRegistry` (0600 YAML), `endpoints add/list/remove/test`,
  phased-command stubs. *Exit (met):* 60 unit tests + ruff green; `endpoints
  test` probes a real PG (integration, skips cleanly).

### Wave 1 — single-table copy, correct & consistent
The vertical slice that proves the bespoke remap data plane end-to-end, **plus
delegation to pgcopydb for the same-name case** so we never ship a copy path
slower than the off-the-shelf tool (§7; build-vs-buy boundary). The bespoke
single-stream `direct_copy` is a *correctness* vehicle for remap, not the speed
story — speed comes from delegation (same-name) and Wave 2 chunking (remap).
- **Phase 1A — introspection.** columns (exclude `GENERATED … STORED`),
  identity/sequence discovery (`pg_get_serial_sequence`/`pg_depend`), table
  profile (size via `relpages`, LOB/RLS/partition flags), pre-flight checks
  (encoding/collation drift → warn; RLS → warn). *Exit:* SQL-string unit tests.
- **Phase 1B — snapshot + `direct_copy`.** one `pg_export_snapshot()`; binary
  `COPY TO STDOUT → FROM STDIN` (text fallback on major mismatch) streaming
  opaque buffers; explicit column lists; `OVERRIDING SYSTEM VALUE`; post-load
  `setval` sequence reset. *Exit:* end-to-end single-table copy cross-host **and**
  same-server in integration.
- **Phase 1C — `rows`+`columns` validation + per-table state.** both sides at the
  pinned snapshot; mismatch → table FAILED. *Exit:* mismatch is detected and
  reported; benchmark target (single medium table) recorded.
- **Phase 1D — `pgcopydb_delegate` for same-name jobs.** picker routes
  src-schema==tgt-schema / whole-schema / large clones to a shell-out wrapper
  over `pgcopydb clone|copy` (consistent, parallel, resumable, CTID/integer
  split — all already provided). Our verification layer (§3.10) runs on top
  regardless, since pgcopydb's own `compare` is checksum-only and "not a full
  comparison." *Exit:* a same-name whole-schema clone completes via pgcopydb and
  passes our independent verify; remap jobs still take the bespoke path.

### Wave 2 — parallelise the remap path, with safe cutover
Scope: speed for the **remap/selective** copies pgcopydb cannot serve (same-name
work is already parallel via the Wave 1 delegate). Don't build a general-purpose
splitter that competes with pgcopydb.
- **Phase 2A — dependency ordering.** `pg_constraint` topo sort; FK strategy
  (drop+recreate, or `NOT VALID`+`VALIDATE`, or DEFERRABLE for cycles).
- **Phase 2B — intra-table parallelism (remap path only).** `ctid_chunked` +
  `key_range_chunked` for large *remap* tables; client connection pool sized to
  budget (≈min(cores,8)); all chunk readers share the snapshot. (Same-name
  tables get their splitting from the Wave 1 pgcopydb delegate.) *Exit:*
  ≥100M-row remap table ≥3× single-stream with `--max-parallel 4`.
- **Phase 2C — load-then-index + atomic cutover.** deferred **parallel**
  `CREATE INDEX` (`maintenance_work_mem`, `max_parallel_maintenance_workers`);
  `synchronous_commit=off`; **stage→RENAME swap** with `lock_timeout`+retry,
  old table dropped separately. *Exit:* swap window milliseconds under a
  concurrent reader; FKs re-validated.

### Wave 3 — phased agent CLI, verification, resume
- **Phase 3A — `plan`** → emits the canonical `plan.schema.json` (strategy per
  table, budget, projected peak connections, warnings) without touching data.
- **Phase 3B — durable job state + `run`/`status`/`wait`/`cancel`/`cleanup`.**
  `job-state.schema.json` under `~/.pgcopy/jobs/`; `--background`; orphan-staging
  sweep. *Exit:* `plan → run --background → status --watch → verify` end-to-end;
  job survives killing the CLI.
- **Phase 3C — strong verification + resume.** `hash`/`digest`/`sample` modes
  (canonical formula, per-chunk localisation), queryable reject store; idempotent
  `--resume` (re-snapshots, `--not-consistent`). (`pgcopydb_delegate` itself
  landed in Wave 1 Phase 1D; here it gains `--resume` pass-through and its
  results flow through the same verification/job-state surface.)

### Wave 4 — fleet ergonomics & scale
- **Phase 4A — `copy` one-shot + `logs --follow`.**
- **Phase 4B — fleet controls.** per-DB connection budgets, global rate limit,
  blast-radius caps, operator-pretune detection, same-server `INSERT … SELECT`
  fast path, blue/green target swap.

> Performance, validation, security and the adversarial review gate **every**
> stage — see `docs/performance.md`, `docs/validation.md`, `docs/security.md`,
> `docs/architecture-review.md`.

---

## 7. Build vs. buy (pgcopydb)

`[RN §1, §6]` `pgcopydb` already implements, install-free: source-side
`pg_export_snapshot()` consistency, `--table-jobs`/`--index-jobs`, automatic
CTID/integer same-table splitting, zero-disk `COPY` streaming, and `--resume`.
Reimplementing all of that lands slower in a worse place. **But** it is
whole-database-clone-shaped; our job is "copy *these* tables between *these*
schemas/hosts across a fleet of 100" — orchestration it doesn't own.

Decision: a **thin fleet/agent orchestrator** that (a) delegates to pgcopydb for
bulk/whole-schema **same-name** clones (`pgcopydb_delegate`) **from Wave 1** — so
no shipped release is slower than the tool that already does consistent, parallel,
resumable, split COPY — and (b) uses direct libpq `COPY` for the case pgcopydb
**structurally cannot** serve: selective table(s) **remapped** into a different
or already-populated target schema/name (pgcopydb has no rename/remap and is
whole-database-clone-shaped). We add the **independent verification layer
pgcopydb lacks** (§3.10) as the differentiator — it runs on top of *both* paths,
because pgcopydb's own `compare` is checksum-only and documented as "not a full
comparison." We ship `pgcopydb` + matching `pg_dump`/`pg_restore` client
binaries on our side; the no-install rule is about the database *hosts*.

Evidence (reproduced, pgcopydb 0.15 / PG 16.13): `pgcopydb clone --help` exposes
`--table-jobs --index-jobs --split-tables-larger-than --filters --resume
--snapshot` but **no remap/target-schema flag**; a >threshold table was
auto-split into 4 COPY processes partitioning on its integer key; a same-data
copy ran ~1.7× a single-stream client passthrough on a local socket (the gap
widens over a real network). See `docs/research-notes.md §RN-Δ`.

---

## 8. Testing strategy

- **Unit:** mock `psycopg`; assert on **constructed SQL strings** (snapshot
  import, `COPY` statements, CTID predicates, FK DDL, sequence reset, scoped
  SQLSTATE handling). Fast (<1s). `tests/unit/`.
- **Integration:** real PG via testcontainers (`postgres:16`) or
  `$PGCOPY_TEST_DSN`; gated behind `-m integration`. Cover what mocks can't:
  snapshot consistency across concurrent writers, CTID boundary
  exhaustiveness/disjointness, FK `VALIDATE` lock level, sequence-after-load
  correctness, stage-swap atomicity, resume after kill. **Write this early** —
  it's the parent project's self-identified biggest gap.

---

## 9. Open questions

To resolve before the corresponding cut.

1. **Cut 1:** binary vs text `COPY` default policy when source/target majors
   differ — auto-detect majors and downgrade to text, or require `--format`?
   (Leaning: auto-detect, record in plan.)
2. **Cut 2:** intra-table chunk sizing — split threshold and chunk count by
   table **bytes** (`relpages × block_size`, target ~256 MB/chunk) vs row count.
   (Leaning: bytes, mirroring pgcopydb.)
3. **Cut 3:** durable state — client-side file (default) vs opt-in target-side
   table for client-host-loss survival. Define the `--state-table` contract.
4. **Cut 3:** how aggressively to delegate to pgcopydb vs native `COPY` — size
   threshold, or whole-schema-only?

Resolved (carried from the parent): job_id format `j_<YYYYMMDD>_<HHMMSS>_<rand4>`
(§3.6); cross-host consistency cannot be one shared coordinate (§3.2);
permanent no-new-secret-store via libpq-native config (§3.3).

---

## 10. Non-goals (explicit)

- Heterogeneous-source copy (Oracle/MySQL → PG). Oracle→PG mapping notes are
  retained in `docs/research-notes.md §5` for possible future work only.
- Anything requiring server-side install, superuser, or restart as a hard
  dependency.
- Logical replication / CDC / continuous sync. One-shot full copy per job.
- Schema diffing / DDL synchronisation (use a migration tool).
- Bidirectional or incremental replication.
- A daemon, a web UI, or a long-running service. CLI only.
- Encrypted-at-rest credential management beyond libpq `.pgpass`.

---

## 11. Oracle → PostgreSQL primitive map (quick reference)

| Oracle (parent project) | PostgreSQL (this project) |
|---|---|
| `current_scn` + `AS OF SCN` | `pg_export_snapshot()` + `SET TRANSACTION SNAPSHOT` (source-local) |
| `INSERT … SELECT … @dblink` (server-side) | `COPY … TO STDOUT → FROM STDIN` (client, opaque buffers) |
| `DBMS_PARALLEL_EXECUTE` | client-driven N connections; CTID / key-range chunks |
| `ORA_HASH(ROWID, n-1)` | `ctid >= '(lo,0)'::tid AND ctid < '(hi,0)'::tid`, or `id BETWEEN …` |
| `DBMS_METADATA.GET_DDL` | `pg_dump --schema-only -t` + `pg_get_indexdef`/`pg_get_constraintdef` |
| `ALL_CONSTRAINTS` FK discovery | `pg_constraint` (`contype='f'`, `conrelid`/`confrelid`) |
| FK `ENABLE VALIDATE` | drop & recreate, or `ADD … NOT VALID` + `VALIDATE CONSTRAINT` |
| `ALTER SEQUENCE … RESTART` | `setval(pg_get_serial_sequence(...), COALESCE(MAX,1), MAX IS NOT NULL)` |
| identity copy | `OVERRIDING SYSTEM VALUE`; exclude `GENERATED … STORED` |
| idempotent ORA- codes | scoped SQLSTATEs (`42P07`, `23505`, …) + `IF NOT EXISTS`/`ON CONFLICT` |
| target-side `oracdb$jobs` state | client-side durable job file (default) |

Sources for every claim above: `docs/research-notes.md`.
