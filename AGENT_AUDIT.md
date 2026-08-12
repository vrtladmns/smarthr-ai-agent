# AI Recruiter Agent — Diagnostic Audit

**Date:** 2026-08-12
**Scope:** `recruiter_agent.py`, `recruiter_dashboard.py`, `config.py`, `llm_factory.py`, deploy units
**Live systems inspected (read-only):** Outlook mailbox `career@virtualadmins.org` via Microsoft Graph, LangSmith project `Voice RAG Agent`, OneDrive interview recordings, local Postgres `recruitment`
**Target DB:** PostgreSQL only (SQL Server path treated as dead code — see §7.1)
**Inspection phase made no writes.** Implementation followed on 2026-08-13 — see §0.

---

## 0. Implementation status — 2026-08-13

Everything below has been implemented and verified against the live data that produced it, except where noted in §0.2.

**Verification:** 30 regression tests in `tests/` (17 of them failed against the original code and now pass), a Postgres end-to-end write-path test, and a replay of the real conversation that looped.

### 0.1 Replay of the failing thread, before and after

Running the fixed pipeline against aaqib's real 24-message conversation:

```
messages fetched : 24   (was 10)
time span        : 05:26:12 -> 09:36:12   (was 05:26:12 -> 06:21:36)
context chars    : 7868
  '373000'  present in prompt context: True     (was absent)
  '1000000' present in prompt context: True     (was absent)
  'kolkata' present in prompt context: True     (was absent)
  '90 days' present in prompt context: True     (was absent)
```

The salary answers the agent kept re-requesting are now in front of the model.

Note: this tenant **rejects** `$filter` + `$orderby` on `/messages` with `InefficientFilter`, so the paging fallback in `fetch_thread_messages_by_paging` is what actually recovers the thread. Without it the fix would have failed silently.

### 0.2 Not done, and why

| Item | Status |
|---|---|
| §3.3 Layer 4 — pgvector matching | Not done. Needs the extension enabled and an embedding backfill; it is an optimisation, not a fix. The LLM matcher is now the authority, which was the point. |
| §6 — split `process_email` into a state machine | Partially done. The budget and hold paths were extracted into `route_completed_screening` and `handle_budget_response`; the CV-intake path is still inline. A full rewrite needs staging traffic to verify. |
| §7.8 — rotate secrets | Cannot be done from here. `.env` was left untouched on purpose; the unused `MSSQL_*` keys are now ignored by the code but rotating live credentials is yours. |
| §7.9 — delete `recruiter_agent copy*.py` | **Not deleted.** These are **untracked** — not in git history — so deleting them is irreversible. The audit previously claimed git had them; that was wrong. Delete them yourself once you have confirmed you do not want them. |
| §8.7 item 14 — re-review applications 91 and 20 | A human decision, not code. |
| §32 — alerting | The events are now emitted (`duplicate_reply_suppressed`, `persona_leak_detected`, `human_takeover_detected`, `interview_recording_too_short`, `budget_response_classified`); wiring them to a notifier is an infra task. |

### 0.3 Deploy notes

- `init_schema()` creates the two new tables and three new columns on start; it is additive and safe to run against the live database.
- New statuses: `budget_disclosed`, `human_handled`. `screening_negotiation` is gone — any application still sitting in it will fall through to the HR hold path on the next reply, which is the intended landing place.
- `pyodbc`, the ODBC Dockerfile layer, and `scripts/install_mssql_odbc_ubuntu.sh` were removed. Rebuild the image.
- New tunables are in `.env.example`; all have working defaults.

---

## 1. Executive summary

The agent has **five independent infinite-reply loops**, not one. Each was reproduced against live data. None is an LLM quality problem — the writing is fine; the agent is reasoning from a stale, truncated, or contradicted view of the world.

| # | Root cause | Effect | Severity |
|---|---|---|---|
| **A** | Graph thread fetch returns the **oldest 10** messages, not the newest | Thread memory freezes after ~6 exchanges → loops forever | 🔴 Critical |
| **B** | 8000-char prompt budget truncates the *newest* messages | Compounds A — window shrinks to ~6 messages | 🔴 Critical |
| **C** | Static `ROLE_FAMILY_KEYWORDS` gate runs **in front of** the dynamic matcher | Every US Bookkeeper CV rejected as "wrong CV"; breaks on any new requirement HR adds | 🔴 Critical |
| **D** | HR approval sets status to `interview_time_requested`, then `ensure_interview_link` returns early | Status never advances → **interview link resent on every reply, forever** | 🔴 Critical |
| **E** | `latest_reply_accepts_budget` returns `True` for any small number | "My notice period is 90 days" ⇒ **HR approval silently bypassed**, salary falsified in DB | 🔴 Critical |
| **F** | `screening_negotiation` has no exit unless the issue is salary | Work-terms mismatch ⇒ budget re-disclosed on every reply, never escalates | 🔴 Critical |
| **G** | Same acceptance function misses 8 of 9 real phrasings | Candidate agrees to budget ⇒ total silence + HR inbox spam | 🟠 High |
| **H** | No dedupe on processed message ids | Duplicate Graph notifications ⇒ two different replies to one email | 🟠 High |
| **I** | `is_status_followup` matches the bare word `interview` | "Thanks for the invite" ⇒ link resent | 🟠 High |
| **J** | No acknowledgement detection | Agent replies to "Thanks", "Noted", "Will do" | 🟠 High |
| **K** | No human-takeover latch | Agent talked over a colleague who had de-escalated | 🟠 High |

**Your instinct about the escalation flow is correct, and it is worse than you thought.** §4 covers it in full: the budget-acceptance race has a bypass *and* a deadlock, negotiation has no exit when the blocker isn't salary, and HR clicking "Approve For Interview" puts the application into a permanent resend loop.

**§4.7 specifies the flow you asked for** — disclose the budget exactly once, classify the answer, then either proceed or escalate. It is a net deletion of code (§4.9): the whole negotiation state disappears, and with it every loop reachable from it.

**§5.4** covers persona consistency — the agent currently tells candidates it will "share your profile with the team", which contradicts the "HR Team" signature. That phrasing is instructed by the code, not improvised.

### The AI voice interview (§8) — separate subsystem, separate problems

I transcribed application 91's recording from OneDrive. Your reading of it is exactly right, and the cause is precise:

| # | Root cause | Effect | Severity |
|---|---|---|---|
| **L** | Client won't submit under 10 words; server won't honour a repeat request over 12 | **A repeat request is only deliverable in a 3-word window.** Hers was 6 words, so it was never sent — she kept talking, hit 34 words, and the echo was scored as her answer | 🔴 Critical |
| **M** | Mic is closed while the agent speaks — no barge-in in the browser path | Her protest *"I am answering the question actually"* is on the recording but never reached the transcript | 🔴 Critical |
| **N** | STT mangles technical vocabulary; the judge scores the transcript | "PostgreSQL" → "post gracious girl", "Redis" → "red is", "Django" → "DJ"/"Jungle" — then scored 3–4/10 for *"lacked coherence"* | 🔴 Critical |
| **O** | Turn LLM re-sends full CV + JD every turn; TTS cache-misses every generated line; filler nudge blocks the real reply | **47.4% of the interview is silence**; 15s from answer to next question | 🟠 High |
| **P** | Agent's next question bleeds into the stored answer text | Corrupted transcript is what gets scored | 🟠 High |

Applications 20, 21, 73, 89 and 91 were all scored by this pipeline. At minimum 91 and 20 should be re-reviewed by a human before any decision stands.

---

## PART 1 — Why it repeats itself

## 2. The agent can only ever see the first ~6 messages of a thread

### 2.1 Bug A — `$top` without `$orderby` returns the oldest messages

`recruiter_agent.py:3278-3294`

```python
def fetch_thread_messages(self, conversation_id: str, limit: int = 10) -> list[ThreadMessage]:
    params = {
        "$filter": f"conversationId eq '{safe_conversation_id}'",
        "$top": str(limit),                    # <-- no $orderby
        ...
    }
```

The code assumes Graph returns newest-first. It does not.

**Proof** (live, conversation `…g8fKFPF4jAY=`):

```
top=5   ['05:26:12', '05:26:42', '05:49:36', '05:49:47', '06:04:02']
top=10  ['05:26:12', '05:26:42', '05:49:36', '05:49:47', '06:04:02',
         '06:04:49', '06:17:19', '06:17:36', '06:21:12', '06:21:36']
```

Ascending. That conversation holds **24 messages spanning 05:26 → 09:36**; the agent's view was frozen at **05:26 → 06:21** permanently.

### 2.2 Bug B — the truncation cuts the wrong end

`build_thread_context()` (`recruiter_agent.py:897-919`) emits messages oldest-first, capping each body at 1800 chars. Every consumer then slices the **front**:

