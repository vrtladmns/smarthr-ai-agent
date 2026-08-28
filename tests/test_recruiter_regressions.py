"""Regression tests for the bugs found in the 2026-08-12 audit.

Each test name maps to a section of AGENT_AUDIT.md. Run with:

    ./venv/bin/python -m pytest tests/ -q
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import recruiter_agent as ra


class FakeThreadMessage:
    def __init__(self, sender, body, received_at, subject="Re: Role", message_id=""):
        self.sender = sender
        self.body = body
        self.received_at = received_at
        self.subject = subject
        self.message_id = message_id
        self.uid = b"1"


class FakeInboxEmail:
    def __init__(self, thread_messages, sender="c@example.com", body="", subject="Role"):
        self.thread_messages = thread_messages
        self.sender = sender
        self.body = body
        self.subject = subject
        self.uid = b"1"
        self.message_id = ""
        self.in_reply_to = ""
        self.references = ""
        self.gmail_thread_id = ""
        self.received_at = None
        self.attachments = []


# --- §2.2 thread context must keep the NEWEST messages ------------------------

def test_thread_context_keeps_newest_messages():
    base = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    messages = [
        FakeThreadMessage("c@example.com", f"MARKER{i} " + ("filler word " * 400), base + timedelta(minutes=i))
        for i in range(10)
    ]
    context = ra.build_thread_context(FakeInboxEmail(messages))
    assert "MARKER9" in context, "newest message must survive truncation"
    assert "MARKER8" in context
    assert "MARKER0" not in context, "oldest message should be dropped first"


def test_thread_context_strips_quoted_history():
    base = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)
    body = "My actual answer.\n\nOn Wed, Aug 12, 2026 at 10:56 AM Career <career@x.org> wrote:\n> old noise " * 1
    body += "\n> " + ("quoted junk " * 300)
    context = ra.build_thread_context(FakeInboxEmail([FakeThreadMessage("c@example.com", body, base)]))
    assert "My actual answer." in context
    assert "quoted junk" not in context, "quoted history must be stripped per message"


# --- §3 role matching must not hard-reject ------------------------------------

def test_bookkeeper_cv_is_not_rejected():
    cv = (
        "Md Aaqib. Accounts Payable professional with 10+ years including invoice "
        "processing, 3-way matching, bookkeeping and general ledger reconciliation."
    )
    assert ra.roles_are_compatible("US Bookkeeper", "Accounts Payable Analyst", cv) is True


def test_role_families_recognise_bookkeeper():
    assert "accounting" in ra.role_families_from_text("US Bookkeeper")
    assert "accounting" in ra.role_families_from_text("Senior Bookkeeper")


def test_generic_words_do_not_count_toward_role_overlap():
    # "us" must not be the token that decides compatibility
    assert ra.meaningful_role_tokens("US Bookkeeper") == {"bookkeeper"}


# --- §4 budget: no false positives from stray numbers -------------------------

def test_notice_period_is_not_budget_acceptance():
    req = {"budget_max": 1000000, "currency": "INR"}
    for text in [
        "My notice period is 90 days.",
        "I need 30 days notice period.",
        "I have 5 years of experience.",
    ]:
        assert ra.deterministic_budget_signal(text, req) != "accepts", text


def test_clear_budget_rejection_is_detected():
    req = {"budget_max": 1000000, "currency": "INR"}
    assert ra.deterministic_budget_signal("No, that is too low. I cannot go below 15 LPA.", req) == "rejects"


# --- §4.6 screening_fit failures must always have an exit ---------------------

def test_terms_mismatch_is_not_a_budget_issue():
    req = {"budget_max": 1000000}
    is_fit, issues = ra.screening_fit({"comfortable_with_terms": False, "expected_salary": 500000}, req)
    assert is_fit is False
    assert ra.screening_issue_kind(issues) == "terms", "terms failure must be routed to HR, not re-negotiated"


def test_budget_issue_is_classified_as_budget():
    req = {"budget_max": 1000000}
    is_fit, issues = ra.screening_fit({"comfortable_with_terms": True, "expected_salary": 1500000}, req)
    assert is_fit is False
    assert ra.screening_issue_kind(issues) == "budget"


# --- §5.1/§5.2 acknowledgements and status follow-ups -------------------------

def test_thanks_for_the_interview_invite_is_not_a_status_request():
    text = (
        "Thank you for inviting me to interview for the position. I appreciate the "
        "opportunity and look forward to speaking with you."
    )
    assert ra.is_status_followup(text) is False


def test_real_status_request_is_detected():
    for text in [
        "Any update on my application?",
        "Could you please share the status of my application?",
        "When can I expect to hear back?",
    ]:
        assert ra.is_status_followup(text) is True, text


def test_pure_acknowledgements_get_no_reply():
    for text in ["Thank you, I will do that.", "Thanks for the update", "Noted, thanks.", "Sure, will do."]:
        assert ra.is_pure_acknowledgement(text) is True, text


def test_acknowledgement_with_a_question_still_gets_a_reply():
    assert ra.is_pure_acknowledgement("Thanks. Could you tell me the office address?") is False


def test_acknowledgement_with_new_information_still_gets_a_reply():
    assert ra.is_pure_acknowledgement("Thanks. My expected salary is 12 LPA and I can join in 30 days.") is False


# --- §5.4 persona consistency -------------------------------------------------

def test_persona_leaks_are_detected():
    for body in [
        "Hi,\n\nI can share your profile with the team for review.",
        "Hi,\n\nWe are still reviewing internally and will update you soon.",
        "Hi,\n\nI will pass this on to our HR team.",
        "Hi,\n\nI have escalated your case.",
    ]:
        assert ra.persona_leak(body), body


def test_clean_reply_has_no_persona_leak():
    body = "Hi,\n\nThanks for sharing the details. I'll come back to you with the next steps shortly."
    assert ra.persona_leak(body) is None


def test_named_hr_manager_is_allowed():
    body = "Hi,\n\nWe would like to move ahead with the final round with our HR Manager."
    assert ra.persona_leak(body) is None


# --- §7.5 NUL bytes -----------------------------------------------------------

def test_nul_bytes_are_stripped_from_cv_text():
    assert "\x00" not in ra.sanitize_db_text("hello\x00world")
    assert ra.sanitize_db_text("hello\x00world") == "helloworld"


# --- §7.4 prompt payloads must not carry blobs --------------------------------

def test_prompt_facts_exclude_binary_and_raw_cv():
    application = {
        "id": 1,
        "requirement_position": "US Bookkeeper",
        "budget_max": 1000000,
        "attachment_payload": b"%PDF-1.7 binary junk",
        "raw_cv_text": "x" * 50000,
        "screening_details": {"current_salary": 500000},
    }
    facts = ra.application_prompt_facts(application)
    assert "attachment_payload" not in facts
    assert "raw_cv_text" not in facts
    assert facts["requirement_position"] == "US Bookkeeper"
    assert facts["screening_details"] == {"current_salary": 500000}




# --- LLM values reaching NUMERIC columns (production failure 2026-08-12) -------

def test_experience_with_plus_sign_is_coerced():
    """`"10+"` aborted the whole transaction on insert."""
    assert ra.numeric_value("10+") == 10.0


def test_common_llm_number_formats_are_coerced():
    cases = {
        "10+": 10.0,
        "85%": 85.0,
        "10-12 years": 10.0,
        "6 LPA": 6.0,
        "1,200,000": 1200000.0,
        " 7.5 ": 7.5,
        12: 12.0,
        3.5: 3.5,
    }
    for raw, expected in cases.items():
        assert ra.numeric_value(raw) == expected, raw


def test_non_numeric_values_become_null_not_garbage():
    for raw in [None, "", "N/A", "null", "Immediate", "fresher", "-", True, False]:
        assert ra.numeric_value(raw) is None, raw


def test_normalize_cv_details_coerces_experience():
    out = ra.normalize_cv_details({"total_experience_years": "10+ years"})
    assert out["total_experience_years"] == 10.0


def test_normalize_evaluation_coerces_scores():
    out = ra.normalize_evaluation({"ats_score": "85%", "jd_match_score": "70 percent"})
    assert out["ats_score"] == 85.0
    assert out["jd_match_score"] == 70.0




# --- near-miss routing (production 2026-08-12: CFO vs US Bookkeeper) ----------

OPEN_REQUIREMENTS = [
    {"id": 1, "position_title": "Business Development Executive",
     "job_description": "sales pipeline, lead generation, client acquisition"},
    {"id": 2, "position_title": "US Bookkeeper",
     "job_description": "QuickBooks, bank reconciliation, AP/AR, month end close"},
    {"id": 3, "position_title": "US Tax Preparer",
     "job_description": "1040 1065 1120 preparation"},
]


def test_accounting_candidate_is_a_near_miss_not_a_rejection():
    """Tushar Gandhi: Fractional CFO, ATS 87, got a flat 'no openings'."""
    hits = ra.near_miss_requirements(
        OPEN_REQUIREMENTS,
        {"primary_role": "Fractional CFO", "role_family": "accounting", "confidence": 0.95},
        {"current_title": "Fractional Accountant / CFO", "skills": ["US GAAP", "QuickBooks"]},
        "10+ years in accounting, month end close, multi-entity consolidation",
    )
    assert [r["position_title"] for r in hits] == ["US Bookkeeper"]


def test_unrelated_candidate_is_not_a_near_miss():
    hits = ra.near_miss_requirements(
        OPEN_REQUIREMENTS,
        {"primary_role": "UI/UX Designer", "role_family": "design", "confidence": 0.9},
        {"current_title": "Product Designer", "skills": ["Figma", "wireframes"]},
        "designed mobile app interfaces and design systems",
    )
    assert hits == []


def test_near_miss_needs_a_known_family():
    assert ra.near_miss_requirements(OPEN_REQUIREMENTS, {}, {}, "") == []


def test_near_miss_is_empty_without_open_requirements():
    assert ra.near_miss_requirements([], {"role_family": "accounting"}, {}, "") == []




# --- matching on evidence, not job titles (production 2026-08-12) -------------

MATCH_REQUIREMENTS = [
    {"id": 1, "position_title": "Business Development Executive",
     "job_description": "sales pipeline, lead generation, client acquisition"},
    {"id": 2, "position_title": "US Bookkeeper",
     "job_description": "Minimum 5 years of US bookkeeping. 5 years on QuickBooks Online. "
                        "Bank reconciliation, AP/AR, month end close."},
    {"id": 3, "position_title": "US Tax Preparer",
     "job_description": "Preparation of 1040, 1065 and 1120 returns for US clients."},
]

TUSHAR_EXTRACTED = {
    "target_position": "Fractional CFO",
    "current_title": "Fractional Accountant / CFO",
    "skills": ["US GAAP & Audit Readiness", "Multi-Entity Consolidation", "QuickBooks", "NetSuite"],
}
TUSHAR_CV = (
    "Fractional CFO & US Bookkeeping | Strategic Finance. QuickBooks, NetSuite, "
    "Multi-Entity Accounting. Execute full-cycle bookkeeping, month-end close and "
    "bank reconciliation for US-based SMBs and CPA firms."
)


def test_bookkeeping_evidence_matches_even_when_the_title_says_cfo():
    """Tushar Gandhi was told 'no openings' despite a CV headed 'US Bookkeeping'."""
    match = ra.deterministic_requirement_match(
        TUSHAR_EXTRACTED, {"detected_position": "US Accounting Manager"},
        MATCH_REQUIREMENTS, source_text=TUSHAR_CV,
    )
    assert match["requirement_id"] == 2, match


def test_unrelated_candidates_still_do_not_match():
    for extracted, cls, cv in [
        ({"target_position": "UI/UX Designer", "current_title": "Product Designer", "skills": ["Figma"]},
         {"detected_position": "UI/UX Designer"},
         "Product designer. Figma, wireframes, prototyping, design systems."),
        ({"target_position": "Python Developer", "current_title": "Backend Engineer", "skills": ["Django"]},
         {"detected_position": "Python Developer"},
         "Python developer. Django, FastAPI, PostgreSQL, REST APIs, Docker."),
    ]:
        match = ra.deterministic_requirement_match(extracted, cls, MATCH_REQUIREMENTS, source_text=cv)
        assert match["requirement_id"] is None, (cls, match)


def test_exact_title_match_outranks_evidence_match():
    match = ra.deterministic_requirement_match(
        {"target_position": "US Bookkeeper", "current_title": "Bookkeeper", "skills": ["QuickBooks Online"]},
        {"detected_position": "US Bookkeeper"},
        MATCH_REQUIREMENTS,
        source_text="US Bookkeeper with 6 years QuickBooks Online and month end close.",
    )
    assert match["requirement_id"] == 2
    assert match["confidence"] == 1.0


def test_partial_title_evidence_is_not_enough():
    """The CV mentions 'tax' but not 'preparer', so Tax Preparer must not win."""
    match = ra.deterministic_requirement_match(
        TUSHAR_EXTRACTED, {"detected_position": "US Accounting Manager"},
        [MATCH_REQUIREMENTS[2]], source_text=TUSHAR_CV + " Coordinated tax filings with external CPAs.",
    )
    assert match["requirement_id"] is None, match




# --- interview link must never precede screening (vrtldmns@gmail.com, 2026-08-12)

def test_unscreened_application_reports_every_missing_field():
    missing = ra.missing_screening_fields({"screening_details": {}})
    assert missing == [
        "confirmation of the shift/office work terms",
        "current salary",
        "expected salary",
        "current location",
        "joining time / notice period",
    ]


def test_partially_screened_application_is_still_blocked():
    missing = ra.missing_screening_fields(
        {"screening_details": {"current_salary": 600000, "expected_salary": 900000}}
    )
    assert "current location" in missing
    assert "joining time / notice period" in missing


def test_fully_screened_application_is_clear():
    assert ra.missing_screening_fields({
        "screening_details": {
            "comfortable_with_terms": True,
            "current_salary": 600000,
            "expected_salary": 900000,
            "current_location": "Mohali",
            "joining_days": 30,
        }
    }) == []


def test_missing_screening_handles_json_string_and_none():
    assert ra.missing_screening_fields(None)
    assert ra.missing_screening_fields({"screening_details": "{}"})




# --- letter-spaced PDF extraction (9591maheshmane@gmail.com, 2026-08-13) ------

LETTER_SPACED = (
    "S U M M A R Y\n"
    "C o m p a n y  N a m e  -  D e v c o n s  S o f t w a r e  S o l u t i o n  P v t .  L t d .\n"
    "P r o j e c t  n a m e  -  W e a l t h  M a n a g e m e n t  &  C l i e n t  O n b o a r d i n g  Q A"
)


def test_letter_spaced_pdf_text_is_repaired():
    repaired = ra.repair_letter_spaced_text(LETTER_SPACED)
    assert "SUMMARY" in repaired
    assert "Devcons Software Solution" in repaired
    assert "Wealth Management" in repaired
    assert "QA" in repaired


def test_normal_text_is_left_alone():
    normal = "SUMMARY\nQA Engineer with 4 years in manual and automation testing.\nSelenium, Java, TestNG."
    assert ra.repair_letter_spaced_text(normal) == normal


def test_short_lines_are_not_mistaken_for_letter_spacing():
    assert ra.repair_letter_spaced_text("A B C") == "A B C"


def test_generic_title_words_cannot_match_on_cv_evidence_alone():
    """A QA CV must not match Business Development Executive."""
    qa_cv = (
        "QA Engineer. Manual and automation testing, Selenium, TestNG, Jira. "
        "Tested a wealth management platform and its business rules."
    )
    match = ra.deterministic_requirement_match(
        {"target_position": "QA Engineer", "current_title": "QA Engineer", "skills": ["Selenium"]},
        {"detected_position": "QA Engineer"},
        MATCH_REQUIREMENTS + [{"id": 9, "position_title": "Business Development Executive",
                               "job_description": "sales pipeline, lead generation"}],
        source_text=qa_cv,
    )
    assert match["requirement_id"] is None, match


def test_distinctive_titles_still_match_on_evidence():
    """The weak-token filter must not undo the bookkeeping fix."""
    match = ra.deterministic_requirement_match(
        TUSHAR_EXTRACTED, {"detected_position": "US Accounting Manager"},
        MATCH_REQUIREMENTS, source_text=TUSHAR_CV,
    )
    assert match["requirement_id"] == 2




# --- HR controls must match the stage (application 105, 2026-08-13) ----------

def _pre_interview_approve_visible(row: dict) -> bool:
    """Mirrors the gate in recruiter_dashboard.application_detail."""
    status = (row.get("application_status") or "").lower()
    already_interviewed = (
        bool(row.get("interview_completed_at"))
        or bool(row.get("interview_started_at"))
        or status in ra.POST_INTERVIEW_STATUSES
    )
    awaiting = bool(row.get("hr_escalated_at")) and not row.get("hr_approved_at")
    decidable = {"hr_escalated", "budget_disclosed", "manual_hr_review", "human_handled"}
    return not already_interviewed and (awaiting or status in decidable)


def test_post_interview_hold_does_not_offer_approve_for_interview():
    """AI said hold; approving must not re-send the interview link."""
    row = {
        "application_status": "interview_on_hold_hr_review",
        "hr_escalated_at": "2026-08-13T09:00:00Z",   # set by mark_post_interview_outcome
        "hr_approved_at": None,
        "interview_completed_at": "2026-08-13T08:00:00Z",
    }
    assert _pre_interview_approve_visible(row) is False


def test_pre_interview_escalation_still_offers_approve_for_interview():
    row = {
        "application_status": "hr_escalated",
        "hr_escalated_at": "2026-08-13T09:00:00Z",
        "hr_approved_at": None,
        "interview_completed_at": None,
        "interview_started_at": None,
    }
    assert _pre_interview_approve_visible(row) is True


def test_started_interview_blocks_the_pre_interview_control():
    row = {
        "application_status": "hr_escalated",
        "hr_escalated_at": "2026-08-13T09:00:00Z",
        "hr_approved_at": None,
        "interview_started_at": "2026-08-13T08:00:00Z",
    }
    assert _pre_interview_approve_visible(row) is False


def test_hr_round_time_requested_can_resend_availability():
    """Editing the status by hand sends no email; the button must stay available."""
    assert "hr_round_time_requested" in ra.POST_INTERVIEW_DECISION_STATUSES
    assert "interview_on_hold_hr_review" in ra.POST_INTERVIEW_DECISION_STATUSES




# --- human takeover must fail safe (application 105, 2026-08-13) --------------

class FakeAgentDB:
    def __init__(self, known_ids):
        self._known = set(known_ids)

    def agent_sent_message_ids(self, limit=500):
        return self._known


def _takeover(thread, known_ids):
    agent = ra.AIRecruiterAgent.__new__(ra.AIRecruiterAgent)
    agent.db = FakeAgentDB(known_ids)
    messages = [
        FakeThreadMessage(sender, "body", None, message_id=mid) for sender, mid in thread
    ]
    return ra.AIRecruiterAgent.thread_taken_over_by_human(agent, FakeInboxEmail(messages))


MAILBOX = ra.MICROSOFT_MAILBOX or ra.RECRUITER_FROM_EMAIL


def test_unrecorded_agent_email_is_not_mistaken_for_a_human():
    """The 'Final HR round availability' email was sent outside the ledger and
    the agent then read its own message as a stranger's, abandoning candidate 105."""
    thread = [
        ("candidate@example.com", "<c1>"),
        (MAILBOX, "<agent-not-recorded>"),
        ("candidate@example.com", "<c2>"),
    ]
    assert _takeover(thread, known_ids=set()) is False


