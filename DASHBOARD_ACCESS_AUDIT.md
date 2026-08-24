# Sharing the agent's PostgreSQL with an external .NET/React dashboard

**Date:** 2026-08-22
**Database:** PostgreSQL on the AWS server, currently `127.0.0.1:5432/recruitment`
**Consumer:** a .NET + React dashboard, built by an external developer, hosted on a different server

---

## 1. The one thing to get right

**Reads can come from the database. Writes must not.**

Every meaningful dashboard action does more than change a row:

| Action | What actually happens |
|---|---|
| Approve for interview | checks screening is complete → emails the candidate the interview link → sets status |
| Approve for HR round | emails the candidate asking for availability → sets status |
| Reject after interview | emails the candidate the outcome → sets status |
| Send Teams link | creates a Teams meeting, writes the HR calendar, cancels any previous one, emails the link |
| Assign a requirement | re-runs the CV against that job description and rewrites ats_score, jd_match_score, strengths, risks |
| Revoke a JD rejection | reopens the application and sends the screening questions |

If the React app writes `application_status = 'hr_approved'` straight into Postgres, **none of that happens**. The row changes, no email is sent, no meeting is made, and the candidate sits waiting for a message nobody sent. The dashboard would look like it worked.

So the split is:

```
React  ──read──▶  Postgres views      (lists, detail, search, counts, charts)
React  ──POST──▶  agent API           (approve, reject, send link, schedule, assign)
                      │
                      └── the agent does the email, the calendar, the scoring, the status
```

---

## 2. Read access

`scripts/create_dashboard_role.sql` creates a `dashboard_app` role: read-only, capped at 20 connections, 15s statement timeout, no CREATE, and no privileges on tables added later.

It reads five views rather than tables, which matters for two reasons: the dashboard is insulated from column changes inside `recruiter_applications` (52 columns and still moving), and raw CV bytes and full CV text are never exposed.

| View | Contains |
|---|---|
| `dash_applications` | application joined to candidate name and requirement title, all scores, screening answers, interview timestamps, Teams link, interview report, `has_cv_file` flag |
| `dash_candidates` | profile fields, skills, education, CV summary — **not** `raw_cv_text` |
| `dash_requirements` | the open roles, budgets, JD, recommended questions |
| `dash_activity` | which message went to which candidate and when — a timeline without message bodies |
| `dash_events` | the processing event log |

Verified by connecting as the role:

```
ALLOWED  dash_applications, dash_candidates, dash_requirements,
         dash_activity, dash_events
BLOCKED  attachment_payload, raw_cv_text, the underlying tables,
         UPDATE, DELETE, CREATE
```

Run it with:

```bash
psql "$DATABASE_URL" -v app_password="'strong-password'" \
     -f scripts/create_dashboard_role.sql
```

### 2.1 The CV file

`dash_applications.has_cv_file` tells the dashboard whether a CV exists. The file itself should come from an agent endpoint (`/applications/<id>/cv`), not from the database — pulling multi-megabyte `bytea` over a WAN connection for a list view is the kind of thing that quietly takes a database down.

---

## 3. Network

The database currently listens on loopback. His server needs a route to it. In order of preference:

**A. Private networking.** If his server is also in AWS, put both in the same VPC (or peer them) and allow 5432 from his security group. Nothing touches the public internet.

**B. Public IP, locked to his server.** Practical when he is hosted elsewhere:

```
# postgresql.conf
listen_addresses = '*'

# pg_hba.conf  — TLS enforced, his server only
hostssl  recruitment  dashboard_app  <HIS.SERVER.IP>/32  scram-sha-256

# AWS security group: TCP 5432 from <HIS.SERVER.IP>/32 only
```

His connection string:

```
Host=<your-elastic-ip>;Port=5432;Database=recruitment;
Username=dashboard_app;Password=...;SSL Mode=Require;Trust Server Certificate=true
```

A server has a fixed IP, so the allowlist that is painful for a laptop is fine here.

**C. Do not** open 5432 to `0.0.0.0/0`. Postgres on a public IP is scanned within minutes.