`recruiter_agent.py:3718` (`draft_reply`), `:3971` (`extract_screening_answers`), `:4002` (`extract_interview_schedule`), `:3798`, `:3831`

```python
Email thread:
{thread_context[:8000]}
```

Slicing the front of an oldest-first list **keeps stale messages and discards recent ones**.

**Proof** (same thread, reconstructed exactly as the agent builds it):

```
full thread_context chars: 15668
per-message raw body chars: [2, 852, 1159, 1870, 2055, 3092, 3383, 4366, 4678, 5588]
Message headers in full ctx:       ['1'..'10']
Message headers inside first 8000: ['1','2','3','4','5','6']
```

Bodies grow monotonically (852 → 5588 chars) because each Gmail reply quotes the whole history. **The longer a conversation runs, the smaller the fraction the agent can see.** Degradation into a loop is guaranteed.

### 2.3 What this produced

`aaqib1408@gmail.com`, live mailbox:

| Time | Who | What |
|---|---|---|
| 06:17:01 | Candidate | "Current CTC-373000, Expected CTC-1000000, location-kolkata, Notice period-90 days" |
| 06:17:35 | **Agent** | "could you please share the current salary, expected salary…" |
| 06:20:55 | Candidate | *(repeats all four)* |
| 06:21:35 | **Agent** | *asks for the same four* |
| 06:26:32 | Candidate | "I already Sent please check the mail." |
| 07:26:34 | Candidate | **"I already Sent all the details but still receiving the same mail again and again"** |
| 09:36:11 | **Agent** | *still asking for the same four* |

He answered at 06:17 — **message #7**. After truncation the agent sees messages 1–6. The answer never entered a prompt.

`himans8285@gmail.com`: confirmed work terms **five times** in explicit list form ("1) Night Shift - OK 2) 5 days working from the Mohali office - OK 3) Cab facility - OK"). Re-asked every time, until a human broke in with *"Himanshu please be calm this was an AI you were interacting with"*.

### 2.4 Fix

```python
# 1. fetch_thread_messages — get the NEWEST messages
params = {
    "$filter": f"conversationId eq '{safe_conversation_id}'",
    "$orderby": "receivedDateTime desc",
    "$top": str(limit),      # raise to 25; these threads run to 24+
}
```

If Graph rejects `$filter` + `$orderby` on this shape, page via `@odata.nextLink` and keep the tail.

```python
# 2. build_thread_context — keep the TAIL, strip quoted history per message
def build_thread_context(inbox_email, max_chars=8000, per_message=1200) -> str:
    blocks = [
        f"Message {i}\nFrom: {m.sender}\nDate: {...}\nBody:\n{latest_reply_text(m.body)[:per_message]}"
        for i, m in enumerate(messages, 1)
    ]
    out, total = [], 0
    for block in reversed(blocks):          # newest first
        if total + len(block) > max_chars:
            break
        out.append(block); total += len(block)
    return "\n\n---\n\n".join(reversed(out))
```

Applying `latest_reply_text()` per message is a large extra win — verified, it reduced a 10,655-char body to the 80-char actual reply. The agent currently spends most of its context re-reading quoted copies of its own emails.

---

## PART 2 — Role matching

## 3. The static keyword list is both broken *and* the wrong design

You're right that this can't stay static. It's worse than a maintenance problem — **the dynamic matcher you need already exists and is being short-circuited by the static one.**

### 3.1 The current call order

In `process_email` (`recruiter_agent.py:5385`), for every CV:

```
1. roles_are_compatible(requested_position, cv_position, cv_text)   <-- STATIC keyword gate
       └─ False?  →  reply_wrong_cv(...)  →  continue        ← candidate rejected, loop exits
2. deterministic_requirement_match(...)                             <-- token overlap on titles
3. self.ai.match_requirement(extracted, requirements)               <-- LLM, reads job_description
```

Step 3 is a genuinely good, requirement-driven matcher (`recruiter_agent.py:3866-3904`): it takes the open requirements straight from the DB with their `job_description`, matches on role family, domain and responsibilities, and requires confidence ≥ 0.75.

**But it never runs for anyone step 1 rejects.** The hardcoded list is a hard gate in front of the intelligent path.

### 3.2 The specific defect

`recruiter_agent.py:1873-1884`

```python
"accounting": {
    "accountant", "accounting", "bookkeeping", "gst", "tds",
    "tally", "ledger", "reconciliation", "payable", "receivable",
},
```

`bookkeeping` is present. **`bookkeeper` is not.** `position_tokens()` does no stemming, so `role_families_from_text("US Bookkeeper")` returns an **empty set**. In `roles_are_compatible()` (`:1922-1965`) the family check is guarded by `if requested_families and ...`, so an empty set silently skips it and falls to:

```python
overlap = requested_words & position_tokens(cv_search_text)
return len(overlap) / len(requested_words) >= 0.66
```

`{"us", "bookkeeper"}` vs an "Accounts Payable Analyst" CV → `{"us"}` → `0.5 < 0.66` → **rejected**.

**Proof** (live, the actual attachment the candidate sent):

```
CV file: Md_Aaqib_US_Bookkeeper_Resume.pdf   chars: 3343
requested tokens   : {'bookkeeper', 'us'}
requested families : set()                       <-- the bug
cv families        : {'accounting', 'it_support'}
'bookkeeping' in cv? True   |  'bookkeeper' in cv? True

roles_are_compatible('US Bookkeeper',            'Accounts Payable Analyst') = False
roles_are_compatible('US Full Cycle Accountant', 'Accounts Payable Analyst') = True
```

The file is *named* `US_Bookkeeper_Resume.pdf`, contains both words, classifies into `accounting` — and is still rejected. The identical CV passes for "US Full Cycle Accountant" only because that title contains `accountant`, which has its own special-case branch at `:1935`.

Every Bookkeeper applicant hit this: `aaqib1408@` ("CV is for an Accounts Payable Analyst"), `yashikag2011@` ("actually for an Accounts Trainer position"), `aroraishika923@` ("for a Quote to Cash position", twice).

There is also an internal inconsistency: `GENERIC_ROLE_WORDS` (`:1605`) already contains `us`, `senior`, `full`, `cycle` — but only `meaningful_role_tokens()` uses it. `roles_are_compatible` uses raw `position_tokens`, so `"us"` sits in the denominator and drags every "US …" title below threshold.

### 3.3 The design it should have

**Requirements are data. Role knowledge must come from the requirement row, not from source code.** When HR adds "US Payroll Specialist" or "AP Team Lead" tomorrow, nothing in `recruiter_agent.py` should need editing.

**Delete `ROLE_FAMILY_KEYWORDS`, `is_design_role_text`, and `roles_are_compatible` entirely.** Replace with:

**Layer 1 — cheap deterministic pre-filter (keep, but non-fatal).**
`deterministic_requirement_match` already scores candidate tokens against `position_title` from the DB. Keep it as a fast path for obvious hits. Its verdict must never reject a candidate — only *skip* the LLM call when confident.

**Layer 2 — requirement-driven semantic match (already built, just promote it).**
`self.ai.match_requirement()` is the right primitive. Make it the authority. Give it the full `job_description` (currently truncated to 1000 chars at `:3877`) and have it return, per requirement, a fit score plus evidence.

**Layer 3 — optional, if you want to remove the LLM from the hot path.**
Store an embedding of `position_title + job_description` on `recruitment_requirements` (add `embedding vector`, pgvector), and one for each CV on `recruiter_candidates`. Match by cosine similarity, LLM only to break ties. This scales to any number of requirements with zero code changes and gives HR a tunable threshold in the dashboard rather than a code deploy.

**Layer 4 — never hard-reject on role.**
A role mismatch is a *signal*, not a verdict. Record it as `jd_match_score` and let the existing `RECRUITER_SCREENING_JD_MIN` threshold decide. If the agent is genuinely unsure, ask **once** — then accept whatever the candidate sends and let scoring handle it. Telling a candidate three times that their correctly-named CV is for the wrong job is the worst outcome available.

**Migration note:** requirements HR enters need enough substance for Layers 2–3 to work. `recruitment_requirements.job_description` is populated (332–358 chars locally) but thin. Make it a required, structured field in the dashboard form (responsibilities, must-have skills, nice-to-have, adjacent titles that should match). That is where role knowledge belongs — in the row HR owns, not in a Python set.

---

## PART 3 — The HR escalation lifecycle

## 4. Escalation is broken in both directions

You suspected this creates problems. It does — there are **four distinct defects**, and the most severe one fires when HR does exactly what the system asks.

### 4.1 The intended flow

```
screening reply → screening_fit() fails on budget
   → escalate_to_hr()            : status = 'hr_escalated', email HR, ALSO reply to candidate
   → HR clicks "Approve For Interview" in dashboard
   → send_interview_request_after_hr_approval() : email interview link to candidate
```