def test_real_human_reply_is_detected_when_the_ledger_has_coverage():
    thread = [
        ("candidate@example.com", "<c1>"),
        (MAILBOX, "<agent-1>"),
        ("candidate@example.com", "<c2>"),
        (MAILBOX, "<typed-by-a-person>"),
    ]
    assert _takeover(thread, known_ids={"<agent-1>"}) is True


def test_agents_own_latest_message_is_not_a_takeover():
    thread = [("candidate@example.com", "<c1>"), (MAILBOX, "<agent-1>")]
    assert _takeover(thread, known_ids={"<agent-1>"}) is False


def test_thread_with_no_outbound_mail_is_not_a_takeover():
    assert _takeover([("candidate@example.com", "<c1>")], known_ids={"<agent-1>"}) is False




# --- matching must reach a JD, not a human (mailbox audit 2026-08-19) ---------

FIELD_REQUIREMENTS = [
    {"id": 1, "position_title": "Business Development Executive",
     "job_description": "B2B sales pipeline, lead generation, talent for closing deals."},
    {"id": 2, "position_title": "US Bookkeeper",
     "job_description": "5 years US bookkeeping, QuickBooks Online, payroll processing, financial statements."},
    {"id": 3, "position_title": "US Tax Preparer",
     "job_description": "2-7 years US CPA firm, 1040, 1120S, tax software."},
]


