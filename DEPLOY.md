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

## 8. Scanned CVs (OCR)

Some candidates send a photograph or scan of a printed CV. Those PDFs carry no
text layer at all, so nothing can be read from them and the agent replies asking
for a text-based file. Two real examples: `CV Parveen Kumar.pdf` and
`YOUNIS Updated Resume 2026-1.pdf`, both zero extractable characters.

Installing Tesseract lets the agent read them:

```bash
sudo apt-get install -y tesseract-ocr
venv/bin/pip install -r requirements-recruiter.txt   # pymupdf, pytesseract, pillow
sudo systemctl restart recruiter-agent recruiter-dashboard
```

Confirm it works:

```bash
venv/bin/python -c "import pytesseract; print(pytesseract.get_tesseract_version())"
```

The chain is pypdf, then PyMuPDF, then OCR - each only if the previous found
nothing - so a normal CV never pays the OCR cost. Measured on the two CVs above:
2.7s and 3.5s, recovering 3816 and 4142 characters.

Without Tesseract nothing breaks: `ocr_unavailable` is logged once and the
candidate gets the same "I could not read your CV" reply as before.

Tunable: `RECRUITER_OCR_ENABLED` (default true), `RECRUITER_OCR_MAX_PAGES`
(default 5), `RECRUITER_OCR_DPI` (default 200).

---

## 9. Not covered by this deploy

- **Rotate the credentials in `.env`** (Graph client secret, DeepSeek, LangSmith,
  dashboard login). They were exposed during the audit and are unchanged.
- **Re-review applications 91 and 20 by hand.** Both were scored by the old
  interview pipeline; 91 was talked past on a question and 20 had its technical
  vocabulary destroyed by speech-to-text.
- **`recruiter_agent copy*.py`** are untracked local files and were not deleted.
  They will not appear on the server.

---

## 10. Giving a developer database access

Do not hand over `DATABASE_URL` as it stands. Two reasons:

**It will not work.** `127.0.0.1` means "the machine running the query". From the
developer's laptop it points at their own computer, not your server.

**It is the owner account.** `recruiter` created every table, so it can also drop
them, and nothing in the logs would tell their queries apart from the agent's.
It reads raw CV bytes, full CV text, phone numbers and salary expectations for
every candidate you have ever received.

### Create a scoped role instead

```bash
psql "$DATABASE_URL" -v webdev_password="'a-strong-password'" \
     -f scripts/create_webdev_role.sql
```

That role is read-only, cannot create or drop anything, has a 30s statement
timeout, and sees candidates and applications through views that omit
`attachment_payload` and `raw_cv_text`. Verified behaviour:

```
ALLOWED  read requirements / candidate view / application view
BLOCKED  raw candidates table, raw CV bytes, reply ledger
BLOCKED  INSERT, UPDATE, DELETE, DROP, CREATE
```

Revoking it later is one command: `DROP OWNED BY webdev; DROP ROLE webdev;`

### Reaching it over the network

Postgres listens on loopback only, which is the right default. Pick one:

1. **SSH tunnel (simplest, nothing exposed).** The developer runs:
   `ssh -L 5432:127.0.0.1:5432 ubuntu@<server>` and connects to
   `postgresql://webdev:...@127.0.0.1:5432/recruitment`. Give them an SSH key,
   not the database port.
2. **Open the port to one address.** In `postgresql.conf` set
   `listen_addresses = '*'`, add a `hostssl recruitment webdev <their-ip>/32 scram-sha-256`
   line to `pg_hba.conf`, and allow that single IP in the AWS security group.
   Require TLS; never open 5432 to `0.0.0.0/0`.
3. **Give them an API, not the database.** Usually the right answer for a front
   end, and it keeps schema changes from breaking their code.

### Before you send anything

This data is candidate personal information - names, phone numbers, salaries,
CVs. Worth a written agreement about what they may store and for how long, and
worth deciding whether they need real data at all or a scrubbed copy would do.