### 4.2 Defect 1 🔴 — HR approval creates a permanent resend loop

`send_interview_request_after_hr_approval()` (`recruiter_agent.py:5890`) runs in this order:

```python
db.mark_hr_approved_for_interview(application_id)   # -> status = 'interview_time_requested'
token = db.ensure_interview_link(application_id)    # -> token already exists, RETURNS EARLY
```

`mark_hr_approved_for_interview` (`:2565`) sets `application_status = 'interview_time_requested'`.

`ensure_interview_link` (`:2398-2420`) sets `application_status = 'interview_link_sent'` **only when it mints a new token**:

```python
token = application.get("interview_link_token") if application else None
if token and not reset:
    return token          # <-- status is never updated
```

So after HR approves, the status is stuck at `interview_time_requested` forever. Now every subsequent candidate reply hits `process_email` (`:4959-4961`):

```python
if current_status in {"interview_time_requested", "interview_link_pending"}:
    self.reply_interview_availability_request(inbox_email, active_application)
    return True
```

And `reply_interview_availability_request` (`:4250-4270`) **sends the interview link and never updates the status** — it logs an `interview_link_sent` *email event* but leaves the application row untouched.

**Result: candidate replies → link resent → status unchanged → candidate replies → link resent → forever.** This is the loop seen with `akhilreddy4447@gmail.com` (link at 07:16:50, resent 07:20:15 and 07:22:14).

The status is only ever set correctly on the *first* link, by the side effect inside `ensure_interview_link`. Putting a state transition inside a function named "ensure X exists" is the structural cause.

**Fix:** make status transitions explicit and idempotent, never a side effect of a getter:

```python
def mark_interview_link_sent(self, application_id: int):
    self.execute("""UPDATE recruiter_applications
                    SET application_status = 'interview_link_sent',
                        interview_link_created_at = COALESCE(interview_link_created_at, NOW())
                    WHERE id = %s""", (application_id,))
```

Call it from **every** path that emails a link, and strip the status write out of `ensure_interview_link`.

### 4.3 Defect 2 🔴 — the budget-acceptance check fires on any small number

This is the race you asked about, and the detector that guards it is badly broken.

`process_email` (`:4826-4832`):

```python
candidate_accepted_escalated_budget = (
    current_status == "hr_escalated"
    and latest_reply_accepts_budget(inbox_email.body, active_application)
)
if current_status in {...} or candidate_accepted_escalated_budget:
    # resumes screening, sets expected_salary = budget_max,
    # screening_fit() now passes -> 'interview_link_pending' -> sends interview link
```

`latest_reply_accepts_budget` (`:1332-1369`) scans the reply for numbers **before** it looks at any acceptance language:

```python
for number in numbers:
    annual_number = number * 100000 if number <= 200 and re.search(r"\b(lpa|lakh|lac)\b", ...) else number
    if annual_number >= 10000:
        saw_salary_number = True
    if annual_number <= budget_max:
        return True          # <-- any number below budget_max means "accepted"
```

**Proof** (executed against the real function, `budget_max = 1,000,000`):

```
reply                                              accepts_budget
"My notice period is 90 days."                     True
"I need 30 days notice period."                    True
"I have 5 years of experience."                    True
"Please proceed."                                  True
```

A notice period, a years-of-experience figure, a date — any number below the budget — is read as "I accept your salary offer."

**Consequences, all at once:**
1. **HR approval is bypassed.** The agent moves the candidate to interview without HR ever clicking anything.
2. **The DB is falsified.** `answers = {**answers, "expected_salary": budget_max, "accepted_budget": True}` overwrites the candidate's real expectation (₹10,00,000 becomes whatever the budget is). The dashboard then shows a salary the candidate never agreed to.
3. **HR loses the control.** The dashboard's "Approve For Interview" button only renders when `application_status == 'hr_escalated'` (`recruiter_dashboard.py:3177`). Once the agent auto-resumes, the button disappears — while HR still holds an email in their inbox saying *"HR approval required"*. They click through and there is nothing to click.
4. **Double emails** if HR does act in the window: the agent sends "here is your interview link" via `reply_interview_availability_request`, and HR's approval sends a second "Interview link" email via `send_interview_request_after_hr_approval`.

### 4.4 Defect 3 🟠 — and it misses almost every *real* acceptance

The same function, tested on realistic phrasings:

```
"Yes, I agree with the budget."          True     <-- only because "i agree" is a literal substring
"I am fine with the offered budget."     False
"Okay, I accept the budget you mentioned." False
"That works for me."                     False
"Yes I am ready to work in that package." False
"Budget is acceptable to me."            False
"Fine with me, please move ahead."       False
"I can adjust with your budget."         False
"Yes"                                    False
```

**8 of 9 miss.** So the *common* case — candidate genuinely accepts the budget before HR acts — falls through to `:5037`:

```python
if existing_status == "hr_escalated":
    self.notify_manual_hr_review(..., event_type="hr_escalated_candidate_reply_handoff", mark_application=False)
    return True
```

`notify_manual_hr_review` (`:4020-4059`) emails HR and logs `"no_candidate_reply_sent": True`. **The candidate receives nothing.** They said yes and got silence.

Worse, `hr_escalated_candidate_reply_handoff` is **not** in the 72-hour suppression list at `:4734-4742`:

```python
["manual_hr_review_requested", "final_status_handoff_to_hr",
 "repeated_missing_cv_handoff_to_hr", "repeated_unclear_query_handoff_to_hr"]
```

so every follow-up from that candidate fires **another** "Manual HR review needed" email. Candidate: silence. HR: one email per candidate reply.

### 4.5 Defect 4 🟠 — escalation runs two conversations at once

`escalate_to_hr` (`:4285-4305`) ends with:

```python
send_hr_notification(self.mailer, subject, body)
self.db.update_application_screening(application_id, "hr_escalated", answers, reason)
...
self.reply_negotiate_screening(inbox_email, application, issues)   # <-- also emails the candidate
```

It asks HR to approve **and** simultaneously invites the candidate to negotiate. Two parallel tracks on one application, with no lock between them. Whichever lands first silently determines the outcome. This is the actual source of the race — the escalation never pauses candidate-facing automation.

### 4.6 Defect 5 🔴 — negotiation has no exit when the issue isn't salary

`screening_fit` (`:1321-1330`) returns two kinds of issue: budget, and work terms. But the escape hatch to HR (`:4939-4948`) only fires on the budget one:

```python
if (current_status == "screening_negotiation"
        and expected_salary is not None
        and budget_max is not None
        and expected_salary > budget_max):
    self.escalate_to_hr(...)
    return True

self.db.update_application_screening(..., "screening_negotiation", {**answers, "issues": issues})
self.reply_negotiate_screening(inbox_email, active_application, issues)   # <-- falls here forever
```

**Proof:**

```
screening_fit({'comfortable_with_terms': False, 'expected_salary': 500000}, budget_max=1000000)
  -> (False, ['candidate is not comfortable with the shift/office terms'])
```

Not fit, salary under budget → the escalation branch is skipped → status is re-set to `screening_negotiation` and `reply_negotiate_screening` sends again. **Every reply re-enters the same branch. There is no exit.**

The prompt at `:4243` literally says *"negotiate politely once"* — the intent was always one-shot. Nothing enforces it, because the state machine re-enters the same state.

This also caused the repeat in `shrikanthps87@gmail.com`'s thread. He replied **"Proceed further."**, which the detector reads as non-acceptance:

```
"Proceed further."   accepts=False      <-- "please proceed" is a phrase; "proceed further" is not
"Yes"                accepts=False
"ok proceed"         accepts=False
```

so the agent re-disclosed the same budget it had already disclosed 5 hours earlier:

```
01:43:19 OUT  "...I wanted to be transparent with... the budget..."
06:39:33 IN   "Proceed further."
06:40:35 OUT  "I wanted to have an open conversation about compensation for this role.
               The budget we have for the US Full Cycle Accountant position is between..."
```

### 4.7 The flow you want — disclose once, then decide

> *"AI agent should ask one time letting candidate know what is budget. If candidate agrees then good, otherwise escalate to HR."*

That is the right rule and it removes the entire negotiation state. Here is the concrete spec.

#### State

Add to `screening_details` (jsonb — no migration needed):

```jsonc
{
  "budget_disclosed_at":   "2026-08-12T06:40:35+05:30",  // set once, never overwritten
  "budget_response":       "accepts | rejects | counter | unclear",
  "budget_counter_amount": 1500000,                       // if they countered
  "accepted_budget":       true                           // only on explicit acceptance
}
```

Replace the `screening_negotiation` status with **`budget_disclosed`**. Delete `screening_negotiation` from the status vocabulary once migrated.

#### Transitions

