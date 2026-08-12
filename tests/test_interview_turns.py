"""Regression tests for the AI voice interview turn-taking (AGENT_AUDIT.md §8).

The verbatim strings below are from application 91's recording.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_dashboard as rd

FOLLOW_UP = (
    "Could you give a specific example of how you qualified a prospect's budget "
    "and authority in a sales conversation?"
)
Q2 = "What strategies did you use to close deals with US clients?"


# --- the exact failure from application 91 -----------------------------------

def test_short_repeat_request_is_honoured():
    assert rd.classify_interview_turn("Could you please repeat the question?", FOLLOW_UP) == "repeat_request"


def test_three_word_repeat_request_is_honoured():
    assert rd.classify_interview_turn("Can you repeat?", FOLLOW_UP) == "repeat_request"


def test_long_polite_repeat_request_is_honoured():
    text = "Sorry, I could not hear you properly, could you repeat the question once more please?"
    assert rd.classify_interview_turn(text, FOLLOW_UP) == "repeat_request"


def test_repeat_request_followed_by_question_echo_is_not_an_answer():
    """The exact utterance that was scored as her answer."""
    text = (
        "Could you please repeat the question? So, according to me, your question is, "
        "could you give me a specific example of how you qualified prospect's budget "
        "and authority in a sales conversation, right?"
    )
    assert rd.classify_interview_turn(text, FOLLOW_UP) != "answer"


def test_bare_question_echo_is_not_an_answer():
    text = "So, your question is, what strategies did you use to close the deal with US clients, right?"
    assert rd.classify_interview_turn(text, Q2) == "question_echo"


def test_thinking_aloud_is_not_an_answer():
    for text in [
        "I'm answering the question. Give me just one minute.",
        "Give me a moment, I am thinking.",
        "Let me think about that for a second.",
        "Hold on, one second.",
    ]:
        assert rd.classify_interview_turn(text, Q2) == "thinking", text


# --- real answers must still be answers ---------------------------------------

def test_real_answer_is_an_answer():
    text = (
        "So first, what I do, I focus on ROI, not just price. The plan will save you "
        "20% downtime. To handle objections I listen and give a solution for budget "
        "and contract concerns, and I created urgency with limited offers and did "
        "timely follow-up."
    )
    assert rd.classify_interview_turn(text, Q2) == "answer"


def test_answer_containing_the_word_repeat_is_still_an_answer():
    text = (
        "I repeat the same reconciliation process for each batch of invoices every "
        "month, and then I hand the summary to the finance lead for their review "
        "before we close the books for that period."
    )
    assert rd.classify_interview_turn(text, Q2) == "answer"


def test_short_but_genuine_answer_is_an_answer():
    assert rd.classify_interview_turn("I used Redis for caching.", "How do you debug a slow API endpoint?") == "answer"


def test_answer_reusing_question_words_is_not_an_echo():
    """Overlapping vocabulary alone must not count as echoing the question."""
    text = (
        "To close deals with US clients I always led with ROI and gave them a "
        "clear migration plan, then followed up on a fixed weekly cadence until "
        "we had signature."
    )
    assert rd.classify_interview_turn(text, Q2) == "answer"


# --- digest ------------------------------------------------------------------

def test_transcript_digest_is_bounded():
    transcript = [{"question": "q" * 500, "answer": "a" * 2000, "status": "answered"} for _ in range(20)]
    digest = rd.interview_transcript_digest(transcript)
    assert len(digest) == 6
    assert len(digest[0]["question"]) <= 300
    assert len(digest[0]["answer"]) <= 600


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
