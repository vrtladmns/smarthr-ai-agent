"""Integration tests for the AI voice interview HTTP API.

These drive the real handler over a real socket against the real database, with
only the LLM stubbed, so the turn state machine, session persistence and error
handling are exercised end to end.

Requires the local Postgres from docker-compose to be running.

    ./venv/bin/python -m pytest tests/test_interview_api.py -q
"""

import json
import socket
import time
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra
import recruiter_dashboard as rd


# --- LLM stub -----------------------------------------------------------------

class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """Returns canned JSON so turn decisions are deterministic."""

    next_action = {"action": "next_question", "reply": "Understood.", "question": "", "reason": "stub"}

    def invoke(self, prompt: str):
        if "Create" in prompt and "interview questions" in prompt:
            return FakeResponse(json.dumps({"questions": [
                "Tell me about your experience with QuickBooks Online.",
                "Describe how you prepare an S corporation return.",
                "How do you handle a client who disputes your figures?",
            ]}))
        if "Evaluate this browser voice interview" in prompt or "overall_score" in prompt:
            return FakeResponse(json.dumps({
                "overall_score": 60, "technical_score": 60, "communication_score": 60,
                "role_fit_score": 60, "recommendation": "hold", "summary": "stub",
                "plus_points": [], "negative_points": [], "question_reviews": [],
                "camera_monitoring": {}, "final_notes_for_hr": "stub",
                "needs_human_review": False, "transcript_quality": "good",
            }))
        return FakeResponse(json.dumps(FakeLLM.next_action))


@pytest.fixture(scope="module")
def server():
    rd.make_chat_model = lambda *a, **k: FakeLLM()
    rd.WEB_INTERVIEW_SESSIONS.clear()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), rd.RecruiterDashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def application():
    """A real application row with an interview token, cleaned up afterwards."""
    db = ra.RecruiterDatabase()
    db.init_schema()
    email = f"itest-{uuid.uuid4().hex[:8]}@example.invalid"
    inbox = ra.InboxEmail(
        uid=b"itest", gmail_thread_id="", message_id=f"<{uuid.uuid4()}>", references="",
        in_reply_to="", sender=email, subject="Interview test", body="b",
        received_at=None, attachments=[], thread_messages=[],
    )
    candidate_id = db.insert_candidate(
        inbox, "QuickBooks and S corporation returns.",
        ra.normalize_cv_details({"full_name": "Integration Tester"}),
        ra.normalize_evaluation({"ats_score": 80, "short_description": "x"}),
        "self_application", email, None,
    )
    app_id = db.insert_application(
        inbox, candidate_id, None, {"target_position": "US Tax Preparer"},
        ra.normalize_evaluation({"ats_score": 80, "jd_match_score": 70, "short_description": "x"}),
        "cv.pdf", uuid.uuid4().hex, None, "interview_link_pending",
        "self_application", email, None,
    )
    token = db.ensure_interview_link(app_id)
    db.close()

    yield {"id": app_id, "token": token, "email": email}

    db = ra.RecruiterDatabase()
    db.execute("DELETE FROM recruiter_sent_replies WHERE application_id = %s", (app_id,))
    db.execute("DELETE FROM recruiter_applications WHERE id = %s", (app_id,))
    db.execute("DELETE FROM recruiter_candidates WHERE id = %s", (candidate_id,))
    db.close()


def start(base, token):
    return requests.post(f"{base}/api/interview/{token}/start", json={}, timeout=30)


def turn(base, token, answer, turn_id):
    return requests.post(
        f"{base}/api/interview/{token}/turn",
        json={"answer": answer, "turn_id": turn_id},
        timeout=30,
    )


