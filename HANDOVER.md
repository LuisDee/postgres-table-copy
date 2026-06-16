# HANDOVER — postgres-table-copy

A self-contained handover so anyone (human or agent) can pick this up. The repo
could not be pushed from the session that built it (the session's git proxy was
scoped to another repo), so the history is delivered as a **git bundle** and a
**format-patch series**. This doc explains how to apply them, what exists, and
what's outstanding.

---

## 1. Apply the delivered history

You'll have received two artifacts (plus this doc):

- `postgres-table-copy.bundle` — the whole repo + history (recommended).
- `pgcopy-patches.tar.gz` — `git format-patch` series for `git am`.

### Option A — git bundle (simplest, full history)

```bash
# Into a fresh directory:
git clone postgres-table-copy.bundle postgres-table-copy
cd postgres-table-copy
git remote set-url origin https://github.com/LuisDee/postgres-table-copy.git
git push -u origin main

# …or into an existing empty clone of the GitHub repo:
cd postgres-table-copy
git pull /path/to/postgres-table-copy.bundle main
git push -u origin main
```

### Option B — format-patch series (apply onto an empty repo)

```bash
mkdir postgres-table-copy && cd postgres-table-copy && git init -b main
tar xzf pgcopy-patches.tar.gz          # extracts 0001-*.patch … 000N-*.patch
git am 0*.patch
git remote add origin https://github.com/LuisDee/postgres-table-copy.git
git push -u origin main
```

Verify after applying:

```bash
git log --oneline        # should show the commits listed in §3
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                # expect: 60 passed, 1 skipped
ruff check src/ tests/   # expect: All checks passed!
```

---

## 2. What this project is (one paragraph)

A **client-side, agent-driven CLI** to copy PostgreSQL tables between schemas,
databases, and hosts across a fleet of ~100 self-managed Postgres instances,
**installing nothing on the database servers** (no extensions, no host shell, no
superuser, no server-config changes). It is a concept-port of
`oracle-schema-refresh`. Read `docs/design.md` first; `CLAUDE.md` is the working
rules; the companion specs (`docs/performance.md`, `validation.md`, `security.md`,
`architecture-review.md`) and canonical schemas (`docs/schemas/*.json`) are the
rest of the contract.

### Two truths that shape everything
1. **The data plane streams through the client as opaque `COPY` buffers.** With
   no fdw/dblink, the only cross-host pipe is `COPY … TO STDOUT` → `COPY … FROM
   STDIN`. Bytes transit our process; we never parse/materialise rows.
2. **Consistency is source-cluster-local.** An exported snapshot can't span two
   hosts — no global SCN. Cross-host = consistent snapshot *of the source* +
   stage-then-swap on the target.

---

## 3. Commit history (what's in the bundle)

```
d7cf234 docs: phased spec, perf/validation/security specs, canonical schemas
8359d36 Cut 0: endpoint registry + CLI contract (TDD, no data plane)
5cfda48 Initial scaffold: motivation, README, design contract & research notes
<this commit> docs: add HANDOVER.md
```

---

## 4. Delivery methodology (enforced — see CLAUDE.md)

`spec → plan → TDD → adversarial review`, with **security** and **speed** as
standing gates at every stage, and **web-research-for-context mandatory** (cite
`docs/research-notes.md`). Multi-wave build; don't pull work forward.

---

## 5. Status: DONE vs OUTSTANDING

### ✅ Done — Wave 0 (contract & registry), committed & green
- `errors.py` — exit-code table, `ErrorCategory`, exception hierarchy,
  SQLSTATE→category, `tolerate(*sqlstates)` (per-operation scoped handling).
- `endpoints/` — `Endpoint` (topology only; secrets in `.pgpass`) with
  `conninfo/connect/probe`; `EndpointRegistry` (0600 YAML, add/get/list/remove,
  typed errors).
- `cli.py` — `pgcopy` Click app; `endpoints add/list/remove/test`; stable JSON
  envelope + exit codes; phased commands as honest "not implemented" stubs.
- Tests — `tests/conftest.py` fakes/fixtures (`FakeConnection/FakeCursor`,
  `make_conn`, `tmp_registry`, `db_error`); **60 unit tests, 1 skipped
  integration; ruff clean.**
- Full design: `docs/design.md` (waved roadmap), `performance.md`,
  `validation.md`, `security.md`, `architecture-review.md`,
  `schemas/{envelope,plan,job-state}.schema.json`, `research-notes.md`.

### ⏳ Outstanding — Waves 1–4 (no data-plane code exists yet)

**Wave 1 — single-table copy, correct & consistent** (NEXT)
- *1A introspection:* explicit column lists excluding `GENERATED … STORED`;
  identity/sequence discovery (`pg_get_serial_sequence`/`pg_depend`); table
  profile (size via `relpages`, LOB/RLS/partition flags); pre-flight checks
  (encoding/collation drift, RLS) → warnings.
- *1B snapshot + `direct_copy`:* one `pg_export_snapshot()`; binary `COPY TO
  STDOUT → FROM STDIN` (text fallback on major mismatch); `OVERRIDING SYSTEM
  VALUE`; post-load `setval` sequence reset.
