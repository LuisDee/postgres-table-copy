# postgres-table-copy

A lightweight, **client-side** CLI to copy PostgreSQL tables — between schemas,
between databases, and between hosts — across a fleet of self-managed Postgres
instances. Designed to be driven by automation and AI agents.

> Status: **pre-implementation.** This repository currently holds the design
> contract (`docs/design.md`), the research it's built on
> (`docs/research-notes.md`), and the working rules (`CLAUDE.md`). No `Cut 0`
> code has landed yet. The roadmap lives in `docs/design.md` §6.

## Why this exists

We run **~100 self-managed PostgreSQL databases** and need one robust, generic
tool to copy tables between schemas and between hosts/databases — without the
ceremony of bespoke scripts per job.

The hard requirement that shapes every decision: **we install nothing on the
database servers.** No extensions (`postgres_fdw`, `dblink`, `pglogical`), no
host shell access, no `postgresql.conf` / `pg_hba.conf` edits, no server
restarts, and **no assumption of superuser**. Everything runs on our side,
talking ordinary libpq/SQL to the servers.

This project is a deliberate **port of the concepts** in
[`oracle-schema-refresh`](https://github.com/luisdee/oracle-schema-refresh) —
its phased engine, its agent-facing CLI, its "one consistent read per job"
discipline, its cut-based delivery — re-expressed idiomatically for
PostgreSQL. It is **not** a translation: PostgreSQL's no-install reality
inverts the parent project's foundational "data never flows through the client"
rule (see below), so the data plane is rebuilt from first principles.

## The one architectural truth to internalise

In Oracle, data movement is server-side (`INSERT … SELECT … @dblink`) and the
client is a pure control plane. **In client-only PostgreSQL that is structurally
impossible** — with no foreign-data-wrapper and no dblink, the only cross-host
pipe is:

```
COPY (SELECT …) TO STDOUT   (on the source)
        │   raw CopyData buffers over libpq
        ▼
COPY … FROM STDIN           (on the target)
```

…which necessarily passes through our client process. We preserve the *spirit*
of the parent project's "no row data through the client" rule by streaming
**opaque COPY buffers** — never parsing, deserialising, or materialising rows —
but the bytes do transit the client. This is a first-class design decision, not
an accident. See `docs/design.md` §3.1.

The second truth: PostgreSQL's consistency anchor (an exported MVCC snapshot)
is **source-cluster-local — it cannot span two hosts.** There is no
PostgreSQL "global SCN." Cross-host consistency is therefore "a consistent
snapshot *of the source*," orchestrated client-side with stage-then-swap on the
target. See `docs/design.md` §3.2.

## What it will do

- `pgcopy endpoints add|list|remove|test` — a fleet endpoint registry backed by
  libpq-native config (`~/.pg_service.conf` for topology, `~/.pgpass` for
  secrets — **no new secret store**).
- `pgcopy plan` — produce a reviewable, JSON plan (tables, strategy per table,
  projected connection budget, hazards) without touching data.
- `pgcopy run` — execute a plan: snapshot the source, stream `COPY`, rebuild
  indexes/constraints, reset sequences, verify.
- `pgcopy status | wait | verify | cancel | cleanup` — the phased, agent-driven
  lifecycle, with a stable JSON envelope and exit codes.
- `pgcopy copy` — the one-shot convenience path (`plan → run → wait → verify`).

## Two consistency/parallelism primitives, both client-side

- **One exported snapshot per job** on the source
  (`pg_export_snapshot()` + `SET TRANSACTION SNAPSHOT` in `REPEATABLE READ`),
  shared by every source reader connection. The PostgreSQL analogue of the
  parent project's "one SCN per job."
- **Client-driven parallelism** — N libpq connections; large tables split by
  **CTID block range** (or a unique integer key) exactly as `pgcopydb` does.
  No server-side scheduler is needed or available.

## Build vs. buy

[`pgcopydb`](https://github.com/dimitri/pgcopydb) already implements most of
this install-free (snapshot consistency, `--table-jobs`/`--index-jobs`,
automatic CTID/integer table splitting, zero-disk `COPY` streaming, resume).
The plan is to build a **thin fleet/agent orchestration layer** that uses
pgcopydb where it fits and direct libpq `COPY` for selective schema→schema and
table-level copies — adding the **independent row-count + hash/sample
verification** the parent project specified but never shipped, as our
differentiator. See `docs/design.md` §7.

## Repository layout

```
README.md              — this file
CLAUDE.md              — working rules for any agent/Claude session here
docs/
  design.md            — the design contract + cuts roadmap (read this first)
  research-notes.md    — the sourced research the design is built on
src/postgres_table_copy/
  cli.py               — `pgcopy` entry point (skeleton)
tests/                 — unit tests (mock psycopg) + integration (testcontainers)
pyproject.toml
```

## License

MIT — see `LICENSE`.
