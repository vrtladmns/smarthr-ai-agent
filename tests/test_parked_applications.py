"""Thirteen applications sat in interview_on_hold_hr_review, never interviewed.

Traced on the server: apps 88, 92, 138, 148, 165, 167, 169, 173, 174, 175, 180,
181, 183 - all interview_report '{}', interview_started_at NULL,
interview_completed_at NULL, interview_attempts 0. Their hr_escalation_reason
text is from near-miss handoffs, experience shortfalls and a budget counter,
none of which is a post-interview state.

Only two writers set application_status and nothing else, and the dashboard
dropdown is one of them. The dashboard then lists these as "AI interview report
needs HR approval or rejection" with a Decide button, and both decisions send an
email about an interview that never took place.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra

AGENT = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
DASH = Path(__file__).resolve().parent.parent.joinpath("recruiter_dashboard.py").read_text()


# --- did the interview happen -------------------------------------------------

def test_an_empty_report_is_not_an_interview():
    assert ra.interview_actually_happened({"interview_report": {}, "interview_completed_at": None}) is False
    assert ra.interview_actually_happened({"interview_report": "{}"}) is False
    assert ra.interview_actually_happened({}) is False


def test_a_real_interview_is_recognised():
    assert ra.interview_actually_happened({"interview_completed_at": "2026-08-27T10:00:00Z"}) is True
    assert ra.interview_actually_happened({"interview_report": {"overall_score": 32.5}}) is True


# --- the emails must not describe an interview that never happened ------------

def test_the_rejection_does_not_thank_them_for_an_interview_they_never_sat():
    body = AGENT[AGENT.index("def send_interview_rejection") :]
    body = body[: body.index("\ndef ", 10)]
    assert "interview_actually_happened(application)" in body
    alt = body[body.index("else:", body.index("interview_actually_happened")) :]
    assert "taking the time to interview" not in alt
    assert "the conversation" not in alt
    assert "Having reviewed your application" in alt


def test_nobody_is_congratulated_on_a_round_they_never_sat():
    body = AGENT[AGENT.index("def send_final_hr_round_request") :]
    body = body[: body.index("\ndef ", 10)]
    assert "if not interview_actually_happened(application):" in body
    guard = body[body.index("if not interview_actually_happened") :]
    guard = guard[: guard.index("elif")]
    assert "cleared the AI technical interview round" not in guard
    assert "reviewed your application" in guard


# --- how they got there, and leaving a trace next time ------------------------

def test_a_hand_set_status_is_recorded():
    body = DASH[DASH.index("def update_application_status") :]
    body = body[: body.index("\n    def ", 10)]
    assert "dashboard_status_changed" in body, "the only untraceable status writer must leave a trace"
    assert '"from"' in body and '"to"' in body, "record what it was before, not just after"


def test_the_status_post_is_checked_against_the_dropdown():
    """The list existed only to render the <select>; the POST took anything."""
    body = DASH[DASH.index("def update_application_status") :]
    body = body[: body.index("\n    def ", 10)]
    assert "self.application_status_values()" in body
    assert "if new_status not in allowed" in body


def test_only_the_expected_writers_set_status_alone():
    """Every other writer also stamps a timestamp, which is what made the
    thirteen traceable at all: their hr_escalated_at did not match the status
    they ended up in. If another bare writer appears, it gets noticed here."""
    import re
    bare = []
    for source, name in ((AGENT, "agent"), (DASH, "dashboard")):
        for m in re.finditer(
            r"UPDATE recruiter_applications\s+SET application_status = %s\s*\n\s*WHERE", source
        ):
            fn = None
            for fm in re.finditer(r"^ {0,4}def (\w+)", source[: m.start()], re.M):
                fn = fm.group(1)
            bare.append(f"{name}.{fn}")
    assert bare == ["dashboard.update_application_status"], (
        "the dashboard dropdown is the only writer that changes status and nothing "
        f"else; a new one appeared: {bare}"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
