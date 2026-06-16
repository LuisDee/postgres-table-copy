# Validation, failure paths & atomic cutover

How we prove a transfer is correct, how we behave when things break, and how we
cut over without a visible-inconsistency window. Sources: `docs/research-notes.md
§RN3`. Adversarial premise: assume rows can silently vanish or corrupt, and that
any single check can be fooled.

## 1. The precondition: one pinned read point

A count or checksum is meaningless unless **both sides read the same logical
point in time**. Default READ COMMITTED takes a fresh snapshot per statement, so
two free-running connections legitimately disagree under concurrent writes —
that is not corruption.

- Leader opens `REPEATABLE READ`, `SELECT pg_export_snapshot()`, stays open for
  the whole job; every reader/validator runs `SET TRANSACTION SNAPSHOT '<id>'`
  as its first statement. (Our "one snapshot per job"; how `pg_dump -j` stays
  consistent.)
- **Every source read/validate txn also sets `SET row_security = off`** (and pins
  `client_encoding`, `DateStyle = ISO`). This is a correctness primitive, not
  just security: under RLS a filtering role undercounts *both* the COPY and the
  `COUNT(*)` identically, so a count-vs-count check passes a partial copy.
  `row_security = off` makes a would-be-filtered read **error** instead of lying
  (reproduced; see `security.md` → RLS). The copy/validate role must be owner or
  `BYPASSRLS` for RLS tables.
- **Record the read point** for after-the-fact disputes: exported-snapshot id +
  `pg_current_snapshot()` (`xmin:xmax:xip`) + `pg_current_wal_lsn()`. PostgreSQL
  has **no flashback** — "validate later" means anchoring to a live snapshot
  *during* the job, or replaying to a replica/PITR paused at the recorded LSN.
- The target has no AS-OF; validate it **at rest** (post-swap) or in its own
  REPEATABLE READ txn. Note `TRUNCATE` and rewriting `ALTER TABLE` are **not
  MVCC-safe** — after commit they look empty to an older snapshot.

## 2. Validation modes

Prefer **order-independent** aggregates so each chunk is checksummed by a
separate worker and folded without a global sort. The trade is collision
resistance. All run **server-side** — no row data through the client.

| Mode | Catches | Misses | Cost | Parallel? |
|---|---|---|---|---|
| `rows` — `COUNT(*)` src vs tgt | missing/extra rows | in-place value corruption | trivial | yes (Σ counts) |
| `columns` — per-column `min/max/sum/count/count(col)` | column-localized drift, null-count drift | value swaps preserving aggregates | 1 pass | yes |
| `hash` (default-strong) — `md5(format('%s-%s', sum(hashtext(row(t.*)::text)::bigint), count(1)))` per side | value changes, row add/remove | algebraic/compensating collisions (rare) | 1 pass | **yes** (Σ + count) |
| `digest` — `md5(string_agg(t::text,'' ORDER BY pk))` | any content/order/set difference | nothing same-server; not stable across PG versions/locale | materialises whole table | no (global sort) |
| `sample` — `TABLESAMPLE … REPEATABLE(seed)` | corruption in the sampled fraction | rare/localized corruption | sub-linear | yes |

**Decisions:**
- Default is `rows` + `columns` on every table (cheap, chunkable).
- `hash` is the pgcopydb-proven primitive: `sum(hashtext(row::text)::bigint)`
  **combined with `count(1)`** (count folded into the digest to cut collisions).
  Run on demand or automatically on a `rows`/`columns` mismatch. A checksum
  match is **probabilistic, not proof** — we say so in output.