```
screening answers complete
  │
  ├─ screening_fit == True ─────────────────► interview_link_pending → send link (once)
  │
  └─ screening_fit == False
       │
       ├─ issue is budget AND budget_disclosed_at is NULL
       │      → status = 'budget_disclosed'
       │      → set budget_disclosed_at = now()
       │      → reply_budget_disclosure()      ◄── THE ONE EMAIL
       │
       └─ anything else  (work terms / timeline / budget already disclosed)
              → escalate_to_hr()               ◄── hold, no further candidate automation


reply arrives while status == 'budget_disclosed'
  │
  ├─ classify_budget_response(latest_reply)   ◄── LLM, not regex
  │
  ├─ "accepts"  → accepted_budget = true
  │               → interview_link_pending → send link (once)
  │
  ├─ "rejects"  → escalate_to_hr(reason="candidate declined the stated budget")
  │  "counter"  → escalate_to_hr(reason=f"counter offer: {amount}")
  │
  └─ "unclear"  → first time only: one short clarification ("just to confirm — does that
                  range work for you?"); second unclear → escalate_to_hr()
```

**Invariant: `reply_budget_disclosure` can fire at most once per application, ever.** Enforced twice — by `budget_disclosed_at IS NULL` in the transition, and by the reply ledger (§5.3) refusing a repeat of the `budget_disclosure` scenario. Belt and braces, because this is the exact failure the candidates saw.

#### The one email

`reply_budget_disclosure` states the number plainly and asks a closed question — no open-ended "let us know your thoughts", which is what invites the ambiguous replies the current detector then mishandles:

```
Hi,

Thanks for sharing your details.

For this role the approved range is INR 6,00,000 – 12,00,000 per annum.
I know that's below the figure you mentioned, so I wanted to be upfront
before we go further.

Could you let me know if that works for you? A simple yes or no is fine.
If it doesn't, tell me what you had in mind and I'll see what I can do.

Regards,
```

The closing line does the same job as naming an escalation — it makes "no" a real option with a real consequence — while staying entirely in the sender's own voice. The handoff happens silently on our side. See §5.4: the agent must never refer to a team it is signing as.

#### The classifier — replaces `latest_reply_accepts_budget` entirely

Delete the function at `:1332-1369`. The number-scan heuristic is unfixable; "90 days notice" is indistinguishable from a salary figure without understanding the sentence. Replace with an LLM call scoped to one question:

```python
@traceable(name="classify_budget_response")
def classify_budget_response(self, latest_reply: str, requirement: dict) -> dict:
    return self.json_call(f"""
Return only valid JSON.
The candidate was told the salary range for this role and asked whether it works for them.
Classify ONLY their answer to that question. Ignore notice periods, dates,
years of experience, and any other numbers that are not a salary.

  "accepts"  - they agree to the stated range
  "rejects"  - they decline it
  "counter"  - they propose a different figure
  "unclear"  - anything else, including no direct answer

JSON schema:
{{"response": "accepts|rejects|counter|unclear",
  "counter_amount": null,
  "evidence": "the exact words that decided it"}}

Stated range: {requirement_budget_text(requirement)}

Candidate's reply:
{latest_reply_text(latest_reply)[:1500]}
""")
```

Pass **only the latest reply**, not the thread — this is a single-question classification and thread context is what corrupts it today. Store `evidence` on the application so HR can see why the agent decided what it did.

Sanity-checking the classifier against the real replies that broke the old one: *"Proceed further."* → `accepts`; *"My notice period is 90 days."* → `unclear` (no salary answer); *"I am fine with the offered budget."* → `accepts`; *"I can adjust with your budget."* → `accepts`. All four are wrong today.

#### Never falsify the record

Current code does `answers = {**answers, "expected_salary": budget_max, "accepted_budget": True}` (`:4834`, `:4921`) — it overwrites what the candidate actually said with the budget ceiling. The dashboard then shows a salary expectation the candidate never stated.

Keep `screening_expected_salary` as the candidate's real figure, always. Acceptance is a separate boolean plus, if relevant, an `agreed_salary` column of its own.

### 4.8 Escalation must be a hold, not a branch

Whichever route reaches it, `escalate_to_hr` has to stop candidate-facing automation:

```python
HUMAN_HOLD_STATUSES = {"hr_escalated", "manual_hr_review", "interview_on_hold_hr_review"}

# very top of process_email, before any scenario dispatch:
if (active_application or {}).get("application_status", "").lower() in HUMAN_HOLD_STATUSES:
    self.db.record_candidate_reply(active_application["id"], inbox_email)   # store, don't act
    self.notify_hr_once(active_application, inbox_email)                    # rate-limited
    self.reply_holding_once(inbox_email, active_application)                # ONE short ack, ever
    return True
```

1. **One decision-maker.** While escalated, the agent does not negotiate, does not advance status, does not re-extract. It stores the reply and stops.
2. **Remove the `reply_negotiate_screening` call from the end of `escalate_to_hr`** (`:4305`). Asking HR to approve *and* inviting the candidate to negotiate is the race itself.
3. **One holding reply, once** — *"Thanks — I've passed this to our HR team and they'll come back to you shortly."* Then silence until HR acts. Not total silence (current behaviour), not a negotiation (also current behaviour).
4. **Rate-limit the HR notification** to one email per application per 24h; append later replies to the dashboard record instead of mailing each one.
5. **Never auto-resume from an escalated state.** Delete the `candidate_accepted_escalated_budget` bypass (`:4826-4832`). If the candidate accepts after escalation, surface it to HR in the dashboard — *"Candidate replied: 'I am fine with the offered budget' → looks like acceptance. [Approve] [Reject]"* — and let HR click.
6. **Make HR's controls state-independent.** Approve/reject should render for any application with an open escalation record, not only when `application_status` happens to equal `'hr_escalated'` (`recruiter_dashboard.py:3177`).

### 4.9 What this deletes

The one-shot rule is a net **removal** of code, which is why it's worth doing properly rather than patching:

| Delete | Lines | Why |
|---|---|---|
| `latest_reply_accepts_budget` | `:1332-1369` | Unfixable number heuristic; replaced by the classifier |
| `reply_negotiate_screening` | `:4237-4249` | Replaced by one-shot `reply_budget_disclosure` |
| `screening_negotiation` status + its branches | `:4914-4957` | Collapses into `budget_disclosed` + escalate |
| `candidate_accepted_escalated_budget` bypass | `:4826-4832` | Escalation becomes a hold |

Net effect: **one disclosure, one classified answer, two possible outcomes.** No state can be re-entered, so no loop is reachable.

---

## PART 4 — When *not* to reply

## 5. The agent has no concept of "this needs no answer"

Right now every inbound email that reaches a scenario branch produces an outbound email. There is no path that reads a message and decides to stay quiet.

### 5.1 Acknowledgements get answered

Live, `akhilreddy4447@gmail.com`:

```
07:16:50 OUT  interview link sent
07:19:37 IN   "Thank you for inviting me to interview for the position.
               I appreciate the opportunity and look forward to speaking with you."
07:20:15 OUT  "The link has already been sent to you and is still valid..."     <-- resend
07:21:34 IN   "Thank you, I will do that."
07:22:14 OUT  "I see it hasn't been completed yet, so here is the link again:"  <-- resend
```

Two courtesy notes, two link resends. The candidate cannot end the exchange except by going silent.

The trigger is `is_status_followup` (`:1502-1515`), which matches the bare word `interview`:

```python
patterns = [ r"\bstatus\b", r"\bupdate\b", r"\bfollow up\b", ...,
             r"\binterview\b", r"\bnext step\b", ... ]
```

Any email containing "interview" — including one *thanking you for the interview invitation* — is classified as a status enquiry.

### 5.2 Fix — an explicit no-reply path

Add a first-class outcome alongside the scenario branches:

```python
ACK_ONLY = {"thanks", "thank you", "noted", "sure", "ok", "okay", "will do",
            "got it", "received", "great", "perfect", "looking forward"}

def is_pure_acknowledgement(latest: str) -> bool:
    text = normalize_position_text(latest_reply_text(latest))
    if len(text.split()) > 25:
        return False
    if "?" in latest:
        return False
    stripped = " ".join(w for w in text.split() if w not in {"hi","hello","regards","team","hr","i","you","for","the","and"})
    return bool(stripped) and any(p in text for p in ACK_ONLY) and not _contains_new_information(text)
```

Then in `process_email`, before scenario dispatch: if the reply is a pure acknowledgement **and** the application status hasn't changed since the last outbound, log `acknowledgement_no_reply` and return without sending.

Tighten `is_status_followup` in the same pass: drop `\binterview\b` and `\bupdate\b` as standalone triggers, require interrogative phrasing (`any update`, `what is the status`, `when can i expect`, `heard back`) or a question mark.

### 5.3 The backstop that makes all of this safe

