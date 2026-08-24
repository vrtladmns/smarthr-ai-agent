# Database audit — how this project uses PostgreSQL, and how to share it

**Date:** 2026-08-24
**Database:** PostgreSQL on the AWS server, `recruitment`
**Question answered:** where the database is used, which dashboard actions send email, and how to give another developer access so he can build a dashboard against the same database.

Everything below was read from the running system, not inferred.

---

## Part 1 — Where the database is used

### 1.1 One access layer

All database work goes through a single class, `RecruiterDatabase` in `recruiter_agent.py`. It exposes **49 methods**, and `recruiter_dashboard.py` imports and reuses the same class rather than opening its own connections. There is no ORM and no second access path.

The most-used entry points:

```
close                          75 call sites
execute                        62
log_email_event                37
rows / one                     62 combined
application_with_requirement   19
update_application_screening    9
mark_post_interview_outcome     8
```

That single layer is why sharing the database is tractable: there is one place where reads and writes are defined, and one schema to describe.

### 1.2 The tables

| Table | Cols | Rows (dev) | What it holds |
|---|---|---|---|
| `recruiter_applications` | 54 | — | one row per application: scores, screening answers, interview state, HR decisions, the CV file |
| `recruiter_candidates` | 24 | — | the person: contact details, skills, experience, full CV text |
| `recruitment_requirements` | 14 | — | the open roles, budgets, JDs, recommended questions |
| `recruiter_email_events` | 7 | — | processing log: every decision the agent made about an email |
| `recruiter_sent_replies` | 7 | — | **ledger**: what the agent has already said to whom |
| `recruiter_processed_messages` | 2 | — | **ledger**: which inbound emails have been handled |
| `agent_action_queue` | 10 | — | work requested by an external dashboard |

### 1.3 Who writes what

```
recruiter_agent.py
   recruiter_applications        SELECT 18  INSERT 1  UPDATE 28  DELETE 1
   recruiter_candidates          SELECT  1  INSERT 1  UPDATE  0  DELETE 1
   recruiter_sent_replies        SELECT 11  INSERT 1  UPDATE  0  DELETE 3
   recruiter_processed_messages  SELECT  6  INSERT 1  UPDATE  0  DELETE 2
   recruiter_email_events        SELECT  2  INSERT 1

recruiter_dashboard.py
   recruiter_applications        SELECT 19  UPDATE 4  DELETE 2
   recruitment_requirements      SELECT 10  INSERT 1  UPDATE 2  DELETE 1
   recruiter_candidates          SELECT  6  UPDATE 1  DELETE 1
   recruiter_email_events        SELECT  3  UPDATE 1  DELETE 1
```

Two things stand out. The **agent owns applications** — 28 update sites against the dashboard's 4. And the **two ledgers are written only by the agent**; the dashboard never touches them. That distinction matters in Part 3.

### 1.4 The two ledgers

These are not ordinary data. They are how the agent avoids repeating itself:

- **`recruiter_sent_replies`** — every candidate-facing message, with its scenario. Before sending anything the agent checks this table; the same scenario is not sent twice inside 24 hours. It is also how the agent recognises its own messages when deciding whether a human has taken a thread over.
- **`recruiter_processed_messages`** — an atomic claim on each inbound email. Microsoft Graph delivers notifications at least once, so without this one email can be answered twice.

Both exist because of real incidents: candidates receiving a dozen near-identical emails, and one email being answered twice 28 seconds apart. An outside writer editing either table reintroduces those faults.

---

## Part 2 — The dashboard, and which actions send email

`recruiter_dashboard.py` is a single-file HTTP server. It serves six read-only pages and twenty-one POST actions.

### 2.1 Read-only pages

```
/applications   /candidates   /requirements   /events   /login   /logout
```

Plus per-record detail pages, CV download and CSV export. These are pure `SELECT` — a new dashboard can reproduce all of them from the database alone.

### 2.2 Actions that touch the database only

These change rows and nothing else. Safe to reimplement as direct SQL:

| Action | Effect |
|---|---|
| `/requirements` (create) | insert a role |
| `/requirements/update`, `/requirements/status`, `/requirements/delete` | edit or close a role |
| `/candidates/update`, `/candidates/delete` | edit or remove a candidate |
| `/applications/status` | set a status field |
| `/applications/update` | edit application fields — **except** it re-scores if the requirement changes (see 2.4) |
| `/applications/delete` | remove an application |
| `/events/update`, `/events/delete` | edit the processing log |

### 2.3 Actions that send email — the important list

Traced through the call graph, these reach a mail send. **A database write alone does none of it:**

| Dashboard action | Beyond the database |
|---|---|
| `/applications/hr-approve` | **EMAIL** — screening questions, or the interview link if already screened |
| `/applications/post-interview-approve` | **EMAIL** — asks the candidate for HR-round availability |
| `/applications/post-interview-reject` | **EMAIL** — the rejection |
| `/applications/revoke-jd-rejection` | **EMAIL** — reopens and sends the screening questions |
| `/applications/final-select` | **EMAIL** — selection and document request |
| `/applications/final-reject` | **EMAIL** — the final rejection |
| `/applications/final-hold` | **EMAIL** — the hold notice |
| `/applications/send-interview-link` | **EMAIL** — the AI interview link |
| `/applications/send-teams-link` | **EMAIL + CALENDAR** — books the meeting, writes the HR calendar, cancels any previous event, then emails the link |

And one more that is not email but is equally invisible to SQL:

| `/applications/update` with a changed requirement | **LLM** — re-runs the CV against the new JD and rewrites `ats_score`, `jd_match_score`, `strengths`, `risks`, `missing_requirements` |

### 2.4 Why this matters for a new dashboard

If a React app runs:

```sql
UPDATE recruiter_applications SET application_status = 'hr_approved' WHERE id = 147;
```

the row changes, **no email is sent**, and the candidate waits for a message nobody sent. The dashboard shows success. This is the single largest risk in handing over database access, and it is not obvious from looking at the schema.

### 2.5 Status vocabulary

Several statuses carry behaviour and cannot be invented freely:

```
holds - the agent stops replying on that thread:
  hr_escalated, manual_hr_review, human_handled, interview_on_hold_hr_review

final - no further automation:
  rejected, withdrawn, interview_rejected, rejected_after_hr_round,
  no_open_requirement, selected_documents_requested, manual_hr_review

post-interview - pre-interview controls must not be offered:
  interview_completed, interview_on_hold_hr_review, hr_round_time_requested,
  interview_availability_received, interview_scheduled, final_hr_round_pending,
  final_hr_round_completed_pending_decision, hold_after_hr_round,
  rejected_after_hr_round, selected_documents_requested
```

Writing an unrecognised status leaves the application in a state the agent will not act on.

---

## Part 3 — Sharing the database with another developer

The goal: he builds a dashboard, the database stays exactly as it is, both of you keep working.

### 3.1 What he needs

| Need | Answer |
|---|---|
| Read the data | a database role, through views |
| Network route | private networking or an IP-locked TLS connection |
| Edit requirements, candidates, application fields | granted write access |
| Perform actions that email the candidate | **not** a raw write — see 3.4 |
| Download a CV file | an agent endpoint, not a `bytea` select over the WAN |

### 3.2 The role

`scripts/create_dashboard_rw_role.sql` creates `dashboard_rw`:

```
recruitment_requirements       SELECT INSERT UPDATE DELETE
recruiter_applications         SELECT INSERT UPDATE
recruiter_candidates           SELECT INSERT UPDATE
recruiter_email_events         SELECT INSERT
agent_action_queue             SELECT INSERT
recruiter_sent_replies         SELECT              (read-only, deliberately)
recruiter_processed_messages   SELECT              (read-only, deliberately)
```

Plus: 20-connection cap so a dashboard without pooling cannot starve the agent, a 30-second statement timeout, no `CREATE` on the schema, and no privileges on tables added later.

The two ledgers stay read-only for the reasons in 1.4. Everything else he can write.

If you would rather he could not write at all to begin with, `scripts/create_dashboard_role.sql` is the read-only equivalent.

### 3.3 Views as the contract