- Never use 32-bit `hashtext` *alone* as the row identity (collisions near ~77k
  rows). Never depend on extension hashes (`pg_xxhash` etc. need
  `CREATE EXTENSION` — privilege we don't have).
- **Per-chunk checksums**, divergence localised to a row/CTID range
  (pt-table-checksum pattern), with adaptive chunk sizing — so a failure reads
  "rows 40k–50k of table X diverge," not "table X failed."
- **No-PK tables:** like AWS DMS, `digest`/PK-aligned validation is unavailable;
  downgrade to `hash`+`columns` and **say so** — never imply a guarantee we
  can't make.

## 3. Failure paths → mitigations

| Failure | What actually happens | Mitigation |
|---|---|---|
| Mid-stream COPY abort / conn drop | `COPY FROM` is all-or-nothing → **zero rows committed**; dead tuples remain | re-run; `VACUUM` the staging table to reclaim space |
| Poison row / cast failure | one bad row aborts the whole COPY | PG17 `COPY … (ON_ERROR ignore, LOG_VERBOSITY verbose)` for **cast errors only** (not constraint violations); else pgloader-style batch+bisect to a reject store. (`REJECT_LIMIT` is PG18.) |
| Duplicate-key on reload | unique violation aborts COPY; `ON_ERROR` won't suppress | always load a fresh/`TRUNCATE`d staging table, never the live target |
| FK violation / load order | per-row FK checks fail children-before-parents | drop+recreate FKs, or `DEFERRABLE INITIALLY DEFERRED`; **never** `DISABLE TRIGGER` (re-enable does not re-validate) |
| Source snapshot lost mid-job | exporting txn died → `SET TRANSACTION SNAPSHOT` fails → consistent copy can't continue | restart with a fresh snapshot (this is why pgcopydb `--resume` forces `--not-consistent`) |
| Orphaned staging after crash | no built-in discovery | deterministic staging names (`pgcopy_stg__<job>__<table>`); sweep `pg_tables` for the prefix at job start and `DROP` |
| Re-run / resume | must equal run-once | deterministic staging + `TRUNCATE`-in-load-txn as the idempotent primitive; resume re-snapshots and re-runs incomplete tables only |

A **queryable reject store** (pgloader `.dat`/`.log` analog) plus a DMS-style
failures record (`RECORD_DIFF` / `MISSING_SOURCE` / `MISSING_TARGET`) makes
failures auditable, not just logged.

## 4. Atomic stage-swap (cutover)

PostgreSQL DDL is transactional: `CREATE`/`ALTER … RENAME`/`DROP` inside
`BEGIN/COMMIT` are atomic and roll back. The swap is **catalog-only**
(milliseconds) — concurrent sessions see entirely the old or entirely the new
table, never an intermediate.

```sql
-- Slow work OUTSIDE the swap txn: load + index + analyze the staging table
-- (deterministic name for idempotent reclaim).

SET lock_timeout = '2s';          -- fail fast; don't build a lock queue
BEGIN;
  ALTER TABLE live    RENAME TO live_old;
  ALTER TABLE staging RENAME TO live;
COMMIT;                            -- atomic cutover

DROP TABLE live_old;               -- separate txn: shorten the lock hold
```

**Lock-queue hazard:** `RENAME`/`DROP`/`TRUNCATE`/most `ALTER` take ACCESS
EXCLUSIVE, which blocks even `SELECT`; PG's FIFO fairness means a *pending*
ACCESS EXCLUSIVE stuck behind one long reader makes **all** subsequent SELECTs
queue behind it. Mitigate with low `lock_timeout` + retry, or
`LOCK TABLE … NOWAIT` (`55P03`) at the top of the swap. **The retry only works if
`55P03` is categorised `TRANSIENT`** — `lock_timeout`/`NOWAIT` both raise class
`55`, which must map to a retryable category, not `INTERNAL` (see `design.md
§4.2` / `errors.py`).

**Why RENAME beats TRUNCATE+reload:** TRUNCATE+reload in one txn holds ACCESS
EXCLUSIVE for the *entire load* — readers block the whole time. The RENAME swap
holds it for ~ms. RENAME wins whenever load time is non-trivial. Lowest-lock
alternative: queries hit a stable view; `CREATE OR REPLACE VIEW live_v AS SELECT
… FROM data_v2` is catalog-only.

## 5. What a paranoid engineer demands before trusting this on 100 DBs

1. One pinned read point per job, recorded (snapshot id + LSN).
2. Two-tier verification, both sides server-side: cheap (`rows`+`columns`)
   always; strong (`hash`) on demand or on mismatch — with residual collision
   risk surfaced.
3. Per-chunk checksums with divergence localised; adaptive chunk sizing.
4. Atomic stage→swap with `lock_timeout` + retry; old table dropped separately;
   a max swap-window SLA per table.
5. Idempotent everything: deterministic staging, orphan sweep, re-run==run-once.
6. A queryable reject/failures store.
7. Explicit no-PK handling — refuse/downgrade, never fake success.
8. Constraint strategy decided up front (drop+recreate or DEFERRABLE).