Even with perfect intent detection, add an **outbound reply ledger**:

```sql
CREATE TABLE recruiter_sent_replies (
    id BIGSERIAL PRIMARY KEY,
    application_id BIGINT NOT NULL,
    scenario TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    provider_message_id TEXT,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON recruiter_sent_replies (application_id, scenario, sent_at DESC);
```

Rule: **never send the same scenario to the same application twice within 24 hours.** On the second attempt, escalate to a human instead of emailing the candidate.

This is the single highest-value change in this report. It caps the damage of *every* bug listed here — including ones not yet found. Every incident in this audit would have been stopped at the second message.

### 5.4 Persona consistency — the agent must speak *as* the sender

The mailbox signs every reply as **"HR Team, Virtual Admins"** (`RECRUITER_SIGNATURE_NAME=HR Team`). So any sentence referring to "our team", "the team", or "our HR team" in the third person is a contradiction: the sender is announcing they will forward the message to themselves. That reads as an intermediary — a bot, an outsourced screener, or an assistant — which is exactly the tell to avoid.

**This is already leaking into production.** Scanning the real sent messages in the mailbox:

```
"…a few required details are still missing before I can share your profile
 with the team for review. Could you please confirm the following: …"
```

### Where it comes from

It is not the model improvising — it is instructed. `draft_reply`'s rule list (`recruiter_agent.py:3708`):

```python
- If the facts say application received, acknowledge receipt and say the team will review it.
```

and reinforced by the facts dicts and fallback bodies:

| Location | Copy |
|---|---|
| `:4167` | `{"application_received": True, "next_step": "internal review"}` |
| `:4439` | "…**our team** will contact you with the next steps." |
| `:4514` | "It is still under review, and **our team** will contact you if your profile is shortlisted." |
| `:6533` | "We are still reviewing **internally** and will update you soon…" |

Note `:4167` — `"internal review"` is passed in as a *fact*, so the model is being handed the word "internal" and invited to use it. Facts dicts are candidate-visible in effect; they need the same scrutiny as the copy.

### The rules to encode

Replace `:3708` and add to the rule list in `draft_reply`:

```python
- You ARE the HR team. Write in first person singular ("I") or first person
  plural ("we"). Never refer to "the team", "our team", "our HR team",
  "the recruiter", or "the concerned department" as a third party — you are them.
- Never say a message will be forwarded, passed on, escalated, shared internally,
  reviewed internally, or sent to anyone else. From the reader's side, decisions
  happen here.
- Never mention systems, tools, automation, scores, matching, records, or databases.
- Do not say "in our records", "in our system", or "our system shows".
```

Rewrite the fallbacks in the same voice:

| Before | After |
|---|---|
| "our team will contact you with the next steps" | "I'll come back to you with the next steps" |
| "It is still under review, and our team will contact you if your profile is shortlisted" | "I'm still reviewing this and will come back to you shortly" |
| "We are still reviewing internally and will update you soon" | "I'm still going through this and will update you soon" |
| "before I can share your profile with the team for review" | "before I can take this forward" |
| `{"next_step": "internal review"}` | `{"next_step": "under review"}` |

One legitimate exception: `:6069` — *"the final round with our HR Manager"* — is fine. A named individual who genuinely is a different person is not a persona break; a vague "team" that the signature claims to be is.

### Add a leak check, not just a prompt rule

Prompt rules are probabilistic; the production leak above happened *despite* "Do not sound automated". Add a deterministic post-filter in `clean_reply_body` (`:3740`) alongside the existing signature stripping:

```python
PERSONA_LEAKS = [
    r"\b(our|the)\s+(hr\s+)?team\b", r"\bpass(ing|ed)?\s+(this|your|it)\s+(on|to)\b",
    r"\bforward(ing|ed)?\s+(this|your|it)\s+to\b", r"\bescalat(e|ing|ed)\b",
    r"\binternal(ly)?\b", r"\bour system\b", r"\bin our records\b",
    r"\bAI\b", r"\bautomated\b", r"\bbot\b",
]
```

On a hit, log `persona_leak_detected` with the matched phrase and fall back to the hand-written body rather than sending the generated one. That gives you a metric for how often the model drifts, instead of finding out from a candidate.

### One thing worth checking with whoever owns compliance

Email tone is ordinary — a shared HR mailbox writing in one consistent voice is standard practice and nothing here changes that. The narrower question is the **AI voice interview**, which is a different artefact: several jurisdictions have specific disclosure duties for automated tools in hiring (EU AI Act Art. 50, Illinois AIVIA, Maryland, NYC LL144). If you hire only into Mohali for domestic roles, most of those don't reach you; if any pipeline touches candidates or clients in those regions, it's worth a look. Flagging the scope, not the answer — it doesn't affect any of the copy changes above.

Worth noting operationally too: on 11 Aug a colleague replying from this same mailbox told a candidate *"this was an AI you were interacting with to check the emotional factor of the candidate"* (§7.3). Whatever line you settle on, the humans sharing the mailbox need to be briefed on it, or the persona gets broken from the inside regardless of what the code does.

---

## PART 5 — The architecture it needs

## 6. Reply from state, not from re-reading the mailbox

You described the intent exactly: *"read email and reply as per that keeping in mind past replies and what is saved in DB."* That is not what the code does today.

**Today:** every inbound email triggers a full re-derivation of the world from raw email text. `classify_email`, `extract_screening_answers`, and `draft_reply` each re-read the thread and re-infer everything. The DB row is written to, but is barely read back as authority. When the thread view is wrong (§2), the derived state is wrong, and the agent contradicts its own records.

Evidence of this: at 06:04 the agent wrote *"I can confirm we've received it"* about the CV, then in a new thread at 09:43 told the same candidate *"the CV you attached is for an Accounts Payable Analyst role"* — because a new `conversationId` created a second application with no memory of the first (`recruiter_applications_message_attachment_idx` at `:552` only dedupes on `(email_message_id, attachment_sha256)`, so a new thread from the same person for the same role starts from zero).

**It should be:** the `recruiter_applications` row is the source of truth. An inbound email is a **delta** applied to it.

```
inbound email
   ↓
resolve application   ← by (thread_id) OR (candidate_email + requirement_id), open-status window
   ↓
load state            ← screening_details, application_status, sent-reply history
   ↓
extract delta         ← ONLY what is new in latest_reply_text(this message)
   ↓
merge into state      ← never discard a known value (merge_screening_answers already does this)
   ↓
state machine         ← one handler per status; decides next action, may decide "do nothing"
   ↓
reply ledger check    ← §5.3 — refuse duplicate scenario
   ↓
send + record transition atomically
```

Three concrete consequences:

1. **`extract_screening_answers` should be given the stored `screening_details` plus only the newest message**, not the whole thread. It currently re-derives everything from a truncated thread (`:3941-3973`), so a value already in the DB gets "un-learned" the moment it falls outside the window. `merge_screening_answers` (`:1295`) is already write-safe — the bug is that the extractor is asked to re-find facts the DB already holds.

2. **Split `process_email`.** It is **1,131 lines** (`:4614-5745`) of nested branching in a **7,260-line** file. That is the direct reason these bugs survived: the screening path and the interview path cannot be reasoned about or tested independently. One handler per status, each a testable function of `(application_state, inbound_delta) → (new_state, action)`.

3. **Dedupe applications on `(candidate_email, requirement_id)`** with an open-status window, and attach new threads to the existing application. `application_has_saved_cv()` already exists (`:1778`) — apply it at intake, not only in the follow-up branch. This alone stops the "asks for CV again in a new thread" case (`aaqib1408@` and `aroraishika923@` both have two conversations for one role).

---

## PART 6 — Remaining findings

## 7. Other issues

### 7.1 🟢 Remove the SQL Server path entirely

Since you're on Postgres only: the dual-dialect layer is pure liability. `RecruiterDatabase.sql()` (`:2159-2183`) does **string rewriting** of every query to translate `%s` → `?`, `NOW()` → `SYSDATETIMEOFFSET()`, `::jsonb` → nothing, and `LIMIT n` → `TOP n` via regex. That is fragile and, more importantly, it forces every query in the codebase to be written to the lowest common denominator.

I confirmed the SQL Server schema at `db59244` is missing ~24 columns the code requires (`email_thread_id`, `screening_details`, `interview_link_token`, `interview_report`, …) — a basic `latest_application_for_thread()` fails there with `Invalid column name 'email_thread_id'`, because the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations at `:559-588` are Postgres-only. The path is already dead in practice.

**Delete:** the `mssql` branch of `__init__`, `sql()`, `apply_mssql_limit()`, `normalize_mssql_connection_string()`, `detect_mssql_driver()`, `is_mssql()`, the MSSQL DDL block (`:620-760`), the `pyodbc` dependency, `scripts/install_mssql_odbc_ubuntu.sh`, and the `MSSQL_*` config in `config.py` / `.env`. Then use real Postgres features — `jsonb` operators, `ON CONFLICT`, `RETURNING` — which make the reply ledger and message ledger clean to implement.