If Postgres runs in Docker with `ports: "5432:5432"`, note that Docker publishes on all interfaces and **bypasses ufw**. Check `sudo ss -ltnp | grep 5432` before assuming it is private.

---

## 4. The write path — direct database writes

**Decision taken: the developer gets read/write on the database, no API.**
`scripts/create_dashboard_rw_role.sql` grants it.

That leaves one problem to solve, and it is solved in the database rather than
over HTTP.

### 4.1 Setting a status is not the same as doing the thing

`UPDATE recruiter_applications SET application_status = 'hr_approved'` changes a
row. It does not email the candidate, does not create a Teams meeting, does not
re-score a CV. The dashboard would look like it worked and the candidate would
be waiting for a message nobody sent.

So the dashboard asks the agent to act, in SQL:

```sql
UPDATE recruiter_applications
   SET requested_action    = 'approve_interview',
       requested_action_by = 'divya@webteam'
 WHERE id = 147;
```

A trigger turns that into a job on `agent_action_queue` and clears the column.
The agent picks it up within ~15 seconds and runs **the same function the
built-in dashboard runs**, so both interfaces behave identically. The dashboard
polls the queue row to show progress:

```sql
SELECT action, status, error, finished_at
  FROM agent_action_queue
 WHERE application_id = 147
 ORDER BY created_at DESC;
```

`status` moves `pending` to `running` to `done` or `failed`, with `error`
populated on failure.

Jobs can also be queued directly:

```sql
INSERT INTO agent_action_queue (application_id, action, payload, requested_by)
VALUES (147, 'reject_after_interview',
        '{"reason": "Not moving ahead at this stage."}', 'divya@webteam');
```

### 4.2 The actions available

| `action` | What the agent actually does |
|---|---|
| `approve_interview` | checks screening is complete, then emails the interview link — or the screening questions if it is not |
| `approve_hr_round` | emails the candidate asking for HR-round availability |
| `reject_after_interview` | emails the outcome (`payload.reason` optional) |
| `send_interview_link` | emails the AI interview link |
| `send_teams_link` | books the Teams meeting, writes the HR calendar, cancels any previous one, emails the link |
| `revoke_jd_rejection` | reopens a JD-score rejection and sends the screening questions |
| `reevaluate` | re-runs the CV against the assigned JD and rewrites the scores |
| `select_after_hr_round` / `reject_after_hr_round` / `hold_after_hr_round` | the final HR decision and its email |
| `reopen_interview` | clears the interview attempt counter |

Verified end to end: a plain `UPDATE` setting `requested_action` produced a
queued job, the agent ran it, and an email was genuinely sent. It also applied
the screening gate correctly — the candidate had not been screened, so it sent
the screening questions rather than an interview link, exactly as the built-in
dashboard would.

### 4.3 What the role may write

```
recruitment_requirements       SELECT INSERT UPDATE DELETE
recruiter_applications         SELECT INSERT UPDATE
recruiter_candidates           SELECT INSERT UPDATE
recruiter_email_events         SELECT INSERT
agent_action_queue             SELECT INSERT
recruiter_sent_replies         SELECT            (read-only, deliberately)
recruiter_processed_messages   SELECT            (read-only, deliberately)
```

Those last two are how the agent knows what it has already said and which emails
it has already handled. An outside writer editing them brings back the repeated
emails this system was fixed for, so they stay read-only. `agent_action_queue`
is INSERT-only for the same reason: the dashboard queues work, the agent decides
when it is done.

Confirmed by connecting as the role: editing requirements, applications and
candidates all allowed; forging a sent reply, clearing the reply ledger, marking
its own job done and dropping a table all refused.

### 4.4 If you would rather not use the queue

You can still write statuses directly — nothing stops it. Just be clear about
what that means: the row changes and no email is sent. That is the right
behaviour for a correction ("this was filed against the wrong role"), and the
wrong behaviour for a decision ("approve this candidate"). Decisions go through
`requested_action`.

---

## 5. The API alternative (not taken)

For the record, the other option was three or four HTTP endpoints wrapping the
same functions. It was not chosen. Three options were considered:

**A. Add a small JSON API to the agent** (recommended). A handful of token-authenticated endpoints wrapping the functions that already exist:

```
POST /api/applications/{id}/approve-interview     → send_interview_request_after_hr_approval
POST /api/applications/{id}/approve-hr-round      → send_final_hr_round_request
POST /api/applications/{id}/reject                → send_interview_rejection
POST /api/applications/{id}/send-interview-link   → send_interview_link_for_application
POST /api/applications/{id}/send-teams-link       → send_teams_link_for_application
POST /api/applications/{id}/requirement           → reevaluate_application_against_requirement
GET  /api/applications/{id}/cv                    → the CV file
```

Every one of those is an existing function; the work is HTTP plumbing and a shared secret, not new logic. Perhaps a day.

**B. Have his .NET app call the existing dashboard endpoints.** They work, but they are HTML form handlers with session cookies that redirect on success — awkward from React and not a contract anyone should depend on.

**C. Grant targeted writes on a few safe columns.** Only defensible for fields with no side effects — an HR notes field, a manual tag. Never `application_status`.

### 4.1 Status vocabulary

If any write path is granted, the dashboard must use the agent's statuses, not invent its own. Several carry behaviour:

```
holds (the agent stops replying):
  hr_escalated, manual_hr_review, human_handled, interview_on_hold_hr_review

final (no further automation):
  rejected, withdrawn, interview_rejected, rejected_after_hr_round,
  no_open_requirement, selected_documents_requested, manual_hr_review

post-interview (the pre-interview controls must not appear):
  interview_completed, interview_on_hold_hr_review, hr_round_time_requested,
  interview_availability_received, interview_scheduled, final_hr_round_pending,
  final_hr_round_completed_pending_decision, hold_after_hr_round,
  rejected_after_hr_round, selected_documents_requested
```

Writing an unrecognised status leaves the application in a state the agent will not act on.

---

## 6. Security

- **TLS required.** `sslmode=require` at minimum; `verify-full` with the server certificate is better.
- **Separate credentials** for the dashboard. Never share the `recruiter` account — it owns the tables and can drop them.
- **Send the password out of band**, not in the same message as the host.
- **Rotate** on any change of developer: `ALTER ROLE dashboard_app WITH PASSWORD '...'`.
- **Revoke in one command**: `DROP OWNED BY dashboard_app; DROP ROLE dashboard_app;`
- **This is personal data** — names, phone numbers, salary expectations, CV summaries, interview transcripts. Worth a written agreement covering what may be stored on his server, for how long, and what happens at the end of the engagement. A dashboard that caches query results is holding candidate PII on infrastructure you do not control.

---

## 7. Operational notes

- **Connection pooling** is mandatory from another server. The role is capped at 20 connections; a .NET app without pooling will exhaust that and start starving the agent. Npgsql pools by default — confirm it is not disabled.
- **The agent keeps changing the schema.** `recruiter_applications` gained columns several times this month. The views are the contract precisely so his EF models do not break; if he queries tables directly, they will.
- **Watch for slow queries** — `pg_stat_statements` will show whether the dashboard is scanning `recruiter_applications` unindexed.
- **Backups** stay your responsibility; a read-only consumer does not change that.

---

## 8. Recommended sequence

1. Run `scripts/create_dashboard_rw_role.sql` and send him the credentials.
2. Give him network access — §3 A or B.
3. Let him build the read-only screens; that is most of a dashboard and needs nothing else.
4. Point him at §4.2 for the actions, and have him use `requested_action`
   rather than writing statuses for anything that should notify a candidate.
5. Run the agent so the queue is drained — the webhook service does this
   automatically, or `--watch-actions` runs it standalone.

Step 3 unblocks him immediately, which is usually what matters. The write path can follow.

---

## 9. Alternative worth considering

If his dashboard is going to be the primary interface, exposing the agent entirely through an API and giving him no database access at all is cleaner: one contract, no schema coupling, no PII replicated onto another server, and no credentials to rotate. The cost is that every screen needs an endpoint, rather than him writing whatever query he needs.

Direct reads plus an action API — the arrangement in §1 — is the pragmatic middle, and what the rest of this document assumes.
