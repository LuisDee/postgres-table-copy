# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Project conventions for any Claude / agent session in this repo. Read
`docs/design.md` first — that's the architectural contract. This file is the
working rules. `docs/research-notes.md` is the sourced research the design
rests on; cite it when a decision needs justifying.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate    # one-time
pip install -e ".[dev]"                          # editable install + dev deps

pytest -q                          # unit tests (mock psycopg), fast; default
                                   # deselects integration via addopts
pytest tests/unit/test_registry.py -q            # one file
pytest -k "register and duplicate" -q            # one test by name
pytest -m integration -q           # real Postgres (testcontainers postgres:16
                                   # or $PGCOPY_TEST_DSN); skips cleanly if absent
ruff check src/ tests/             # lint — zero warnings expected
ruff check --fix src/ tests/       # autofix

pgcopy --help                      # the CLI entry point (console script)
```

No build step beyond the wheel (`python -m build`); this is a pure-Python
package with a `pgcopy` console script (`pyproject.toml [project.scripts]`).

## Delivery methodology (enforced)

Every change moves through this pipeline, in order. Don't skip stages.

1. **Spec** — write/extend the contract first (`docs/design.md` + the companion
   specs below). No behaviour is "designed in code."
2. **Plan** — break the spec into the smallest reviewable cut; state the cut's
   exit criterion (`docs/design.md §6`).
3. **TDD** — failing test → code → green. Mandatory (see Testing & TDD).
4. **Adversarial review** — argue against the change. Run it past the standing
   punch-list in `docs/architecture-review.md`; add new questions you couldn't
   answer. Push for a faster/cleaner/safer method before defending v1.

Two gates apply at **every** stage, not just at the end:
- **Security** (`docs/security.md`): no superuser/extension assumptions; secrets
  only in `.pgpass`; `sslmode=verify-full` + `channel_binding=require`; least
  privilege; never log row data; RLS silently filters `COPY TO` — detect & warn.
- **Speed** (`docs/performance.md`): know the bottleneck before optimising;
  binary COPY + CTID-split parallelism + load-then-index; emit per-phase
  throughput; meet the benchmark targets.

**Web-research for context is mandatory** when touching PG behaviour, a tool, or
a perf/security claim — cite the source in `docs/research-notes.md` (don't answer
from memory). Prefer version-pinned postgresql.org docs.

This is a **multi-wave** build (`docs/design.md §6`): Wave 0 (registry/contract,
done) → Wave 1 (single-table copy) → Wave 2 (multi-table + parallelism +
cutover) → Wave 3 (phased agent CLI + verification + resume) → Wave 4 (fleet
ergonomics). Don't pull work forward across waves.

### Companion specs (read alongside the contract)

- `docs/design.md` — the architectural contract + waved roadmap (read first).
- `docs/performance.md` — ranked lightning levers + benchmark targets.
- `docs/validation.md` — validation modes, failure paths, atomic stage-swap.
- `docs/security.md` — security defaults, least-privilege grants, RLS landmine.
- `docs/architecture-review.md` — the standing adversarial punch-list.
- `docs/schemas/*.schema.json` — canonical data contracts (envelope, plan,
  job-state). Keep code and these in lock-step.
- `docs/research-notes.md` — the sourced evidence base; cite `§RN…` in decisions.

## What this project is

A lightweight, **client-side** PostgreSQL table-copy CLI for a fleet of ~100
self-managed Postgres databases. Copies tables between schemas, databases, and
hosts. Thin Python orchestrator; the data plane is libpq `COPY` streaming.
Designed to be driven by agents.

It is a concept-port of `oracle-schema-refresh` — same phased-engine,
agent-CLI, one-consistent-read-per-job, cut-based-delivery DNA — rebuilt for
PostgreSQL's no-install reality.

Status: **pre-implementation. Cut 0 not started.** Roadmap in `docs/design.md`
§6.

## The two non-negotiable truths (read before writing any data-plane code)

1. **The data plane streams through the client as opaque `COPY` buffers.**
   With no `postgres_fdw` / `dblink` (we install nothing server-side), the only
   cross-host pipe is `COPY (SELECT …) TO STDOUT` → `COPY … FROM STDIN`. Bytes
   transit our process. Honour the spirit of "no row data through the client"
   by piping **raw `CopyData`** — never parse, deserialise, transform, or
   buffer whole rows as Python objects. If you reach for per-row Python
   handling of table data, stop and pick a different strategy.
2. **Consistency is source-local.** A PostgreSQL exported snapshot cannot span
   two hosts (per-cluster XID space). There is no global SCN. "One snapshot per
   job" means one `pg_export_snapshot()` on the **source**, shared by every
   source reader via `SET TRANSACTION SNAPSHOT`. Target consistency is achieved
   by stage-then-swap, orchestrated client-side — never assume source+target
   share a point in time.

## Working rules

1. **Land work as cuts.** Each cut is one commit, lands green tests, updates
   the roadmap in `docs/design.md` §6. Don't collapse cuts.
2. **Test-first.** New behaviour starts with a failing test, then code, then
   green. No untested behaviour change.
3. **Assume no superuser and no server-side install, ever.** Before using any
   mechanism, confirm it works with ordinary table-owner privileges over
   libpq. If a knob needs superuser / a server restart / a config-file edit /
   an extension, it is **out of scope** unless surfaced as "operator may
   pre-provision" (e.g. `session_replication_role`, `wal_level=minimal`,
   `max_wal_size`). Detect-and-fall-back; never hard-require.
4. **Never silently swallow SQLSTATEs.** The PG analogue of the parent's
   ignore-codes rule: tolerated SQLSTATEs are passed **per operation**, scoped
   to the one statement that legitimately produces them (e.g. `42P07`
   duplicate_table only around a CREATE, `23505` only around an idempotent
   re-insert). Never a global union. Prefer `IF NOT EXISTS` / `ON CONFLICT`
   idioms where they exist (note: `ADD CONSTRAINT` has **no** `IF NOT EXISTS`).
5. **One snapshot per job.** All source reads (table list, row counts, `COPY`
   selects, hash verification) anchor to one exported snapshot captured at job
   start. Across-table consistency depends on it.
6. **Always schema-qualify, and fix the case-folding policy once.** Oracle
   folds unquoted identifiers UPPER; PostgreSQL folds them lower. Normalise to
   unquoted-lowercase by default; quote only when an identifier isn't a legal
   lowercase identifier. Never rely on `search_path`.
7. **Confirm before destructive or shared-state actions** (git push,
   force-push, `DROP`, `TRUNCATE`, target-schema swaps outside the engine's own
   staging DDL/DML). Local file edits and test runs are fine without asking.
8. **Match scope to the request.** Don't add abstractions, error handlers, or
   fallback paths for cases that can't happen. Internal code can trust internal
   code.
9. **No new secret store.** Credentials live in libpq-native `~/.pgpass`
   (`chmod 0600`); topology in `~/.pg_service.conf` (referenced by `PGSERVICE`).
   Don't write passwords into the endpoint YAML, into SQL text, or anywhere new.

## Build vs. buy

`pgcopydb` is a purely client-side tool that already implements snapshot
consistency, table/index-job parallelism, CTID/integer table splitting, and
zero-disk `COPY` streaming. Default posture: **orchestrate over pgcopydb where
it fits; drop to direct libpq `COPY` for selective table/schema copies.** Don't
reimplement what pgcopydb already does well. Do build the thing it lacks:
independent row-count + hash/sample **verification** (`docs/design.md` §3.10).
`pg_dump`/`pg_restore`/pgcopydb are **client binaries** — shipping them on our
side is allowed; the "no install" rule is about the database *hosts*.

## Testing & TDD (mandatory)

TDD is mandatory here: write a failing test, then the code, then green. No
behaviour change without a test first. Commands are under "Commands" above.

- Prefer tests that assert on the **SQL string constructed** rather than on the
  side effects of running it — real-DB behaviour is the integration suite's job.
- Mock `psycopg` at the connection/cursor boundary. Shared fakes and fixtures
  live in `tests/conftest.py` (`FakeConnection`/`FakeCursor`, `fake_conn`,
  `tmp_registry`, …) — use them rather than rebuilding ad-hoc mocks per test.
  This mirrors the parent project's `tests/_helpers.py` discipline.
- Unit tests live in `tests/unit/`, integration in `tests/integration/`
  (gated behind `-m integration`). Write the integration suite early — it's the
  parent project's self-identified biggest gap.

## Commits

- One cut = one commit. Subject line imperative, ≤72 chars, e.g.
  `Cut 0: endpoint registry over pg_service/.pgpass (no data plane yet)`.
- Body explains *why*, lists module-level changes, notes back-compat /
  privilege decisions, references resolved open questions (`docs/design.md` §9).
- Don't include model identifiers in commit messages.

## What not to do

- Don't import or vendor other projects' code. Reimplement minimally if a
  pattern is worth borrowing.
- Don't add design-contract content here; that lives in `docs/design.md`.
- Don't add features beyond the current cut's scope, even small ones.
- Don't depend on `session_replication_role`, logical replication, fdw/dblink,
  or any server-config change as a hard requirement (rule 3).