def test_requirement_field_comes_from_the_title_not_the_prose():
    """'payroll' and 'talent' in a JD made unrelated roles look adjacent."""
    assert ra.requirement_role_families(FIELD_REQUIREMENTS[1]) == {"accounting"}
    assert ra.requirement_role_families(FIELD_REQUIREMENTS[0]) == {"sales"}
    assert ra.requirement_role_families(FIELD_REQUIREMENTS[2]) == {"tax"}


def test_accounting_titles_reach_the_bookkeeper_jd():
    """None of these shares a token with 'US Bookkeeper', so all went to HR."""
    for title in [
        "Senior Accountant",
        "Accounts Executive",
        "Sr. Accounts Associate",
        "Sr. US Accountant & Payroll Executive",
        "Junior Accountant",
    ]:
        sole = ra.single_family_requirement(
            FIELD_REQUIREMENTS,
            {"primary_role": title, "role_family": "accounting"},
            {"current_title": title, "skills": []},
            "general ledger, AP/AR, reconciliation, month end close",
        )
        assert sole and sole["position_title"] == "US Bookkeeper", title


def test_unrelated_fields_are_still_not_routed():
    for title, family in [
        ("Talent Acquisition Lead", "hr"),
        ("QA Engineer", "software"),
        ("UI/UX Designer", "design"),
    ]:
        assert ra.single_family_requirement(
            FIELD_REQUIREMENTS,
            {"primary_role": title, "role_family": family},
            {"current_title": title, "skills": []},
            "",
        ) is None, title


