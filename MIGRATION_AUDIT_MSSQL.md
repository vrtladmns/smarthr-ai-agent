# Migration audit — PostgreSQL to SQL Server (db59244)

**Date:** 2026-08-22
**Target:** `db59244.public.databaseasp.net,1433` — SQL Server 2025 (17.0.4060.2), shared hosting
**Source:** PostgreSQL on the AWS server, `127.0.0.1:5432/recruitment`
**Purpose:** what has to change, in the database and in the code, before the agent can run on SQL Server.

Everything below was measured against both live databases, not assumed.

---

## 1. Summary

The move is feasible but it is not a configuration change. It is roughly a **two to three day job**, and there are three findings worth deciding on before starting.

| Finding | Detail |
|---|---|
| **Schema gap** | `recruiter_applications` is missing **27 of its 52 columns** on SQL Server. Two whole tables are absent. |
| **Query latency** | **261 ms per query, 2.8 s to connect**, versus about 1 ms locally. Six lookups alone cost 1.6 s. |
| **Shared ownership** | That database belongs to a .NET application (`__EFMigrationsHistory`, `hr_users`, `refresh_tokens`). Its EF migrations can alter columns the agent depends on. |

The latency is the one I would weigh hardest — see §5. It is not a reason the migration cannot be done, but it changes how the agent has to be written.

---

## 2. Where the two databases stand today

```
SQL SERVER db59244                       POSTGRES recruitment
  __EFMigrationsHistory      1 row         (not present - .NET only)
  audit_logs                 0             (not present - .NET only)
  hr_users                   1             (not present - .NET only)
  refresh_tokens            14             (not present - .NET only)
  recruiter_applications    14 rows        147+ rows
  recruiter_candidates      14             147+
  recruiter_email_events    57             hundreds
  recruitment_requirements   5             3 open
  (absent)                                 recruiter_sent_replies
  (absent)                                 recruiter_processed_messages
```

The SQL Server copy is a **stale snapshot** — it stopped being written to around the point the agent moved to Postgres. The live data is entirely on Postgres.

---

## 3. Schema work

### 3.1 `recruiter_applications` — 27 columns missing

```
budget_disclosed_at        hr_interviewer_name        interview_reminder_sent_at
budget_response            human_handled_at           interview_report
email_thread_id            interview_attempts         interview_scheduled_at
hr_approved_at             interview_availability     interview_session
hr_escalated_at            interview_completed_at     interview_started_at
hr_escalation_reason       interview_link_created_at  screening_current_location
hr_interviewer_email       interview_link_token       screening_current_salary
                           interview_reminder_count   screening_details
                                                      screening_expected_salary
                                                      screening_joining_days
                                                      teams_event_id
                                                      teams_join_url
```

Every one of these is load-bearing. Without `screening_details` there is no screening; without `interview_link_token` no interview; without `interview_session` no resume-after-restart.

### 3.2 Missing tables

- **`recruiter_sent_replies`** — the reply ledger. Without it the duplicate guard, the human-takeover check and the repeat protection all stop working.
- **`recruiter_processed_messages`** — inbound idempotency. Without it one email can be answered twice.

### 3.3 Other

- `recruitment_requirements.recommended_questions` missing (1 column).
- `recruiter_candidates` and `recruiter_email_events` already match.

### 3.4 Type mapping

| PostgreSQL | SQL Server | Note |
|---|---|---|
| `BIGSERIAL` | `BIGINT IDENTITY(1,1)` | 5 occurrences |
| `TIMESTAMPTZ` | `DATETIMEOFFSET` | 25 occurrences |
| `JSONB` | `NVARCHAR(MAX)` | 17 columns — SQL Server has no JSON type, only functions over text |
| `BYTEA` | `VARBINARY(MAX)` | `attachment_payload` |
| `TEXT` | `NVARCHAR(MAX)` | throughout |
| `UUID` | `UNIQUEIDENTIFIER` | `application_uid`, `candidate_uid` |

---

## 4. Code changes

66 SQL statements in `recruiter_agent.py`, 41 in `recruiter_dashboard.py`. The dialect-specific constructs:

| Construct | Uses | SQL Server equivalent |
|---|---|---|
| `::jsonb` casts | **42** | drop the cast; store as `NVARCHAR(MAX)` |
| `NOW()` | **28** | `SYSDATETIMEOFFSET()` |
| `%s` placeholders | every query | `?` (pyodbc) |
| `RETURNING` | 6 | `OUTPUT INSERTED.*` |
| `ON CONFLICT ... DO NOTHING/UPDATE` | 3 | `MERGE`, or catch the duplicate-key error |
| `LIMIT n` | many | `TOP n` or `OFFSET/FETCH` |
| `NULLS LAST` | 2 | `CASE WHEN x IS NULL THEN 1 ELSE 0 END, x` |
| `make_interval(mins => n)` | 1 | `DATEADD(minute, -n, SYSDATETIMEOFFSET())` |
| `psycopg.rows.dict_row` | 1 | build dicts from `cursor.description` |
| `ADD COLUMN IF NOT EXISTS` | ~30 migrations | `IF COL_LENGTH(...) IS NULL ALTER TABLE ...` |
| `CREATE INDEX IF NOT EXISTS` | several | `IF NOT EXISTS (SELECT 1 FROM sys.indexes ...)` |