### 7.2 🟠 No dedupe on processed message ids

`process_one_graph_message()` (`:5779`) processes unconditionally. `fetch_message_by_id()` (`:3213`) selects `isRead` and never reads it. Graph webhooks are **at-least-once**, and `isRead` is only set ~50s after processing begins (LangSmith shows `process_recruiting_email` at 47–58s).

**Proof:** `09:42:34` and `09:43:02` — two *different* replies, 28s apart, to one inbound message. Also `10:32:58` / `10:33:21`.

`dict.fromkeys()` in `process_notifications` dedupes only within a single HTTP POST; `schedule_retry` (`:6829`) adds another reprocessing path.

**Fix:**

```sql
CREATE TABLE IF NOT EXISTS recruiter_processed_messages (
    provider_message_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Claim with `INSERT ... ON CONFLICT DO NOTHING` and bail if zero rows inserted — atomic, and survives restarts unlike the in-process `self.lock`.

### 7.3 🟠 No human-takeover latch

`himans8285@gmail.com`:

```
11:36:10 OUT (human)  "Himanshu please be calm this was an AI you were interacting with...
                       @Pragati Pradhan will get in touch with you soon"
11:39:21 OUT (agent)  "Just to complete the screening, could you please confirm that you are..."
```

The existing guards (`is_final_agent_status` `:326`, `recent_event_count` `:4734`) only trigger on states the *agent* set. A human replying from the shared mailbox sets nothing.

**Fix:** record every agent-sent `internetMessageId` (the reply ledger in §5.3 gives you this). Before replying, check the last outbound message in the conversation — if it isn't one of ours, latch the application to `human_handled` and stop permanently.

### 7.4 🟠 A binary PDF is serialized into LLM prompts

`extract_screening_answers` (`:3965`) and `reply_interview_availability_request` (`:4264`) both do `json.dumps(application, default=str)`. `application` comes from `application_with_requirement()` (`:2337`), which selects `ra.*` — including **`attachment_payload BYTEA`**, the raw CV file — plus `rc.raw_cv_text`. With `default=str` the BLOB becomes a `b'%PDF-1.7\x0a...'` repr and is sent to DeepSeek.

Cost, context pressure, degraded extraction, and an unintended export of CV bytes to the LLM provider. **Fix:** whitelist fields at every prompt call site.

### 7.5 🟡 NUL bytes crash the insert

Production, via LangSmith: `DataError('PostgreSQL text fields cannot contain NUL (0x00) bytes')` at `recruiter_agent.py:5526`. `extract_pdf_text()` can emit `\x00`; nothing sanitizes before `insert_candidate` (`:2677`). The exception aborts `process_email` *after* replies may already have gone out; the message is left unread and reprocessed, re-sending them.

**Fix:** `cv_text = cv_text.replace("\x00", "")` in `extract_cv_text()` (`:1990`).

### 7.6 🟡 Poller starvation in `--watch` mode

`process_email` returns `False` for non-employment mail (`:4967`); `run_once` (`:5745`) then calls `mark_unseen`. Those emails stay unread forever, and `fetch_unseen` takes `$top=RECRUITER_POLL_LIMIT` (10). Once 10 ignored emails accumulate they permanently fill the window and **no new candidate email is ever processed**. Latent today (production is webhook-only) but it makes `--watch` unusable as a fallback. The message ledger in §7.2 removes the dependency on the unread flag as queue state.

### 7.7 🟢 Interview recordings

OneDrive `AI Recruiter Interview Recordings` holds 4 applications:

```
Application 21  3 files (20.0 / 21.7 / 28.9 MB)   <- interview taken 3 times, no cap
Application 73  1 file  (37.7 MB)
Application 89  1 file  (0.06 MB)                 <- seconds long; aborted, will still be scored
Application 91  1 file  (26.8 MB)
```

Add an attempt cap and a minimum-duration check before generating a report. Also revisit `RECRUITER_INTERVIEW_QUESTION_COUNT=2` as the basis for `RECRUITER_INTERVIEW_PASS_SCORE=35`.

### 7.8 🟠 Secrets

`.env` is correctly gitignored and absent from history, but holds live plaintext credentials for Microsoft Graph, DeepSeek, LangSmith, Gmail, and the dashboard login. The Graph app credential grants **application-level mailbox and OneDrive access across the tenant** — that file alone let me read the entire mailbox and every interview recording. Move to AWS Secrets Manager / SSM and rotate anything previously shared.

Also stale/broken config: `GMAIL_*`, `RECRUITER_IMAP_*`, `RECRUITER_EMAIL_PASSWORD` no longer apply under `MAIL_PROVIDER=microsoft_graph`; `.env:9` reads `OICE_MAX_UTTERANCE_SECONDS` (missing `V`), so `VOICE_MAX_UTTERANCE_SECONDS` silently uses its default.

### 7.9 🟠 Maintainability

- `recruiter_agent.py` — 7,260 lines / 302 KB; `process_email` alone is 1,131 lines.
- Three dead snapshots: `recruiter_agent copy.py`, `copy 2.py`, `copy 3.py` (~137 KB). Git has the history; delete them.
- **Zero tests.** Every bug here is one unit test away: `roles_are_compatible('US Bookkeeper', 'Accounts Payable Analyst', cv)`, `latest_reply_accepts_budget('My notice period is 90 days', req)`, `build_thread_context()` tail retention, `is_status_followup('thank you for inviting me to interview')`.
- Every branch repeats a `log_json` + `trace_recruiter_event` + `db.log_email_event` triple with hand-copied payloads — hundreds of near-duplicate lines, and three places to drift out of sync.
- `README.md` still describes a "local RAG agent over company_policy.pdf" and never mentions the recruiter.

---

---

## PART 7 — The AI voice interview

## 8. What application 91 actually shows

I downloaded `application-91-Rachael Pershad-20260811-174024.webm` from OneDrive and transcribed it. It reproduces exactly what you described, and the cause is a pair of thresholds that contradict each other.

### 8.1 The annotated timeline

| Time | Speaker | What was said |
|---|---|---|
| 00:19 | Agent | *"How did you identify and qualify prospects while at AT&T?"* |
| 00:26–00:52 | Candidate | Real answer — BANT, budget/authority/need/timeline, worked example |
| 00:53–01:00 | — | **7.7s of dead air** |
| 01:01 | Agent | *"Give me a moment, I am reviewing that."* (filler) |
| 01:08 | Agent | Follow-up: *"Could you give a specific example of how you qualified a prospect's budget and authority…?"* |
| 01:15 | **Candidate** | **"Could you please repeat the question?"** |
| 01:20–01:44 | Candidate | *"So, according to me, your question is, could you give me a specific example of how you qualified prospect's budget and authority in a sales conversation, right?"* |
| 01:44 | Agent | *"Thanks, I am just reviewing your response."* ← **treated her repeat request as the answer** |
| 01:49 | **Candidate** | **"I am answering the question actually."** ← protest, spoken over the agent |
| 01:54 | Agent | *"Thanks for explaining."* |
| 01:57 | Agent | Moves to next question — **she never answered the follow-up at all** |
| 02:02 | Candidate | *"So, your question is, what strategies did you use to close the deal with US clients, right? I'm answering the question. Give me just one minute. I am thinking, right now, to answer your question."* |
| 02:17–02:47 | Candidate | Real answer — ROI framing, objection handling, urgency, follow-up |
| 02:47 | Agent | *"Give me a moment. I am reviewing that."* |
| 02:52–03:34 | — | **~40s of near-silence before the closing** |
| 03:34 | Agent | *"That completes our interview for today."* |

She was scored on a 3.9-minute interview in which she answered **two** questions, was talked past once, and spent 47% of the session in silence.

### 8.2 Root cause — a repeat request is only deliverable in a 3-word window

Two thresholds, one on each side of the wire, contradict each other.

**Client** (`recruiter_dashboard.py:3861-3867`) will not submit an answer until it has **≥ 10 words**:

```javascript
function answerCanAutoSubmit() {
  const words = answerWordCount();
  if (interviewPhase === 'greeting') return words >= 1;
  if (answerHasExplicitCompletion()) return true;
  if (answerLooksIncomplete()) return false;
  return words >= 10;                    // <-- floor
}
```

**Server** (`recruiter_dashboard.py:1094-1105`) will not honour a repeat request above **12 words**:

```python
repeat_requested = any(phrase in lowered for phrase in repeat_phrases)
mostly_repeat_request = word_count <= 12 or re.search(r"(repeat|come again|say that again|can you repeat)\??$", lowered)
if repeat_requested and mostly_repeat_request:
    return {"action": "repeat", ...}     # <-- ceiling