- *1C validation + per-table state:* `rows`+`columns` both sides at the pinned
  snapshot; mismatch → FAILED.

**Wave 2 — many tables, fast, safe cutover**
- *2A* FK topo sort (`pg_constraint`) + strategy (drop+recreate / `NOT VALID`
  +`VALIDATE` / DEFERRABLE).
- *2B* intra-table parallelism (`ctid_chunked`, `key_range_chunked`), connection
  pool ≈ min(cores, 8), all readers share the snapshot.
- *2C* load-then-**parallel** `CREATE INDEX`; `synchronous_commit=off`; atomic
  **RENAME** stage-swap with `lock_timeout`+retry.

**Wave 3 — phased agent CLI, verification, resume**
- *3A* `plan` → emits `plan.schema.json`.
- *3B* durable job state (`job-state.schema.json` under `~/.pgcopy/jobs/`) +
  `run`/`status`/`wait`/`cancel`/`cleanup`; `--background`; orphan-staging sweep.
- *3C* strong verify (`hash`/`digest`/`sample`, per-chunk localisation); reject
  store; idempotent `--resume` (re-snapshots, `--not-consistent`);
  `pgcopydb_delegate` for whole-schema clones.

**Wave 4 — fleet ergonomics**
- `copy` one-shot; `logs --follow`; per-DB connection budgets; rate limiting;
  blast-radius caps; same-server `INSERT … SELECT` fast path; blue/green swap.

---

## 6. The very next task (Wave 1, Phase 1A) — concrete start

TDD as always. Suggested module + first failing tests:

- New `src/postgres_table_copy/introspect/columns.py`:
  `copyable_columns(conn, schema, table) -> list[str]` — query
  `information_schema.columns` / `pg_attribute`; **exclude** generated-stored
  columns (`attgenerated = 's'`); preserve ordinal order; return quoted-safe
  identifiers.
- First tests in `tests/unit/test_introspect_columns.py` using the existing
  `make_conn` fixture: assert the SQL is parameterised and the generated column
  is dropped from the list. (Assert on the **constructed SQL string**, per the
  testing rule.)
- Then `profile.py` (size/flags) and `sequences.py`, each test-first.

Pattern to follow: every DB call takes an injectable connection (like
`Endpoint.connect(connector=…)`) so unit tests use `FakeConnection` and the
integration suite uses real psycopg. Keep `psycopg` a **lazy import**.

---

## 7. Landmines already mapped (don't re-learn the hard way)

- **RLS silently filters `COPY TO`** for non-owner roles → a copy that looks
  complete but isn't. Detect `pg_class.relrowsecurity`; warn at plan; hard-fail a
  row-count gap. `COPY FROM` is unsupported under RLS → use INSERT.
- **Collation/encoding drift corrupts indexes** → pre-flight check; REINDEX /
  `ALTER COLLATION … REFRESH VERSION`; pin `client_encoding`/`DateStyle`.
- **Ownership, not grants**, is required for ALTER/RENAME/CREATE INDEX/DROP → the
  tool must create & own its target + staging tables.
- **`session_replication_role`** needs superuser/granted `SET ON PARAMETER` →
  don't depend on it; use drop+recreate / `NOT VALID`+`VALIDATE`.
- **UNLOGGED → `SET LOGGED`** is a trap (double WAL + full rewrite).
- **`REJECT_LIMIT` is PG18** (PG17 only has `ON_ERROR`/`LOG_VERBOSITY`).
- **Snapshot lost mid-job** → resume must re-snapshot and run `--not-consistent`.
- **Atomic swap** = catalog-only RENAME (ms), but ACCESS EXCLUSIVE blocks SELECT
  and a pending one queues all readers → low `lock_timeout` + retry.

---

## 8. Build vs buy (decided)

For whole-DB cross-host clones with consistency+parallelism+resume+LO+sequence
handling, **delegate to `pgcopydb`** (`pgcopydb_delegate`, Wave 3). The custom
orchestrator earns its keep on: agent-driven per-table sub-jobs, same-server
schema refresh, fleet connection budgeting, selective table copies, and
**independent verification** (pgcopydb's own `compare` explicitly says it is not
a full check). Ship `pgcopydb` + matching `pg_dump`/`pg_restore` as client
binaries — the no-install rule is about the database *hosts*, not our box.

---

## 9. Push / auth situation

The repo `https://github.com/LuisDee/postgres-table-copy` exists, but the
building session's git proxy was scoped only to `oracle-schema-refresh`, so
`git push` returned `repository not authorized`. Resolution: either authorize
that repo in the session/environment's repository list, or apply this bundle
locally and push from your own machine (§1). Commits are unsigned
(`commit.gpgsign=false`) because the session's signing identity differed from the
configured author; re-sign on your machine if your repo requires signed commits.

---

## 10. Quick reference

```bash
pip install -e ".[dev]"                 # dev setup
pytest -q                               # unit (mock psycopg); 60 pass, 1 skip
pytest tests/unit/test_registry.py -q   # one file
pytest -k "duplicate" -q                # one test
pytest -m integration -q                # real PG via $PGCOPY_TEST_DSN; else skips
ruff check src/ tests/                  # lint
pgcopy endpoints --help                 # CLI
```