def test_a_recruiter_is_not_told_bookkeeping_is_the_same_area():
    hits = ra.near_miss_requirements(
        FIELD_REQUIREMENTS,
        {"primary_role": "Talent Acquisition Lead", "role_family": "hr"},
        {"current_title": "Talent Acquisition Lead", "skills": ["sourcing"]},
        "talent acquisition, sourcing, interviews, onboarding",
    )
    assert hits == []


def test_declared_field_wins_when_a_title_reads_as_two():
    """'Payroll Executive' also reads as HR; accounting is what they do."""
    sole = ra.single_family_requirement(
        FIELD_REQUIREMENTS + [{"id": 9, "position_title": "HR Manager", "job_description": "employee relations"}],
        {"primary_role": "Sr. US Accountant & Payroll Executive", "role_family": "accounting"},
        {"current_title": "Sr. US Accountant & Payroll Executive", "skills": []},
        "US accounting and payroll, QuickBooks",
    )
    assert sole and sole["position_title"] == "US Bookkeeper"




# --- stated experience minimums must be enforced (2026-08-20) ----------------

BDE_2Y = {
    "id": 1, "position_title": "Business Development Executive", "experience_min_years": 2,
    "job_description": "B2B sales. Minimum 2 years field sales experience.",
}
GOOD_SCORES = {"ats_score": 75, "jd_match_score": 55, "recommendation": "shortlist"}