### 4.1 Two ways to do it

**A. Restore the translation layer.** A previous version of this codebase carried one: `sql()` rewrote every query by string substitution before execution. It was removed on 2026-08-13 because it forced all SQL to the lowest common denominator and had silently drifted out of sync with the schema — the very gap in §3.1 is what it left behind. Re-adding it is the fastest route and reintroduces that fragility.

**B. Write the queries for SQL Server and keep one dialect.** More work up front, no translation layer, no drift. If Postgres is genuinely being retired this is the honest option.

I would not recommend maintaining both. The last attempt is why 27 columns went missing.

### 4.2 Things with no direct equivalent

- **`ON CONFLICT DO UPDATE ... WHERE`** in `claim_provider_message` — the atomic claim that stops an email being answered twice. `MERGE` can express it but has known concurrency caveats; needs `HOLDLOCK` and careful testing.
- **`jsonb` querying.** Nothing currently filters inside JSON, so `NVARCHAR(MAX)` is fine — but it closes that door.
- **Partial unique index** (`recruiter_applications_message_attachment_idx ... WHERE ... IS NOT NULL`) — SQL Server supports filtered indexes, syntax differs.

---

## 5. Latency — the finding I would weigh hardest

Measured from this machine to that host:

```
connect        2774 ms
simple query    261 ms
```

Against roughly 1 ms for local Postgres. Six lookups from a single inbound email cost **1.6 s** of pure network wait; a full email does considerably more, and the interview turn handler queries on every exchange.

Consequences to expect:
- Email processing slows from seconds to tens of seconds.
- The **voice interview** is the worst affected: it already had a latency problem, and every turn touches the database. Adding 261 ms per query undoes the work done there.
- Connections are opened per request in the dashboard; at 2.8 s each that alone is unusable without pooling.

If the migration goes ahead, connection pooling stops being optional, and the interview path needs its queries batched or cached.

**Worth checking first:** measure from the AWS server rather than from here. If the server sits near that host the numbers may be far better, and this concern shrinks. That single measurement should decide whether this is a two-day job or a redesign.

---

## 6. Ownership risk

`__EFMigrationsHistory`, `hr_users`, `refresh_tokens` and `audit_logs` say a .NET application owns this database and manages its schema with EF Core migrations.

If both systems write to `recruiter_applications`, then:
- an EF migration can rename or drop a column the agent needs, silently;
- two writers with different status vocabularies will disagree (that database already uses `Shortlisted`, `CV Rejected`, `Hired`, while the agent uses `screening_questions_sent`, `budget_disclosed`, `interview_link_sent`);
- there is no shared migration history between them.

Options: give the agent its own database on that server, agree that only one system writes each table, or align the status vocabulary explicitly. This needs deciding before data moves.

---

## 7. Plan

**Phase 0 — decide (before any code)**
1. Measure latency from the AWS server: `psql`-equivalent timing test against db59244.
2. Decide single-dialect (B) or translation layer (A).
3. Decide the ownership question in §6.

**Phase 1 — schema**
4. Write `MSSQL_CREATE_TABLES_SQL` covering all 52 application columns, both missing tables, the missing requirement column, and the indexes.
5. Apply to a **copy** first, never the live shared database.

**Phase 2 — code**
6. Reintroduce the provider switch in `RecruiterDatabase.__init__`.
7. Convert the statements in §4 — 107 across both files.
8. Add connection pooling.
9. Restore `pyodbc` to requirements and the ODBC layer to the Dockerfile (both removed on 2026-08-13).

**Phase 3 — data**
10. Export from Postgres, transform, load. `attachment_payload` (BYTEA to VARBINARY) and the JSONB columns need explicit handling.
11. Reconcile row counts and spot-check CVs, interview reports and screening details.

**Phase 4 — verify**
12. The existing 128 tests run against SQL Server; the integration suite needs a SQL Server fixture.
13. Run both databases in parallel for a period, comparing outcomes, before cutting over.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Latency makes the agent unusable | Measure from the server first (Phase 0.1) |
| EF migrations break the agent's columns | Separate database, or an agreed contract |
| Translation layer drifts again | Choose single-dialect (option B) |
| Data loss moving JSONB/BYTEA | Migrate to a copy, reconcile counts and samples |
| Shared hosting limits | Current size is 16 MB; check the plan's ceiling — CV binaries grow fast |
| No rollback once cut over | Keep Postgres running read-only for a fortnight |

---

## 9. What I would do

Not migrate, unless there is a requirement I am not seeing.

The reason to move is usually "the .NET API and the agent should share one database". That is achievable without moving the agent: the API can read the agent's Postgres, or the agent can push what the API needs over HTTP — `CV_UPLOAD_API_URL_TEMPLATE` already does exactly that.

Set against that: 27 columns to add, 107 statements to rewrite, a 261 ms per-query penalty on a system whose interview latency was a live complaint a week ago, and a schema owned by another application's migrations.

If the decision is to go ahead anyway, do §7 Phase 0 first. The latency measurement from the AWS server is one command and it determines whether the rest is worth starting.
