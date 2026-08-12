# Deploying the 2026-08-13 audit fixes

Server: `/home/ubuntu/company-policy-agent` (user `ubuntu`)

This release changes the database schema, removes the SQL Server code path, and
changes how the agent decides to reply. Read §1 before pulling.

---

## 1. Before you pull — three things that matter

**a. Back up the database.** The migration is additive, but this is the only
irreversible step in the release.

```bash
grep DATABASE_URL /home/ubuntu/company-policy-agent/.env      # confirm the target
pg_dump "<that DATABASE_URL>" > ~/recruitment-backup-$(date +%F-%H%M).sql
ls -lh ~/recruitment-backup-*.sql                              # sanity: not 0 bytes
```

**b. Check `DB_PROVIDER` in the server `.env`.** The agent is PostgreSQL-only now
and always reads `DATABASE_URL`. If the server has `DB_PROVIDER=mssql`, it used
to connect to SQL Server and will now connect to `DATABASE_URL` instead. Make
sure `DATABASE_URL` is set and points where you expect — the agent raises on
startup if it is empty.

**c. No new required settings.** Every new tunable has a working default. You do
not have to edit `.env` for this release. `.env.example` lists them if you want
to override any.

---

## 2. Deploy

```bash
cd /home/ubuntu/company-policy-agent

# find the real unit names first - they may differ from the files in deploy/
systemctl list-units --type=service | grep -i recruiter

sudo systemctl stop recruiter-agent recruiter-dashboard

git pull

# pyodbc was removed; this drops it and picks up anything else that moved
venv/bin/pip install -r requirements-recruiter.txt

# apply the migration explicitly rather than waiting for the first webhook
venv/bin/python recruiter_agent.py --init-db
# expect: "Recruiter tables are ready."

sudo systemctl start recruiter-agent recruiter-dashboard
sudo systemctl status recruiter-agent recruiter-dashboard --no-pager
```

If you deploy the agent via Docker instead of systemd, the Dockerfile changed
(the SQL Server ODBC layer is gone), so rebuild without cache:

```bash
docker compose build --no-cache recruiter-agent && docker compose up -d
```

---

## 3. Verify

```bash
# webhook is alive
curl -fsS http://127.0.0.1:8081/health && echo

# migration landed
psql "$DATABASE_URL" -c "\dt recruiter_*"
# expect recruiter_processed_messages and recruiter_sent_replies to exist

psql "$DATABASE_URL" -c "\d recruiter_applications" | grep -E "budget_disclosed_at|budget_response|human_handled_at"
# expect all three columns

# no startup errors
sudo journalctl -u recruiter-agent -n 50 --no-pager
```

Then send a test email to `career@virtualadmins.org` from an address with no
history and watch:

```bash
sudo journalctl -u recruiter-agent -f
```

**Expected on this tenant:** a warning line
`graph_thread_orderby_unsupported_paging_instead`. That is normal and correct —
this tenant rejects `$filter` + `$orderby` on `/messages`, so the agent falls
back to paging the conversation. It means the thread-window fix is working.

**New events you should see in the logs over the first few days:**

| Event | Means |
|---|---|
| `candidate_reply_sent` | a reply went out, with its scenario name |
| `duplicate_reply_suppressed` | the loop guard fired — the agent tried to repeat itself and was stopped |
| `acknowledgement_no_reply` | a "thanks" was correctly left unanswered |
| `human_takeover_detected` | someone replied by hand; the agent has backed off that thread |
| `persona_leak_detected` | the model drifted out of the HR voice and was filtered |
| `budget_response_classified` | a candidate answered the budget question |
| `interview_recording_too_short` | a stub recording was rejected instead of scored |

`duplicate_reply_suppressed` appearing is a *good* sign early on — it means the
backstop is catching something. If it appears constantly for one candidate,
that thread has a real problem worth looking at.

---

## 4. Behaviour changes to expect

- **`screening_negotiation` no longer exists.** Any application sitting in that
  status will fall through to the HR hold path on the candidate's next reply,
  which is the intended landing place. Nothing is stranded, but HR will see a
  few land in their queue shortly after deploy.
- **New statuses:** `budget_disclosed` (waiting on a yes/no about salary) and
  `human_handled` (a person took the thread over).
- **The agent replies less.** Acknowledgements get no answer, and no scenario
  repeats within 24 hours. Quieter is the fix, not a fault.
- **Escalated applications go quiet to the candidate** apart from one holding
  note, and HR gets at most one email per application per day.

---

## 5. Rollback

```bash
cd /home/ubuntu/company-policy-agent
sudo systemctl stop recruiter-agent recruiter-dashboard
git log --oneline -5          # find the commit before this release
git checkout <previous-sha>
venv/bin/pip install -r requirements-recruiter.txt
sudo systemctl start recruiter-agent recruiter-dashboard
```

The new tables and columns are additive and the old code ignores them, so a code
rollback needs **no** database rollback. Only restore the dump if the database
itself is damaged.

---

## 6. Known-good log lines vs real errors

`graph_thread_orderby_unsupported_paging_instead` — **expected**, means the fix works.

`email_processing_failed` with a `psycopg` error — **real**, investigate. If you
see `InFailedSqlTransaction` in the *cleanup* handlers after a first error, you
are on a build older than the 2026-08-13 hotfix; pull again.

---

## 7. Recovering a stuck message

If processing crashed before the 2026-08-13 hotfix, the inbound message stays
claimed in `recruiter_processed_messages` and is skipped forever:

```
INFO email_already_processed_skipped {...}
```

Release it and re-run:

```bash
venv/bin/python recruiter_agent.py --list-claimed 20
venv/bin/python recruiter_agent.py --release-message '<the id from the log uid field>'
venv/bin/python recruiter_agent.py --run-once
```

Or release everything claimed that never produced a reply:

```bash
venv/bin/python recruiter_agent.py --release-failed-messages
```

That is safe: the reply ledger still prevents anything already sent from being
sent twice.

---

## 8. Not covered by this deploy

- **Rotate the credentials in `.env`** (Graph client secret, DeepSeek, LangSmith,
  dashboard login). They were exposed during the audit and are unchanged.
- **Re-review applications 91 and 20 by hand.** Both were scored by the old
  interview pipeline; 91 was talked past on a question and 20 had its technical
  vocabulary destroyed by speech-to-text.
- **`recruiter_agent copy*.py`** are untracked local files and were not deleted.
  They will not appear on the server.