def test_fresher_is_rejected_against_a_stated_minimum():
    """Scores alone would have let an MBA fresher through to screening."""
    extracted = ra.normalize_cv_details({"total_experience_years": 0})
    assert ra.experience_shortfall(BDE_2Y, extracted)
    assert ra.passes_screening_threshold(ra.normalize_evaluation(GOOD_SCORES), BDE_2Y, extracted) is False


def test_the_rejection_reason_names_the_gap():
    extracted = ra.normalize_cv_details({"total_experience_years": 0})
    reason = ra.jd_rejection_reason(ra.normalize_evaluation(GOOD_SCORES), BDE_2Y, extracted)
    assert "minimum of 2" in reason and "experience" in reason.lower()


def test_meeting_the_minimum_still_passes():
    extracted = ra.normalize_cv_details({"total_experience_years": 3})
    assert ra.experience_shortfall(BDE_2Y, extracted) is None
    assert ra.passes_screening_threshold(ra.normalize_evaluation(GOOD_SCORES), BDE_2Y, extracted) is True


def test_a_near_miss_on_years_is_within_tolerance():
    """CVs round their dates; 1.6 against 2 is noise, not a gap."""
    extracted = ra.normalize_cv_details({"total_experience_years": 1.6})
    assert ra.experience_shortfall(BDE_2Y, extracted) is None