def wait_for_report(application_id, timeout=30):
    """The report is generated off the response path; poll for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        db = ra.RecruiterDatabase()
        try:
            row = db.one(
                "SELECT interview_report FROM recruiter_applications WHERE id = %s",
                (application_id,),
            )
        finally:
            db.close()
        report = ra.json_dict(row["interview_report"]) if row else {}
        if report:
            return report
        time.sleep(0.3)
    raise AssertionError(f"no interview report was written for application {application_id}")


# --- the interview actually runs ---------------------------------------------

def test_interview_starts_and_returns_a_question(server, application):
    r = start(server, application["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["question"]
    assert body["total_questions"] >= 1


def test_a_real_answer_advances_the_interview(server, application):
    start(server, application["token"])
    FakeLLM.next_action = {"action": "next_question", "reply": "Understood.", "question": "", "reason": "ok"}
    r = turn(server, application["token"],
             "I reconciled bank feeds in QuickBooks Online and closed the month for twelve clients.", 1)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "next_question"


# --- the bugs from the audits -------------------------------------------------

def test_repeat_request_is_not_scored_as_an_answer(server, application):
    """§8.2: a natural repeat request was undeliverable and got scored."""
    start(server, application["token"])
    r = turn(server, application["token"], "Could you please repeat the question?", 1)
    assert r.json()["action"] == "repeat"
    session = rd.WEB_INTERVIEW_SESSIONS[application["token"]]
    assert session["transcript"] == [], "a repeat request must not enter the transcript"


def test_thinking_aloud_holds_the_question_open(server, application):
    start(server, application["token"])
    r = turn(server, application["token"], "Give me a moment, I am thinking.", 1)
    assert r.json()["action"] == "wait"
    session = rd.WEB_INTERVIEW_SESSIONS[application["token"]]
    assert session["transcript"] == []
    assert session["current_index"] == 0, "the question must stay open"


def test_question_echo_is_not_an_answer(server, application):
    started = start(server, application["token"]).json()
    r = turn(server, application["token"], f"So your question is, {started['question']} right?", 1)
    assert r.json()["action"] == "wait"
    assert rd.WEB_INTERVIEW_SESSIONS[application["token"]]["transcript"] == []


def test_duplicate_turn_id_is_ignored(server, application):
    start(server, application["token"])
    FakeLLM.next_action = {"action": "next_question", "reply": "Ok.", "question": "", "reason": "ok"}
    turn(server, application["token"], "A full and complete answer about reconciliations.", 1)
    r = turn(server, application["token"], "A full and complete answer about reconciliations.", 1)
    assert r.json()["action"] == "ignored"


# --- session survives a restart ----------------------------------------------

def test_session_survives_a_restart_mid_interview(server, application):
    """A dashboard restart used to reset the candidate to question one."""
    token = application["token"]
    start(server, token)
    FakeLLM.next_action = {"action": "next_question", "reply": "Ok.", "question": "", "reason": "ok"}
    turn(server, token, "My first substantive answer about bank reconciliation work.", 1)

    before = rd.WEB_INTERVIEW_SESSIONS[token]
    questions_before = list(before["questions"])
    assert before["current_index"] == 1 and len(before["transcript"]) == 1

    rd.WEB_INTERVIEW_SESSIONS.clear()          # simulate the process restarting

    db = ra.RecruiterDatabase()
    stored = db.load_interview_session(application["id"])
    db.close()
    assert stored["current_index"] == 1, "progress must be persisted"
    assert stored["questions"] == questions_before
    assert len(stored["transcript"]) == 1

    # The next turn resumes rather than starting over, and the earlier answer
    # reaches the final report.
    FakeLLM.next_action = {"action": "complete", "reply": "", "question": "", "reason": "done"}
    r = turn(server, token, "My second substantive answer about preparing S corp returns.", 2)
    assert r.status_code == 200, r.text

    transcript = wait_for_report(application["id"]).get("transcript") or []
    assert len(transcript) == 2, "the answer given before the restart must survive into the report"
    assert "bank reconciliation" in transcript[0]["answer"]


# --- attempt cap --------------------------------------------------------------

def test_attempt_cap_survives_a_restart(server, application):
    """The counter lives in the database, so a process restart cannot reset it.

    Each iteration clears the stored session as well as the in-memory one, which
    is what makes the next call a genuinely fresh start rather than a resume.
    """
    token = application["token"]
    for _ in range(rd.MAX_INTERVIEW_ATTEMPTS):
        db = ra.RecruiterDatabase()
        db.clear_interview_session(application["id"])
        db.close()
        rd.WEB_INTERVIEW_SESSIONS.clear()
        assert start(server, token).status_code == 200

    db = ra.RecruiterDatabase()
    db.clear_interview_session(application["id"])
    db.close()
    rd.WEB_INTERVIEW_SESSIONS.clear()
    assert start(server, token).status_code == 429, "the cap must be enforced across restarts"


# --- errors are JSON, not an HTML page ---------------------------------------

def test_interview_api_failure_returns_json(server, application, monkeypatch):
    """Application 105 got an HTML page into fetch().json() and no log line."""
    token = application["token"]
    start(server, token)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated LLM outage")

    monkeypatch.setattr(rd.RecruiterDashboardHandler, "web_interview_turn_decision", boom)
    r = turn(server, token, "An answer that will hit the failure.", 99)
    assert r.status_code == 500
    assert r.headers["Content-Type"].startswith("application/json"), r.headers
    assert r.json()["ok"] is False


def test_unknown_token_is_a_json_404(server):
    r = requests.post(f"{server}/api/interview/{'z' * 32}/turn", json={"answer": "x"}, timeout=15)
    assert r.status_code == 404
    assert r.headers["Content-Type"].startswith("application/json")


# --- completion guards --------------------------------------------------------

def test_completing_early_is_flagged_for_human_review(server, application):
    """Application 105 completed without answering every planned question."""
    token = application["token"]
    started = start(server, token).json()
    planned = started["total_questions"]
    assert planned >= 2

    # Complete on the very first turn, leaving the rest unanswered.
    FakeLLM.next_action = {"action": "complete", "reply": "", "question": "", "reason": "done"}
    r = turn(server, token, "One answer, then the interview ends early.", 1)
    assert r.json()["action"] == "complete"

    report = wait_for_report(application["id"])
    assert report.get("needs_human_review") is True, report
    reasons = " ".join(report.get("human_review_reasons") or [])
    assert "planned questions" in reasons or "were actually answered" in reasons, reasons
    assert report.get("recommendation") == "hold", "an early exit must not produce a confident verdict"
    assert report["completion_context"]["ended_early"] is True


def test_an_empty_answer_never_completes_the_interview(server, application):
    """The system must not close a session on silence."""
    token = application["token"]
    start(server, token)
    FakeLLM.next_action = {"action": "complete", "reply": "", "question": "", "reason": "done"}
    r = turn(server, token, "", 1)
    assert r.json()["action"] == "clarify", r.json()
    assert token in rd.WEB_INTERVIEW_SESSIONS, "the session must stay open"


def test_completed_session_is_cleared_from_the_database(server, application):
    token = application["token"]
    start(server, token)
    FakeLLM.next_action = {"action": "complete", "reply": "", "question": "", "reason": "done"}
    turn(server, token, "A closing answer.", 1)
    wait_for_report(application["id"])
    db = ra.RecruiterDatabase()
    stored = db.load_interview_session(application["id"])
    db.close()
    assert stored == {}, "the stored session must be cleared once the report exists"


# --- question shaping ---------------------------------------------------------

def test_question_count_is_capped(server, application):
    body = start(server, application["token"]).json()
    assert body["total_questions"] <= rd.MAX_INTERVIEW_QUESTIONS




# --- resuming is not restarting (application 135, 2026-08-21) ----------------

def test_reload_resumes_without_consuming_an_attempt(server, application):
    """A page reload used to burn an attempt and lock the candidate out."""
    token = application["token"]
    assert start(server, token).status_code == 200
    FakeLLM.next_action = {"action": "next_question", "reply": "Ok.", "question": "", "reason": "ok"}
    turn(server, token, "A real answer about reconciliations and month end close.", 1)

    rd.WEB_INTERVIEW_SESSIONS.clear()          # browser reload / server restart
    r = start(server, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("resumed") is True
    assert body["question_number"] == 2, "must pick up where they left off"

    db = ra.RecruiterDatabase()
    used = db.one(
        "SELECT interview_attempts FROM recruiter_applications WHERE id = %s",
        (application["id"],),
    )["interview_attempts"]
    db.close()
    assert used == 1, f"a resume must not consume an attempt (used {used})"


def test_a_genuinely_fresh_start_still_counts(server, application):
    token = application["token"]
    db = ra.RecruiterDatabase()
    db.clear_interview_session(application["id"])
    db.execute(
        "UPDATE recruiter_applications SET interview_attempts = %s WHERE id = %s",
        (rd.MAX_INTERVIEW_ATTEMPTS, application["id"]),
    )
    db.close()
    rd.WEB_INTERVIEW_SESSIONS.clear()
    assert start(server, token).status_code == 429


def test_reopening_clears_the_lockout(server, application):
    token = application["token"]
    db = ra.RecruiterDatabase()
    db.clear_interview_session(application["id"])
    db.execute(
        "UPDATE recruiter_applications SET interview_attempts = %s WHERE id = %s",
        (rd.MAX_INTERVIEW_ATTEMPTS + 5, application["id"]),
    )
    db.close()
    rd.WEB_INTERVIEW_SESSIONS.clear()
    assert start(server, token).status_code == 429

    db = ra.RecruiterDatabase()
    db.execute("UPDATE recruiter_applications SET interview_attempts = 0 WHERE id = %s", (application["id"],))
    db.close()
    rd.WEB_INTERVIEW_SESSIONS.clear()
    assert start(server, token).status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
