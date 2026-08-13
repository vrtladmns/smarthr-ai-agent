"""Tests for the interview page's turn-ending logic (AGENT_AUDIT §2, INTERVIEW_AUDIT_105 §2).

The logic lives in JavaScript inside a Python f-string, so these tests extract
the rendered page source and check the invariants that actually caused
candidates to be cut off mid-sentence. They are deliberately assertions about
the shipped page text, not a JS runtime.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_dashboard as rd

SOURCE = Path(__file__).resolve().parent.parent.joinpath("recruiter_dashboard.py").read_text()


def js_block(name: str) -> str:
    """Return the body of a JavaScript function as it appears in the page."""
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for i in range(start, len(SOURCE)):
        if SOURCE[i] == "{":
            depth += 1
        elif SOURCE[i] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start : i + 1]
    raise AssertionError(f"could not extract {name}")


# --- silence must include the microphone --------------------------------------

def test_silence_uses_microphone_activity():
    """The mic signal was computed every frame and then never read."""
    body = js_block("currentSilenceMs")
    assert "lastMicActivityAt" in body, "silence must consider microphone energy"
    assert "speechStarted ?" not in body, (
        "the old ternary ignored the microphone once any transcript text existed"
    )
    assert "Math.max(" in body


def test_microphone_activity_is_tracked_unconditionally():
    """It used to only record activity after speechStarted, so it was stale."""
    monitor = js_block("startAudioActivityMonitor")
    assert "rms >= MIC_ACTIVITY_THRESHOLD && speechStarted" not in monitor
    assert "rms >= MIC_ACTIVITY_THRESHOLD" in monitor


def test_final_result_path_also_respects_the_microphone():
    """Chrome finalises a phrase whenever the speaker draws breath."""
    body = js_block("scheduleFinalResultAutoAdvance")
    assert "currentSilenceMs()" in body, (
        "this path submitted on transcript timing alone and reintroduced cut-offs"
    )


# --- thresholds ---------------------------------------------------------------

def _const(name: str) -> int:
    return int(re.search(rf"const {name} = (\d+);", SOURCE).group(1))


def test_silence_thresholds_are_not_trigger_happy():
    """1200ms cut a candidate off if they paused for breath."""
    assert _const("ANSWER_SILENCE_MS") >= 1500
    assert _const("LONG_ANSWER_SILENCE_MS") >= _const("ANSWER_SILENCE_MS")
    assert _const("FINAL_TRANSCRIPT_GRACE_MS") >= 1000
    assert _const("INCOMPLETE_ANSWER_SILENCE_MS") >= 5000


def test_incomplete_answers_hold_the_turn_open():
    body = js_block("answerLooksIncomplete")
    # Answers ending on these were submitted mid-sentence in application 105.
    for word in ["'the'", "'to'", "'and'", "'is'", "'we'", "'if'"]:
        assert word in body, f"{word} must mark an answer as incomplete"


def test_no_word_count_floor_blocks_short_utterances():
    """A repeat request must still be deliverable (application 91)."""
    body = js_block("answerCanAutoSubmit")
    assert "words >= 10" not in body, "length was never the right signal"


# --- barge-in -----------------------------------------------------------------

def test_barge_in_is_wired_up():
    assert "aiIsSpeaking" in SOURCE
    assert "BARGE_IN_MIN_WORDS" in SOURCE
    assert "Go ahead, I am listening." in SOURCE


# --- the filler must not delay the real reply ---------------------------------

def test_filler_never_blocks_the_real_response():
    assert "if (processingNudgeSpoken) afterCurrentSpeech(deliverResponse)" not in SOURCE, (
        "waiting for the filler to finish added seconds to every turn"
    )


# --- server-side session shape ------------------------------------------------

def test_question_count_is_bounded():
    assert rd.MAX_INTERVIEW_QUESTIONS <= 8


def test_interview_role_context_is_compact():
    """The turn prompt used to carry 5000 chars of JD and 7000 of CV each turn."""
    context = rd.interview_role_context({
        "requirement_position": "US Tax Preparer",
        "full_name": "Test Person",
        "cv_summary": "x" * 5000,
        "job_description": "y" * 9000,
    })
    assert len(context["cv_summary"]) <= 1200
    assert len(context["job_description"]) <= 1200
    assert "raw_cv_text" not in context


def test_transcript_digest_is_bounded():
    digest = rd.interview_transcript_digest(
        [{"question": "q" * 900, "answer": "a" * 3000, "status": "answered"} for _ in range(30)]
    )
    assert len(digest) <= 6
    assert all(len(d["answer"]) <= 600 for d in digest)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