```

**Proof** — running both predicates against her actual words:

```
words=6   client_autosubmits=False  repeat_word=True  mostly=True   -> REPEAT
   "Could you please repeat the question?"

words=34  client_autosubmits=True   repeat_word=True  mostly=False  -> sent to LLM as an ANSWER
   "Could you please repeat the question? So according to me your question is,
    could you give me a specific example of how you qualified prospect's budget
    and authority in a sales conversation, right?"

words=15  client_autosubmits=True   repeat_word=True  mostly=False  -> sent to LLM as an ANSWER
   "Sorry, I could not hear you properly, could you repeat the question once more please?"

Usable window: [10, 11, 12] words.
```

Her repeat request was **6 words** — below the client floor. So nothing was sent, the mic stayed open, and she did what anyone does in silence: kept talking. She repeated the question back to confirm she'd understood it. That pushed the buffer to 34 words — above the server ceiling. The `repeat` branch was skipped and the whole string went to the LLM as her answer.

The LLM then saw a restatement of its own question presented as an answer, and did the only sensible thing with it: acknowledged and moved on.

**A polite, natural repeat request is structurally impossible to deliver.** "Can you repeat?" (3 words) and "Sorry, I could not hear you properly, could you repeat the question once more please?" (15 words) both fail. Only a 10–12 word request works.

### 8.3 Her protest was never heard — there is no barge-in

At 01:49 she says *"I am answering the question actually."* It is in the recording. It never reached the transcript.

The mic only opens in the `onend` callback of the agent's audio (`recruiter_dashboard.py:4373`, `:4607`):

```javascript
speak(currentQuestion || '', () => beginListening());
```

While the agent is speaking, `recognition` is not running. Anything the candidate says over it is lost — captured by the always-on `MediaRecorder` for the recording, but never transcribed, never submitted, never seen by the model.

The barge-in settings in `.env` (`VOICE_BARGE_IN_VERIFY_SECONDS`, `VOICE_BARGE_IN_CONFIDENCE`, `VOICE_BARGE_IN_MIN_WORDS`, `VOICE_BARGE_IN_COOLDOWN_SECONDS`) apply to `voice_agent.py`, the local CLI interviewer — **not** to the browser interview candidates actually use. The browser path has no interruption handling at all.

This is why the interview feels like talking to a wall: the candidate can hear herself being ignored, and the system genuinely cannot hear her.

### 8.4 The lag, measured

VAD analysis of the 236-second recording (webrtcvad, aggressiveness 2, 30ms frames):

```
duration : 236.3s (3.9 min)
speech   : 124.2s (52.6%)
silence  : 112.1s (47.4%)

gaps >= 2s : 7   totalling 30s
gaps >= 3s : 4   totalling 23s
gaps >= 5s : 2   totalling 15s

longest:  00:52.95 -> 7.65s      01:37.92 -> 3.72s
          03:35.43 -> 7.50s      03:23.82 -> 2.37s
          03:08.34 -> 4.08s      02:50.40 -> 2.31s
```

**Nearly half the interview is dead air.** On turn 1, the gap from the candidate finishing (00:52.9) to the next question (01:08) was **15 seconds**, of which the first 7.7s had no sound at all.

The latency chain per turn, from the constants at `recruiter_dashboard.py:3616-3620`:

| Stage | Cost | Source |
|---|---|---|
| Silence before submit | 2200ms (3200ms if ≥45 words) | `ANSWER_SILENCE_MS` / `LONG_ANSWER_SILENCE_MS` |
| Final-transcript grace | 1400ms | `FINAL_TRANSCRIPT_GRACE_MS` |
| Auto-advance poll granularity | up to 1400ms | `scheduleAutoAdvance` interval |
| `web_interview_turn_decision` LLM | **seconds** | prompt carries `job_description` 5000 + `cv_text` 7000 + full transcript, every turn |
| Filler nudge, then wait for it to finish | 2000ms + the nudge's own duration | `PROCESSING_NUDGE_MS`, then `:4599` `afterCurrentSpeech(deliverResponse)` |
| edge-tts round trip | **seconds** | `ensure_dashboard_speech_file` — cache miss on every unique utterance |

Four issues stack here:

1. **~5s of deliberate waiting before the request is even sent.** 2200 + 1400 + up to 1400ms of poll granularity.
2. **The turn LLM re-sends the entire CV and JD on every single turn** (`:1140-1150`). LangSmith shows comparable calls at 30s+. This is the dominant cost and it is almost entirely redundant — the CV does not change between turns.
3. **The filler nudge actively delays the real answer.** At `:4599`, if the nudge has started, the real response waits for it to finish speaking: `if (processingNudgeSpoken) afterCurrentSpeech(deliverResponse);`. A 2.5s filler that begins at 2.0s pushes the real reply to at least 4.5s even if the LLM answered at 2.1s.
4. **TTS is cached by exact text hash** (`:1418`), so fixed strings ("Let us begin", the nudges) hit cache, but every LLM-generated question and acknowledgement is a **guaranteed cache miss** — a full edge-tts network round trip on the critical path.

### 8.5 Speech-to-text quality is corrupting the scores

This is the most damaging finding in this section, because it produces confidently wrong hiring decisions.

From application 20's stored `interview_report` transcript — a Python developer interview:

| Transcribed as | Candidate almost certainly said |
|---|---|
| "one **DJ** project" / "used in the **Jungle**" | Django |
| "**post gracious girl**" | PostgreSQL |
| "we can use **red is**" | Redis |
| "converter CV into **Jason**" | JSON |
| "fetch **Tata** from database" | data |
| "we can verify from **looks**" | logs |
| "connect **over you I** with my database" | ORM |

The candidate named the correct technologies. The transcript mangled them. Then the LLM judge scored the *transcript*:

```
score 4/10 — "Candidate could not clearly articulate project architecture or design choices."
              "Described a CRM project but answer was disjointed."
score 3/10 — "Answer lacked coherence and technical depth."
              "explanation was unclear and off-topic"
