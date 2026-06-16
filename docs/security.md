# Security

Security is a first-class gate, not an afterthought. The tool streams real row
data through the client process across ~100 self-managed DBs, so transport,
credentials, least privilege, in-flight exposure, and audit all matter.
Sources: `docs/research-notes.md §RN4`.

## Defaults (setting → value → why)

| Setting | Default | Why |
|---|---|---|
| `sslmode` | **`verify-full`** | libpq default `prefer` is "not recommended in secure deployments"; `require` encrypts but does **no** cert check (no MITM protection). `verify-full` validates the chain **and** matches hostname. |
| `sslrootcert` | **pinned internal CA** (or `system` on PG16+) | `verify-*` needs a trusted root; default `~/.postgresql/root.crt`. Ship/pin the fleet CA. |
| `channel_binding` | **`require`** | PG13+. Binds SCRAM auth to the server's TLS cert (`SCRAM-SHA-256-PLUS`), defeating a relaying MITM that presents a different cert. Complementary to `verify-full`, not redundant. |
| password source | **`.pgpass` (0600)** / service file | libpq **silently ignores** `.pgpass` if not `0600`. Keeps secrets out of argv/logs. |
| `PGPASSWORD` | **avoid** | readable via `ps`/`/proc` on some OSes; inherited by children. Last resort only. |
| password in argv / SQL / logs | **never** | `/proc/<pid>/cmdline` is world-readable; SQL and stderr land in logs. |
| `client_encoding`, `DateStyle=ISO`, `IntervalStyle` | **pin on both ends** | COPY text output is GUC-sensitive; mismatch → silent value drift. |
| `RLIMIT_CORE` | **0** for the tool process | a core dump contains row data verbatim. |

`verify-full` and `channel_binding` are layered: the former validates cert+host
for any auth method; the latter ties SCRAM auth to *that* cert.

## Least privilege — the minimal grant set

The client-streamed COPY forms need **no superuser** and **no
`pg_read/write_server_files`** (those are only for server-side FILE/PROGRAM
COPY). `COPY TO` needs `SELECT`; `COPY FROM` needs `INSERT` (fires triggers,
checks constraints — no extra grant).

```sql
-- Source (read)
GRANT CONNECT ON DATABASE <src_db>     TO copytool;
GRANT USAGE   ON SCHEMA   <src_schema> TO copytool;
GRANT SELECT  ON <source tables>       TO copytool;

-- Target (write) — tool CREATEs and therefore OWNS its tables
GRANT CONNECT       ON DATABASE <tgt_db>     TO copytool;
GRANT USAGE, CREATE ON SCHEMA   <tgt_schema> TO copytool;
GRANT TEMPORARY     ON DATABASE <tgt_db>     TO copytool;  -- if staging/temp used
```

Operations that **cannot be granted** — they require **ownership**:
`ALTER TABLE`/`RENAME`, `CREATE INDEX`, `DROP TABLE`. (`TRUNCATE` is the lone
grantable one.) **Design implication:** the tool creates and owns its target
(and staging) tables so ALTER/INDEX/RENAME/DROP work without elevation. Touching
pre-existing tables it doesn't own means those structural ops are simply
unavailable — design around it or require ownership transfer.

## Data-in-flight exposure

Rows live briefly in process memory while streaming. Mitigations:
- **Stream opaque `CopyData` buffers**; never parse, transform, or log row
  payloads; scrub buffers after use.
- **No core dumps** (`RLIMIT_CORE=0` / systemd `LimitCORE=0`, `MADV_DONTDUMP`).
- **Avoid swap** of sensitive buffers (disable swap or `mlock`).
- **End-to-end TLS** (`verify-full`) so rows are never on the wire in clear.

## RLS — a correctness *and* compliance landmine

- **`COPY … TO` does not error under RLS — it silently filters.** A
  least-privilege, non-owner, non-`BYPASSRLS` role copies **only the rows its
  SELECT policies permit** → a copy that looks complete but isn't.
- **`COPY FROM` is unsupported on RLS tables** — must use INSERT.
- Owners bypass RLS unless `FORCE ROW LEVEL SECURITY`; superuser/`BYPASSRLS`
  always bypass.
- **Why a row-count gate is NOT enough (reproduced):** as a non-owner,
  `SELECT`-only role on a table with a restrictive policy, `COPY … TO STDOUT`
  returned **50 of 100 rows with no error** — *and* `SELECT count(*)` **under
  that same role also returned 50**. So a validation that counts the source
  under the copy role sees `50 == 50` and **passes a partial copy**. Detecting a
  "row-count gap" cannot work when both sides are read through the same filter.
- **Required behaviour — fail loud at the read, not at the count:** every source
  read transaction runs **`SET row_security = off`** as part of its setup
  (alongside the snapshot import and `client_encoding`/`DateStyle` pins). With
  `row_security = off`, a query that *would* be filtered by a policy **errors**
  instead of silently undercounting:
  `ERROR: query would be affected by row-level security policy for table "…"`
  (reproduced; this is PostgreSQL's documented backup-safety behaviour, the same
  guard `pg_dump` relies on). The copy role must therefore be the table **owner**
  or **`BYPASSRLS`** for any RLS table we promise to copy in full.
- **Detect + record:** still detect RLS via `pg_class.relrowsecurity` /
  `relforcerowsecurity` at plan time and emit an `RLS_FILTER` warning; if the
  read raises under `row_security = off`, **fail the table** (do not downgrade to
  a warning) unless the operator passes an explicit opt-in
  (`--allow-rls-partial`). Record `row_security_off` (and the effective role) in
  job-state and the manifest so a copy can be *proven* not RLS-truncated.

## Audit & compliance

- `log_statement=mod` captures `COPY FROM` but only `all` captures `COPY … TO`
  (superuser-only). For real auditing use **pgAudit**. Neither logs the row
  payload — only the statement; logs alone won't tell you *which* rows moved.
- Emit an application-level **manifest** per job: source, target, table, row
  count, snapshot id + LSN, checksum(s), timestamp, operator/role — a defensible
  record of what was copied where, suitable for PII/compliance review.
