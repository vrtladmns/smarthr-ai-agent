"""The agent must act on the decision it has already made.

Application 187 (ashwaninaret@gmail.com, 31 Aug): the CV scored 30 against a
JD minimum of 40 and was marked rejected_jd_score. The agent then emailed HR
"click Revoke JD Rejection", and told the candidate their application was
"currently under review". Nobody was ever going to write the rejection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra

SOURCE = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()


class Recorder:
    """Stands in for the agent's mailer and model."""

    def __init__(self):
        self.sent = []
        self.ai = self

    def draft_reply(self, inbox_email, instruction, facts, fallback):
        self.instruction = instruction
        self.facts = facts
        return fallback

    def send_candidate_reply(self, inbox_email, subject, body, scenario=None, application=None):
        self.sent.append({"subject": subject, "body": body, "scenario": scenario})
        return True


class Email:
    subject = "applying for accounts role"
    sender = "ashwaninaret@gmail.com"
    body = "Please find my CV attached."
    attachments = []


def send_rejection(role="US Bookkeeper", is_referral=False):
    r = Recorder()
    ra.AIRecruiterAgent.reply_not_selected(
        r, Email(), {"id": 187, "requirement_position": role}, is_referral=is_referral
    )
    return r


# --- the candidate is told ----------------------------------------------------

def test_a_rejected_candidate_is_actually_told():
    r = send_rejection()
    assert r.sent, "the rejection must reach the candidate"
    assert r.sent[0]["scenario"] == "not_selected"


def test_the_reply_gives_an_answer_not_a_holding_note():
    body = send_rejection().sent[0]["body"].lower()
    assert "not be taking it forward" in body
    for holding in ["under review", "get back to you as soon as", "currently reviewing"]:
        assert holding not in body, f"{holding!r} is what left application 187 hanging"


def test_it_does_not_leak_the_machinery():
    r = send_rejection()
    body = r.sent[0]["body"].lower()
    for leak in ["score", "ats", "jd match", "hr team will", "pass this to", "ai ", "algorithm"]:
        assert leak not in body, f"the candidate must not see {leak!r}"
    assert "do not mention anyone else being involved" in r.instruction
    assert "no scores" in r.instruction


def test_it_closes_the_thread_rather_than_inviting_a_debate():
    r = send_rejection()
    assert r.facts.get("final") is True
    assert r.facts.get("ask_for_nothing") is True


def test_the_role_is_named_when_known_and_omitted_when_not():
    assert "us bookkeeper" in send_rejection().sent[0]["body"].lower()
    body = send_rejection(role=None).sent[0]["body"].lower()
    assert "none" not in body and "what we are hiring for" in body


def test_it_is_sent_once():
    assert "not_selected" in ra.ONCE_PER_APPLICATION_SCENARIOS


# --- the wiring ---------------------------------------------------------------

def test_a_jd_rejection_no_longer_falls_through_to_under_review():
    assert "jd_rejected = True" in SOURCE
    assert "if jd_rejected:" in SOURCE
    i = SOURCE.index("if jd_rejected:")
    branch = SOURCE[i : i + 700]
    assert "reply_not_selected" in branch
    assert "reply_received" not in branch.split("elif")[0]


def test_hr_is_informed_not_asked():
    assert "The candidate has been told." in SOURCE
    assert "If this should continue, open the application and click Revoke JD Rejection." not in SOURCE, (
        "that wording made a decision the agent had already taken look provisional"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
