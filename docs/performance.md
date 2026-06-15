# Performance: how we make it lightning

The mandate is speed. This doc is the ranked, evidence-based set of levers, the
bottleneck model that tells us *which* lever to pull, and the benchmark
**targets** we hold ourselves to. Sources: `docs/research-notes.md §RN2`.

## Bottleneck model (decide before optimising)

For a cross-host copy the cost order is almost always:
**target write/WAL + index build  >  source read  ≈  network.**

Detect which dominates, then pull the matching lever:

| Symptom | Bound on | Lever |
|---|---|---|
| One target backend at ~100% CPU, network idle | WAL/parse | binary COPY + parallel workers + drop indexes |
| Network link pinned (≈110 MB/s on 1 GbE), CPUs idle | network | PG18 libpq `compression=zstd`/`lz4` |
| Long tail after rows land, target CPU on `CREATE INDEX` | index build | parallel `CREATE INDEX` + `maintenance_work_mem` |

Parallelism only helps if the single-thread path isn't already saturating a
resource — if one COPY already maxes target disk, more workers do nothing.

## Ranked levers (by measured impact)

1. **Parallelism: multi-table + intra-table CTID split — the headline win (≈5–10×).**
   Reference numbers: 1 TB copy — `pg_dump|pg_restore` ~17 h vs CTID-partitioned
   parallel (PeerDB) ~1 h 49 m ≈ **550 MB/s**; the 5×/10× figures are all from
   splitting one big table by `ctid` range and copying partitions concurrently.
   Reproducible with libpq only: one exported snapshot, per-worker
   `COPY (SELECT … WHERE ctid >= '(lo,0)'::tid AND ctid < '(hi,0)'::tid) TO STDOUT (FORMAT binary)`.
   Optimal workers ≈ **min(target cores, 8)** — COPY scaling shows diminishing
   returns past ~8. Mind `max_connections` and PgBouncer.
2. **Load first, build indexes/constraints after — in parallel.** Parallel
   `CREATE INDEX` measured ~80% faster; raise `maintenance_work_mem` and
   `max_parallel_maintenance_workers` (both session-settable, no superuser).
   Build all of a table's indexes concurrently off one heap scan
   (`synchronize_seqscans`); create the unique index then
   `ALTER TABLE … ADD … USING INDEX` to dodge the ACCESS EXCLUSIVE serialize.
3. **Binary COPY, streamed STDOUT→STDIN, zero disk staging.** Native format
   skips server-side text parsing; psycopg3 COPY ≈1.2M rows/s. Stream raw
   `CopyData` buffers — never stage to a file (the pg_dump directory-format
   penalty), never materialise rows as Python objects.
4. **psycopg3 pipeline mode** for the surrounding small-statement storm (DDL,
   per-partition setup, constraint rebuilds): 2–5× by removing round-trips,
   biggest on high-RTT links.
5. **PG18 libpq protocol compression** (`compression=zstd`|`lz4` in the
   conninfo) — client-settable, no superuser. Use only when network-bound;
   prefer `lz4`/`zstd:1` (ratio-per-CPU). TLS compression is dead (CRIME) —
   never use it.
6. **Session durability knobs (no superuser):** `SET synchronous_commit = off`
   for the load session (safe — a crashed job is re-run). Defer/recreate FKs.

## Traps (do not use)

- **UNLOGGED → `SET LOGGED`:** the conversion **rewrites the whole table and
  WAL-logs all of it** — you pay back the WAL you saved, plus a full rewrite.
  Only worth it if the table can *stay* unlogged.
- **`COPY (FREEZE)` WAL-skip / `max_wal_size` / `wal_level=minimal`:** real wins
  but **server-side / superuser / restart** — out of our reach. Surface as
  "operator may pre-tune," never as a tool action.
- **PgBouncer transaction-pooling for the copy:** breaks session-scoped
  `SET TRANSACTION SNAPSHOT` and `SET`s. Use session pooling or direct
  connections for snapshot jobs.

## Benchmark targets (the bar we hold ourselves to)

Measured in the integration suite (`postgres:16`, then PG18 for compression),
reported by `pgcopy` in the run summary. These are *targets*, revised as we
gather real fleet numbers.

| Scenario | Target |
|---|---|
| Single medium table (10M rows, ~5 GB), same-region cross-host | within ~1.5× of raw `\copy` STDOUT→STDIN |
| Large table (≥100M rows) with CTID split, `--max-parallel 4` | ≥3× single-stream throughput |
| Whole-schema clone vs `pg_dump -Fd -j N | pg_restore -j N` | ≥ parity, ideally faster (zero staging) |
| Post-load index build, parallel | ≥1.5× single-thread |
| Validation (`rows`+per-column) overhead | <5% of copy wall-clock |

Every `run`/`copy` emits throughput (MB/s, rows/s) and a per-phase breakdown
(read / network / write / index / validate) so regressions are visible and the
bottleneck model is always populated with real data.