def test_unknown_experience_is_not_treated_as_zero():
    """Rejecting on a missing field would discard perfectly good candidates."""
    extracted = ra.normalize_cv_details({})
    assert ra.experience_shortfall(BDE_2Y, extracted) is None
    assert ra.passes_screening_threshold(ra.normalize_evaluation(GOOD_SCORES), BDE_2Y, extracted) is True


def test_requirement_without_a_minimum_is_unaffected():
    extracted = ra.normalize_cv_details({"total_experience_years": 0})
    assert ra.experience_shortfall({"position_title": "Open Role"}, extracted) is None


def test_hr_candidate_matches_nothing_when_no_hr_role_is_open():
    """An HR fresher should get 'no opening', not a manual review queue."""
    reqs = [BDE_2Y,
            {"id": 2, "position_title": "US Bookkeeper", "job_description": "US bookkeeping, QuickBooks."},
            {"id": 3, "position_title": "US Tax Preparer", "job_description": "1040, 1120S."}]
    summary = {"primary_role": "HR / Recruitment", "role_family": "hr"}
    extracted = {"current_title": "HR Trainee", "skills": ["recruitment", "onboarding"]}
    cv = "Objective to start my career in Human Resource Management, recruitment, onboarding, employee engagement."
    assert ra.single_family_requirement(reqs, summary, extracted, cv) is None
    assert ra.near_miss_requirements(reqs, summary, extracted, cv) == []




# --- scheduling must read the reply, not the quoted headers (2026-08-20) -----

def test_quoted_email_headers_are_not_read_as_availability():
    """A final round was booked for 19 August 2027 off a quoted header."""
    assert ra.parse_interview_datetime_fallback(
        "Thanks.\n\nOn Wed, 19 Aug 2026 at 11:28 PM, Career <career@x.org> wrote:\nHi, ..."
    ) is None


def test_a_past_date_never_rolls_forward_a_year():
    """'19 August' the day after is a misread, not next August."""
    now = ra.recruiter_now()
    yesterday = now - ra.timedelta(days=1)
    text = f"Let's meet {yesterday.day} {yesterday.strftime('%B')} at 7pm"
    assert ra.parse_interview_datetime_fallback(text) is None