Five views exist so his code is not coupled to a 54-column table that changed shape several times this month:

```
dash_applications   application + candidate name + requirement title,
                    scores, screening answers, interview state, has_cv_file
dash_candidates     profile, skills, education, CV summary (no raw CV text)
dash_requirements   the open roles
dash_activity       which message went to which candidate and when
dash_events         the processing log
```

Tell him to build against these. When a column is added or renamed inside `recruiter_applications`, the view absorbs it and his dashboard keeps working.

### 3.4 The action problem, and the way round it

Given 2.3, he needs a way to make the agent actually do things. The mechanism already in the database:

```sql
UPDATE recruiter_applications
   SET requested_action    = 'approve_interview',
       requested_action_by = 'his-name'
 WHERE id = 147;
```

A trigger queues that on `agent_action_queue`; the agent runs the same function the built-in dashboard runs — email, calendar, scoring and all — and writes the outcome back:

```sql
SELECT action, status, error FROM agent_action_queue WHERE application_id = 147;
```

Available actions: `approve_interview`, `approve_hr_round`, `reject_after_interview`, `send_interview_link`, `send_teams_link`, `revoke_jd_rejection`, `reevaluate`, `select_after_hr_round`, `reject_after_hr_round`, `hold_after_hr_round`, `reopen_interview`.

*(This queue was added on 2026-08-24 and is the one part of this document describing recent work rather than long-standing behaviour. If you prefer not to keep it, the alternative is a small HTTP API on the agent exposing the same eleven functions — the audit conclusion is unchanged either way: actions must go through the agent, not through SQL.)*

### 3.5 Network

The database listens on loopback. To reach it from his server:

- **Same VPC** if he is in AWS — allow 5432 from his security group. Nothing public.
- **Otherwise** `listen_addresses = '*'`, a `hostssl recruitment dashboard_rw <HIS.IP>/32 scram-sha-256` line, and a security group rule for that one address. His server has a fixed IP, so the allowlist is stable.
- **Never** 5432 open to `0.0.0.0/0`. If Postgres runs in Docker with `ports: "5432:5432"`, note Docker publishes on all interfaces and bypasses ufw — check `sudo ss -ltnp | grep 5432`.

His connection string:

```
Host=<server>;Port=5432;Database=recruitment;Username=dashboard_rw;
Password=...;SSL Mode=Require;Trust Server Certificate=true
```

### 3.6 Operating alongside each other

- **Both dashboards can run at once.** They share one database; the agent is the only writer of the ledgers, and last-write-wins on ordinary fields.
- **Connection pooling is required.** Npgsql pools by default — confirm it is on, or the 20-connection cap will be hit.
- **The schema will keep changing.** Views are the mitigation; tell him not to select from the base tables.
- **Backups stay yours.** A second consumer does not change that, and now matters more.

### 3.7 Security and data protection

- Separate credentials, never the `recruiter` owner account — it can drop every table.
- TLS required; password sent out of band.
- Rotate on any change of personnel: `ALTER ROLE dashboard_rw WITH PASSWORD '...'`
- Revoke in one command: `DROP OWNED BY dashboard_rw; DROP ROLE dashboard_rw;`
- This is candidate personal data — names, phone numbers, salary expectations, CVs, interview transcripts. A dashboard that caches results holds that PII on infrastructure you do not control. Worth a written agreement covering what may be stored, for how long, and what happens when the engagement ends.

---

## Part 4 — Recommended sequence

1. Run `scripts/create_dashboard_rw_role.sql`, send the credentials out of band.
2. Give network access (3.5).
3. Send him this document — Part 2.3 is the part he must read.
4. He builds the read-only screens first; that is most of a dashboard and needs nothing else.
5. Wire actions through `requested_action`, never through a status write.
6. Confirm the agent is running so the queue drains (the webhook service does this).

## Part 5 — The one thing to get wrong-proof

If you take nothing else from this: **reading is safe, writing ordinary fields is safe, and writing a status is not an action.** Approving a candidate, rejecting them, sending a link or scheduling a meeting all send email and some touch a calendar. Those must go through the agent. Everything else he can do directly.
