"""Screening questions must arrive as a list, not one run-on sentence.

The body builder collapsed every paragraph with \\s+ -> " ", so a list written
across separate lines came out as a single line, and the HTML renderer turned
what was left into <br> separated text. Both had to change for a bullet to
survive the trip.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra


class Recorder:
    def __init__(self):
        self.sent = []
        self.ai = self

    def draft_reply(self, inbox_email, instruction, facts, fallback):
        self.instruction = instruction
        self.facts = facts
        return fallback

    def send_candidate_reply(self, inbox_email, subject, body, scenario=None, application=None):
        self.sent.append({"subject": subject, "body": body, "scenario": scenario})


class Email:
    subject = "Resume Submition"
    sender = "mandhadivya13@gmail.com"
    body = ""
    attachments = []


def screening_body(role="US Bookkeeper"):
    r = Recorder()
    ra.AIRecruiterAgent.reply_screening_questions(r, Email(), {"position_title": role} if role else None)
    return r, r.sent[0]["body"]


# --- the plain text ------------------------------------------------------------

def test_the_questions_are_on_their_own_lines():
    _, body = screening_body()
    for question in ["- Your current salary", "- Your expected salary",
                     "- Your current location", "- How soon you can join"]:
        assert question in body, question


def test_the_work_terms_are_listed_too():
    _, body = screening_body()
    assert "- Shift:" in body
    assert "- Office location:" in body


def test_they_are_not_run_together_in_a_sentence():
    _, body = screening_body()
    assert "current salary, expected salary, current location" not in body


# --- the HTML ------------------------------------------------------------------

def test_the_html_carries_real_list_markup():
    _, body = screening_body()
    html = ra.email_body_to_html(body)
    assert html.count("<ul") == 2, "work terms and questions are two lists"
    assert html.count("<li") == 9
    assert "- Your current salary" not in html, "a dash in text is not a bullet"


def test_a_numbered_list_becomes_an_ordered_list():
    html = ra.email_body_to_html("Hi,\r\n\r\nSteps:\n1. First\n2. Second")
    assert "<ol" in html and html.count("<li") == 2


def test_text_around_a_list_keeps_its_paragraph():
    html = ra.email_body_to_html("Hi,\r\n\r\nBefore we start:\n- one\n- two\n\nThanks.")
    assert "<p>Before we start:</p>" in html
    assert "<ul" in html
    assert "<p>Thanks.</p>" in html


def test_links_inside_a_bullet_are_still_linkified():
    html = ra.email_body_to_html("Hi,\r\n\r\n- Join at https://example.com/x")
    assert '<a href="https://example.com/x"' in html
    assert "<li" in html


# --- nothing else changes shape -------------------------------------------------

def test_ordinary_paragraphs_are_unaffected():
    body = ra.recruiter_email_body("Thanks for your note.", "I will come back to you shortly.")
    assert body == "Hi,\r\n\r\nThanks for your note.\r\n\r\nI will come back to you shortly."
    assert ra.email_body_to_html(body) == (
        "<p>Hi,</p><p>Thanks for your note.</p><p>I will come back to you shortly.</p>"
    )


def test_a_paragraph_wrapped_in_the_source_is_still_one_line():
    """Implicit concatenation across source lines must not become a line break."""
    body = ra.recruiter_email_body(
        "This sentence is written across two source lines "
        "but is one line in the value."
    )
    assert "\n" not in body.split("\r\n\r\n")[1]


def test_the_model_is_told_to_use_bullets():
    r, _ = screening_body()
    assert "bulleted list" in r.instruction
    assert r.facts.get("format_questions_as_bullets") is True
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    assert 'each on its own line starting with "- "' in source, "the drafting prompt must say so"


# --- the chase for missing details ---------------------------------------------

def test_missing_details_are_listed():
    r = Recorder()
    ra.AIRecruiterAgent.reply_screening_missing_details(
        r, Email(), {"current_salary": 500000, "current_location": "Mohali"}
    )
    body = r.sent[0]["body"]
    assert "- Expected salary" in body
    assert "<li" in ra.email_body_to_html(body)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
