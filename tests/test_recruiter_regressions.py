"""Regression tests for the bugs found in the 2026-08-12 audit.

Each test name maps to a section of AGENT_AUDIT.md. Run with:

    ./venv/bin/python -m pytest tests/ -q
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
