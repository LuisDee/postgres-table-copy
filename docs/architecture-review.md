# Adversarial architecture review

We argue against our own design. This is the standing punch-list a skeptical
senior data architect / data engineer would raise, each with our resolution and
where it's tracked in the roadmap (`docs/design.md §6`). Revisit at every wave
boundary. Sources: `docs/research-notes.md §RN3/§RN4`.

| # | The hard question | Resolution | Tracked |
|---|---|---|---|
| 1 | "No global cross-host snapshot — what consistency do you actually promise?" | Be explicit: **consistent snapshot of the source only; the target is a separate domain.** Acceptable for one-shot loads into an empty/quiesced/post-swap target. Cross-host atomicity = stage→swap, client-orchestrated. | design §3.2, §3.14; Wave 2 |
| 2 | "Within the source, do all tables read at one instant?" | One `pg_export_snapshot()`, every worker `SET TRANSACTION SNAPSHOT`. A per-table-transaction pipeline would read each table at a *different* point — banned. | design §3.2; Wave 1/2 |
| 3 | "Collation/encoding drift will silently corrupt indexes." | Pre-flight check; **fail/warn** on `server_encoding`/`lc_collate`/collation-provider/version mismatch. Don't reuse index DDL blindly across providers; `REINDEX` + `ALTER COLLATION … REFRESH VERSION` on target. Pin `client_encoding`/`DateStyle`. | design §3.12; Wave 2 pre-flight |
| 4 | "COPY mishandles table features." | Explicit column lists **excluding `GENERATED … STORED`**; `OVERRIDING SYSTEM VALUE` + post-load `setval` for identity; **refuse or special-case large objects** (`pg_largeobject` is not moved by table COPY); resolve partition/inheritance/domain/enum/composite/extension-type cases at plan time. | design §3.1, §3.7, §3.9; Waves 1–3 |
| 5 | "Observability, idempotency, resumability?" | `plan` dry-run; per-table durable job state + resume token; structured logs (no row data); throughput + per-phase metrics; `rows`+`hash` verify; bounded memory via COPY backpressure; resume re-snapshots and is `--not-consistent` by nature. | design §3.6, §3.10; Wave 3 |
| 6 | "Fleet scale (100 DBs): connection budget, blast radius?" | Per-DB connection budgets + global rate limit; **session pooling / direct conns** for snapshot jobs (transaction-pooled PgBouncer breaks `SET TRANSACTION SNAPSHOT`); cap concurrent DBs; secrets via `.pgpass`/service regen; runbooks; blue/green swap. Long snapshot txns pin `xmin`/block vacuum — budget that cost. | design §3.3, §3.5; Wave 4 |
| 7 | "RLS will silently under-copy." | Detect `relrowsecurity`; warn at plan, **hard-fail** a row-count gap on RLS tables when validating; `COPY FROM` unsupported under RLS → INSERT path. | security.md; Wave 2 |
| 8 | "What must integration tests prove that unit tests can't?" | RLS non-owner copy detects the gap; differing-collation target triggers reindex/refresh; identity sequence resynced; generated columns aligned; bad server cert is **refused** by `verify-full`/`channel_binding=require`; snapshot held under concurrent writes; CTID chunk boundaries exhaustive+disjoint; atomic swap window. | design §8; every wave |
| 9 | "Build vs buy — why not just pgcopydb?" | For generic full-DB cross-host clone with consistency+parallelism+resume+LO+sequence handling, **use pgcopydb** (`pgcopydb_delegate`). A custom orchestrator is justified for: agent-driven per-table sub-jobs with external scheduling/observability, same-server schema refresh, fleet connection budgeting, selective table copies, and **independent verification** (which pgcopydb's own `compare` explicitly says is not a full check). | design §7; Wave 3 |
| 10 | "Your checksum can be fooled." | True — `hash` is probabilistic; `bit_xor` is blind to even-count duplicates; `hashtext` is 32-bit. We fold `count(1)` into the digest, offer per-chunk localisation, and **state the residual risk** in output. `digest` (sorted md5) is the definitive-but-costly fallback. | validation.md §2 |

Two corrections we baked in from the research: **`REJECT_LIMIT` is PG18** (not
17; PG17 has `ON_ERROR`/`LOG_VERBOSITY`), and **`COPY FROM` rollback still
leaves dead tuples** (all-or-nothing applies to visible rows, not disk → VACUUM
staging).