def test_weekday_availability_picks_the_soonest_day():
    """Must not depend on which day the suite happens to run."""
    text = "I will be available anytime on friday, monday and tuesday."
    slot = ra.parse_interview_datetime_fallback(text)
    assert slot is not None
    offered = {4, 0, 1}                      # Friday, Monday, Tuesday
    assert slot.weekday() in offered, slot
    now = ra.recruiter_now()
    # It is the soonest of the days offered, and a named day never means today.
    soonest = min(ra.next_weekday_date(day, now) for day in offered)
    assert slot.date() == soonest, (slot, soonest)
    assert 0 < (slot.date() - now.date()).days <= 7


def test_a_bare_weekday_still_yields_a_slot():
    slot = ra.parse_interview_datetime_fallback("I'm available Friday")
    assert slot is not None and slot.weekday() == 4


def test_weekday_with_a_time_keeps_the_time():
    slot = ra.parse_interview_datetime_fallback("I am free on Tuesday at 7 pm")
    assert slot is not None and slot.weekday() == 1 and slot.hour == 19


def test_no_slot_is_invented_from_a_plain_thank_you():
    assert ra.parse_interview_datetime_fallback("Thanks, looking forward to it.") is None


def test_a_slot_far_in_the_future_is_rejected():
    now = ra.recruiter_now()
    far = now + ra.timedelta(days=ra.MAX_SCHEDULE_DAYS_AHEAD + 30)
    text = f"Let's meet {far.day} {far.strftime('%B')} at 7pm"
    assert ra.parse_interview_datetime_fallback(text) is None




# --- the duplicate guard must not rewrite pipeline state (2026-08-20) --------

def test_duplicate_guard_does_not_mark_the_application():
    """A run that had just booked a Teams meeting was dragged back to
    manual_hr_review by the suppression branch, so the meeting existed and the
    candidate was never told."""
    import inspect

    source = inspect.getsource(ra.AIRecruiterAgent.send_candidate_reply)
    assert "notify_manual_hr_review" not in source, (
        "suppression must notify HR without rewriting application status"
    )
    assert "notify_hr_rate_limited" in source
    assert "mark_application" not in source




# --- joining answered as a date, not a number of days (2026-08-21) ----------

def test_a_joining_date_answers_the_joining_question():
    """Divya said "4 Sep"; only joining_days was accepted, so she was asked again."""
    answers = {
        "comfortable_with_terms": True, "current_salary": 444000, "expected_salary": 700000,
        "current_location": "Mohali", "joining_days": None, "joining_date": "4 Sep",
    }
    assert ra.screening_answers_complete(answers) is True
    assert ra.missing_screening_fields({"screening_details": answers}) == []


def test_joining_dates_in_various_shapes_are_understood():
    for text in ["4 sep", "2026-09-04", "3 or 4 Sep", "September 4", "after 31 August"]:
        assert ra.parse_joining_date(text) is not None, text


def test_joining_date_converts_to_days():
    answers = {"joining_date": ra.recruiter_now().date().isoformat()}
    assert ra.joining_days_from_answers(answers) == 0


def test_a_notice_period_in_days_still_works():
    answers = {
        "comfortable_with_terms": True, "current_salary": 444000, "expected_salary": 700000,
        "current_location": "Mohali", "joining_days": 15,
    }
    assert ra.screening_answers_complete(answers) is True


def test_screening_is_still_incomplete_when_joining_is_unknown():
    answers = {
        "comfortable_with_terms": True, "current_salary": 444000, "expected_salary": 700000,
        "current_location": "Mohali",
    }
    assert ra.screening_answers_complete(answers) is False
    assert "joining time / notice period" in ra.missing_screening_fields({"screening_details": answers})


def test_joining_time_is_not_listed_twice():
    missing = ra.missing_screening_fields({"screening_details": {}})
    assert len([m for m in missing if "joining" in m]) == 1


def test_a_scenario_may_be_sent_twice_before_suppression():
    """Suppressing the second message left candidates with total silence."""
    assert ra.REPLY_SCENARIO_MAX_PER_WINDOW >= 2
    assert "budget_disclosure" in ra.ONCE_PER_APPLICATION_SCENARIOS




# --- "send me a new link, I'll do it tomorrow" (application 135, 2026-08-21) --

def test_his_actual_message_is_treated_as_a_delay():
    """Rohit got a "your interview is still pending" reminder, then silence."""
    text = "send me new link i will give it tommorow"
    assert ra.is_interview_delay_reply(text) or ra.wants_a_fresh_interview_link(text)