```

**The candidate is being marked down for the transcription's incoherence, not their own.** Someone who correctly explained Django ORM, PostgreSQL, Redis caching and JSON parsing scored 3–4/10 for being "unclear". That is a false negative that looks, in the dashboard, like a well-reasoned rejection.

Note also `WHISPER_MODEL=tiny.en` in config — the smallest, least accurate model. It is used by `voice_agent.py`; the browser path uses the Chrome Web Speech API, whose accent handling on Indian-English technical vocabulary is visibly poor here. Either way the pipeline has no domain vocabulary hinting.

### 8.6 The agent's own questions leak into candidate answers

Same report, first transcript entry ends:

```
"...so that these and go can  you tell w"
```

The beginning of the interviewer's *next* question is glued onto the end of the candidate's answer. The stored answer text is corrupted with agent speech, and that corrupted text is what gets scored.

### 8.7 Fixes

**Turn-taking**

1. **Delete the word-count thresholds on both sides.** Detect intent, not length. A short utterance is not an incomplete answer — it is usually a question.
2. **Classify every candidate turn before treating it as an answer** — one cheap call, or a local rule set, returning `answer | repeat_request | thinking | clarification | skip`. Add an explicit **question-echo check**: if the utterance is ≥70% similar to the question just asked, it is not an answer.
3. **Let short utterances submit.** Replace `words >= 10` with: submit on silence ≥ threshold regardless of length, and let the server decide what the utterance was.
4. **Recognise thinking-aloud.** "Give me one minute", "I am thinking", "let me think" should extend the silence window, not advance the turn. `INCOMPLETE_ANSWER_SILENCE_MS = 7000` exists for this but is gated behind `answerLooksIncomplete()`, which only checks trailing conjunctions.
5. **Implement barge-in.** Keep recognition running during TTS; on confident candidate speech, stop the audio and listen. Without this the candidate has no way to interrupt a wrong turn — which is precisely what failed here at 01:49.

**Latency**

6. **Stop re-sending the CV and JD every turn.** Build the question list once at `api_interview_start`, cache the role/CV summary in the session, and send the turn LLM only the current question + the latest answer + a short running summary. This is the single biggest win.
7. **Pre-generate TTS for the next question** while the candidate is still answering — the question list is known at session start, so every main question can be synthesised and cached before it is needed.
8. **Drop the filler-nudge blocking behaviour** at `:4599`. If the real response is ready, speak it; never wait for a filler to finish.
9. **Cut `ANSWER_SILENCE_MS` to ~1200ms** and rely on intent classification rather than long timers to decide whether the candidate has finished.

**Scoring integrity**

10. **Never score a transcript the system knows is unreliable.** Capture a per-utterance confidence score; below a threshold, flag the interview `needs_human_review` instead of producing a number.
11. **Add domain vocabulary hinting** (`SpeechRecognition.grammars`, or move to a server-side Whisper `small`/`medium` with an `initial_prompt` seeded from the JD's technology list). `tiny.en` is not adequate for technical interviews.
12. **Instruct the judge to ignore transcription artefacts.** The scoring prompt should be told the text is machine-transcribed, that phonetic mangling of technical terms is expected, and that it must score *substance*, not fluency.
13. **Strip agent speech from answer text** before storing — retain the last question asked and remove any suffix matching it.
14. **Re-score the completed interviews.** Applications 20, 21, 73, 89 and 91 were all scored from transcripts produced by this pipeline; at minimum 91 (talked past on one question) and 20 (technical terms destroyed) should be reviewed by a human before any decision stands.

**Session integrity**

15. **Cap interview attempts.** Application 21 has three recordings; nothing prevents repeats.
16. **Reject unusable recordings.** Application 89 is 64 KB — seconds long — and will still be scored as a completed interview.
17. **Revisit `RECRUITER_INTERVIEW_QUESTION_COUNT=2`.** Two questions plus follow-ups is a thin basis for `RECRUITER_INTERVIEW_PASS_SCORE=35`, especially when one of the two can be lost to the bug in §8.2.

---

## 9. Fix order

### Phase 1 — stop the bleeding (hours, ~30 lines)

1. `$orderby: receivedDateTime desc` + raise limit to 25 in `fetch_thread_messages`. **(§2.4)**
2. `build_thread_context` keeps the **tail**; apply `latest_reply_text()` per message. **(§2.4)**
3. `mark_interview_link_sent()` — explicit status transition on every link send; remove the status write from `ensure_interview_link`. **(§4.2)**
4. Disable the auto-resume from `hr_escalated` — delete the `candidate_accepted_escalated_budget` bypass. **(§4.3)**
5. Make the non-salary `screening_fit` failure escalate instead of re-entering `screening_negotiation`. **(§4.6)**
6. Add `bookkeeper` + variants to the accounting family *as a stopgap only*, and make `roles_are_compatible` non-fatal. **(§3)**
7. Strip NUL bytes in `extract_cv_text`. **(§7.5)**

### Phase 2 — make loops structurally impossible (days)

8. **The one-shot budget flow.** Add `budget_disclosed_at` / `budget_response` to `screening_details`; replace `screening_negotiation` with `budget_disclosed`; add `reply_budget_disclosure` and `classify_budget_response`; delete `latest_reply_accepts_budget` and `reply_negotiate_screening`. **(§4.7, §4.9)**
9. **Outbound reply ledger + 24h per-scenario rate limit.** Highest value item in this report, and the second guard on the one-shot budget rule. **(§5.3)**
10. Escalation becomes a hold: remove the candidate reply from `escalate_to_hr`, one holding ack, rate-limited HR notification. **(§4.8)**
11. `recruiter_processed_messages` ledger, claimed atomically. **(§7.2)**
12. Acknowledgement detection → no-reply path; tighten `is_status_followup`. **(§5.2)**
13. Human-takeover latch. **(§7.3)**
14. Whitelist prompt fields; stop serializing `attachment_payload`. **(§7.4)**
15. Dedupe applications on `(candidate_email, requirement_id)`. **(§6)**
16. Make HR's approve/reject controls state-independent. **(§4.8)**
17. **Persona consistency:** rewrite the `draft_reply` rule at `:3708`, fix the four fallback bodies, and add the `PERSONA_LEAKS` post-filter with a `persona_leak_detected` metric. **(§5.4)**

### Phase 2b — the voice interview (days, parallel track)

25. **Remove the word-count gates on both sides** and classify each turn as `answer | repeat_request | thinking | clarification | skip`, with an explicit question-echo similarity check. **(§8.7 items 1–4)**
26. **Implement barge-in** — keep recognition live during TTS. **(§8.7 item 5)**
27. **Stop re-sending CV + JD on every turn**; cache the session context and send only the current question, latest answer, and a running summary. Biggest latency win. **(§8.7 item 6)**
28. Pre-generate TTS for upcoming questions; stop the filler nudge from blocking the real reply; cut `ANSWER_SILENCE_MS` to ~1200ms. **(§8.7 items 7–9)**
29. **Scoring integrity:** capture STT confidence and flag low-confidence interviews `needs_human_review`; add domain vocabulary hinting; tell the judge the text is machine-transcribed and to score substance not fluency; strip agent speech from stored answers. **(§8.7 items 10–13)**
30. Re-review applications 91 and 20 by hand. **(§8.7 item 14)**
31. Cap interview attempts; reject sub-threshold recordings; revisit `RECRUITER_INTERVIEW_QUESTION_COUNT=2`. **(§8.7 items 15–17)**

### Phase 3 — the right architecture (weeks)

25. **Delete the static role vocabulary.** Promote `match_requirement` to authority; feed it full `job_description`; make role mismatch a score, never a rejection. **(§3.3)**
26. Make `job_description` a required structured field in the dashboard so HR owns role knowledge. **(§3.3)**
27. Optional pgvector layer for requirement/CV matching at scale. **(§3.3)**
28. Split `process_email` into a per-status state machine; reply from stored state + delta. **(§6)**
29. Regression tests from the cases in this report, including a persona-leak corpus and the turn-taking cases in §8.2.
30. Remove the SQL Server path; use real Postgres features. **(§7.1)**
31. Delete `copy*.py`; rewrite README; move secrets to a manager. **(§7.8, §7.9)**
32. Alerting: >3 agent replies in one thread within 1h, any `processing_failed`, any scenario sent twice to one candidate, any `persona_leak_detected`.

---

## 10. A note on the framing

The agent's **writing** is good — read the replies in isolation and they're warm, varied, and human. That part works. The problem is that well-written emails are being generated from a stale, truncated view, and then sent by a state machine that has no memory of what it already said and no way to decide to stay quiet.

Three of the four loops (§2, §4.2, §5.1) are **state-management bugs with no AI content whatsoever**. Better prompts or a larger model would not touch them.

The urgent part is reputational. Candidates received a dozen near-identical requests for information they had already supplied; one worked out he was talking to a machine and said so in writing; another had to be calmed down by a human. Phase 1 plus item 7 (the reply ledger) would have prevented every incident documented here.

---

## Appendix — evidence index

| Claim | Source |
|---|---|
| Graph `$top` returns oldest-first | Live Graph, conversation `…g8fKFPF4jAY=`, `$top=5` vs `$top=10` |
| Thread has 24 messages, agent saw 10 | Live Graph, unpaged count vs `$top=10` |
| 8000-char cut drops messages 7–10 | `build_thread_context` reproduced verbatim on live bodies |
| Bookkeeper CV rejected | Live attachment `Md_Aaqib_US_Bookkeeper_Resume.pdf` through the real function |
| "90 days notice" ⇒ budget accepted | `latest_reply_accepts_budget` executed, `budget_max=1000000` |
| 8/9 real acceptances missed | Same function, realistic phrasings |
| Negotiation has no exit on a terms issue | `screening_fit({'comfortable_with_terms': False, 'expected_salary': 500000})` → `(False, [terms])`, escalation branch skipped |
| Budget re-disclosed after "Proceed further." | Mailbox, `shrikanthps87@gmail.com`, `01:43:19` and `06:40:35`; `accepts=False` for that reply |
| Persona leak in production copy | Mailbox: "before I can share your profile with the team for review"; instructed at `recruiter_agent.py:3708` |
| HR approval → `interview_time_requested` | `mark_hr_approved_for_interview` `:2565` + `ensure_interview_link` `:2404` early return |
| Duplicate replies 28s apart | Mailbox, `2026-08-12T09:42:34Z` / `09:43:02Z` |
| Interview-link resend loop | Mailbox, `akhilreddy4447@gmail.com`, `07:19–07:22` |
| Agent replied after human takeover | Mailbox, `himans8285@gmail.com`, `11:36–11:39` |
| NUL-byte crash; 47–58s processing | LangSmith `Voice RAG Agent` |
| SQL Server schema drift | `INFORMATION_SCHEMA.COLUMNS` on `db59244` |
| Recording anomalies | Graph OneDrive listing |
| App 91: repeat request scored as an answer | OneDrive recording transcribed; timeline at §8.1 |
| Repeat request deliverable only at 10–12 words | Both predicates executed against her actual utterances |
| 47.4% silence, 7.65s and 7.50s dead gaps | webrtcvad analysis of `application-91-…webm` (236.3s) |
| Candidate protest lost | Present in recording at 01:49, absent from transcript; mic gated at `recruiter_dashboard.py:4373`, `:4607` |
| STT corrupting scores | App 20 stored `interview_report`: "post gracious girl", "red is", "DJ project"; judge notes "lacked coherence" |