def test_misspelled_tomorrow_and_other_verbs_are_understood():
    for text in [
        "I'll give the interview tomorrow",
        "I am busy today, will attempt tommorow",
        "I will attend on Monday",
        "tomorrow i will complete it",
        "I need some time",
    ]:
        assert ra.is_interview_delay_reply(text), text


def test_asking_for_the_link_again_is_recognised():
    for text in [
        "send me new link",
        "Please resend the link",
        "can you share the interview link again?",
    ]:
        assert ra.wants_a_fresh_interview_link(text), text


def test_unrelated_replies_are_not_treated_as_delays():
    for text in [
        "Thanks, I have completed the interview",
        "What is the status of my application?",
        "Thank you for inviting me to interview for the position.",
        "My notice period is 2 months",
    ]:
        assert not ra.is_interview_delay_reply(text), text
        assert not ra.wants_a_fresh_interview_link(text), text




# --- scanned CVs (parveenhaier / naikoo738, 2026-08-22) ---------------------

def test_pdf_extraction_falls_through_to_ocr(monkeypatch, tmp_path):
    """pypdf -> PyMuPDF -> OCR, each only when the previous found nothing."""
    calls = []
    monkeypatch.setattr(ra, "extract_pdf_text_pypdf", lambda p: calls.append("pypdf") or "")
    monkeypatch.setattr(ra, "extract_pdf_text_pymupdf", lambda p: calls.append("pymupdf") or "")
    monkeypatch.setattr(ra, "ocr_pdf_text", lambda p: calls.append("ocr") or "SCANNED TEXT")
    assert ra.extract_pdf_text(tmp_path / "x.pdf") == "SCANNED TEXT"
    assert calls == ["pypdf", "pymupdf", "ocr"]


def test_a_readable_pdf_never_reaches_ocr(monkeypatch, tmp_path):
    """OCR is slow; it must not run when the text layer is fine."""
    monkeypatch.setattr(ra, "extract_pdf_text_pypdf", lambda p: "real text")
    monkeypatch.setattr(ra, "ocr_pdf_text", lambda p: pytest.fail("OCR should not run"))
    assert ra.extract_pdf_text(tmp_path / "x.pdf") == "real text"


def test_ocr_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "OCR_ENABLED", False)
    assert ra.ocr_pdf_text(tmp_path / "x.pdf") == ""


def test_missing_ocr_tooling_degrades_to_unreadable(monkeypatch, tmp_path):
    """Without Tesseract the candidate still gets a clear explanation."""
    monkeypatch.setattr(ra, "extract_pdf_text_pypdf", lambda p: "")
    monkeypatch.setattr(ra, "extract_pdf_text_pymupdf", lambda p: "")
    monkeypatch.setattr(ra, "ocr_pdf_text", lambda p: "")
    assert ra.extract_pdf_text(tmp_path / "x.pdf") == ""




# --- external dashboard action queue (2026-08-24) ----------------------------

def test_every_dashboard_action_maps_to_a_real_function():
    """A SQL status change cannot send an email; these handlers can."""
    handlers = ra.agent_action_handlers()
    for name in [
        "approve_interview", "approve_hr_round", "reject_after_interview",
        "send_interview_link", "send_teams_link", "revoke_jd_rejection",
        "reevaluate", "select_after_hr_round", "reject_after_hr_round",
        "hold_after_hr_round", "reopen_interview",
    ]:
        assert name in handlers, name
        assert callable(handlers[name])


def test_ddl_splitter_handles_comments_and_function_bodies():
    """Splitting on every ';' cut a plpgsql body and a comment in half."""
    script = """
    CREATE TABLE a (id int);
    -- a comment; with a semicolon in it
    CREATE FUNCTION f() RETURNS TRIGGER AS $$
    BEGIN
        NEW.x := 1;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TABLE b (id int);
    """
    statements = ra.split_sql_statements(script)
    assert len(statements) == 3, statements
    assert statements[0].startswith("CREATE TABLE a")
    assert "RETURN NEW" in statements[1] and statements[1].startswith("CREATE FUNCTION")
    assert statements[2].startswith("CREATE TABLE b")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))


# --- LLM client must not wait forever (2026-08-29) ----------------------------

def test_chat_models_are_built_with_a_timeout():
    """The OpenAI client defaults to 600 seconds per attempt, plus retries."""
    import llm_factory
    assert llm_factory.LLM_TIMEOUT_SECONDS <= 120
    assert llm_factory.LLM_MAX_RETRIES <= 2
    import inspect
    params = inspect.signature(llm_factory.make_chat_model).parameters
    assert "timeout" in params and "max_retries" in params
