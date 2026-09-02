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
    assert "speechStarted" not in monitor, "the monitor must not be gated on speechStarted"
    assert "lastMicActivityAt = Date.now();" in monitor


def test_final_result_path_also_respects_the_microphone():
    """Chrome finalises a phrase whenever the speaker draws breath."""
    body = js_block("scheduleFinalResultAutoAdvance")
    assert "currentSilenceMs()" in body, (
        "this path submitted on transcript timing alone and reintroduced cut-offs"
    )


# --- thresholds ---------------------------------------------------------------

def _const(name: str) -> int:
    return int(re.search(rf"const {name} = (\d+);", SOURCE).group(1))


def _const_float(name: str) -> float:
    return float(re.search(rf"const {name} = ([\d.]+);", SOURCE).group(1))


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




# --- background noise must not hold the turn open (2026-08-21) ---------------

def test_microphone_threshold_adapts_to_the_room():
    """A fan sits above the old fixed threshold, so the mic never went quiet
    and the candidate had to mute to advance."""
    monitor = js_block("startAudioActivityMonitor")
    assert "noiseFloor" in monitor
    assert "NOISE_FLOOR_MULTIPLIER" in monitor
    assert "speechThreshold" in monitor
    assert "rms >= MIC_ACTIVITY_THRESHOLD" not in monitor, (
        "a fixed threshold cannot tell a fan from speech"
    )


def test_the_noise_floor_only_learns_from_non_speech():
    """Otherwise a few seconds of talking drags the floor past the speaker."""
    monitor = js_block("startAudioActivityMonitor")
    assert "if (!soundsLikeSpeech)" in monitor, (
        "the floor must not be updated from speech frames"
    )


def test_the_microphone_can_never_block_a_turn_forever():
    body = js_block("currentSilenceMs")
    assert "MIC_VETO_CEILING_MS" in body
    assert _const("MIC_VETO_CEILING_MS") <= 10000, "the ceiling must actually bite"


def test_noise_floor_constants_are_sane():
    assert _const_float("NOISE_FLOOR_MULTIPLIER") > 1.0, "speech must exceed the room"
    assert 0 < _const_float("NOISE_FLOOR_RISE") < _const_float("NOISE_FLOOR_FALL"), (
        "the floor should fall to a quiet room quickly and rise reluctantly"
    )


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))


# --- the answer box must not carry a request into the next answer (2026-08-29) -

def _branch(action: str) -> str:
    """The body of one `if (action === '...')` arm in the submit handler."""
    marker = "if (action === '" + action + "')"
    start = SOURCE.index(marker)
    depth = 0
    for i in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[i] == "{":
            depth += 1
        elif SOURCE[i] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start : i + 1]
    raise AssertionError(f"could not extract the {action} branch")


def test_the_answer_box_is_cleared_before_listening_again():
    """Reported: after a repeat, every later answer was read as another repeat
    request. beginListening() seeds the transcript from the answer box, so
    anything left in it is prepended to whatever the candidate says next."""
    listening = js_block("beginListening")
    assert "finalTranscript = answerEl.value.trim()" in listening, (
        "precondition: the transcript is seeded from the answer box"
    )
    for action in ["repeat", "clarify", "wait"]:
        body = _branch(action)
        assert "answerEl.value = '';" in body, (
            f"the {action} branch must clear the answer box before listening again"
        )


def test_the_seeded_transcript_is_cleared_too():
    for action in ["repeat", "clarify", "wait"]:
        assert "finalTranscript = '';" in _branch(action), action


# --- the room must never hang on "Processing your answer..." (2026-08-29) ------

def test_the_turn_request_has_a_timeout():
    """fetch() has none of its own, so a turn that never came back left the room
    on 'Processing your answer...' with no way out."""
    assert "TURN_TIMEOUT_MS" in SOURCE
    assert "new AbortController()" in SOURCE
    assert "signal: turnAbort.signal" in SOURCE
    ms = int(re.search(r"const TURN_TIMEOUT_MS = (\d+);", SOURCE).group(1))
    assert 20000 <= ms <= 60000, "long enough for a real turn, short enough to rescue"


def test_the_turn_timeout_clears_real_observed_latency():
    """From LangSmith, 30 days: interview turns ran p50 5.5s, p90 14.2s, max
    14.2s. A timeout at or below that would cut off turns that were working."""
    import recruiter_dashboard as rd
    assert rd.INTERVIEW_TURN_LLM_TIMEOUT >= 20, "must clear the observed 14.2s max with margin"
    assert rd.INTERVIEW_TURN_LLM_TIMEOUT <= 40, "a candidate is sitting in silence"


def test_the_report_is_allowed_to_be_slow():
    """The slowest real call in 30 days was a 59.1s interview report. It runs
    after the candidate has gone, so it must not inherit a short leash."""
    import recruiter_dashboard as rd, llm_factory
    assert rd.INTERVIEW_REPORT_LLM_TIMEOUT > 60
    assert rd.INTERVIEW_REPORT_LLM_TIMEOUT > llm_factory.LLM_TIMEOUT_SECONDS
    assert rd.INTERVIEW_REPORT_LLM_TIMEOUT > rd.INTERVIEW_TURN_LLM_TIMEOUT


def test_the_client_waits_longer_than_the_server():
    """Otherwise the client gives up on turns the server was about to answer."""
    import recruiter_dashboard as rd
    ms = int(re.search(r"const TURN_TIMEOUT_MS = (\d+);", SOURCE).group(1))
    assert ms > rd.INTERVIEW_TURN_LLM_TIMEOUT * 1000


def test_a_failed_turn_leaves_the_room_usable():
    start = SOURCE.index("} catch (error)", SOURCE.index("const turnAbort"))
    depth = 0
    for i in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[i] == "{":
            depth += 1
        elif SOURCE[i] == "}":
            depth -= 1
            if depth == 0:
                catch = SOURCE[start : i + 1]
                break
    else:
        raise AssertionError("could not extract the catch block")
    assert "isSubmitting = false;" in catch
    assert "answerEl.value = '';" in catch, "the retry must not append to the lost attempt"
    assert "beginListening()" in catch, "the candidate must be listened to again"


# --- found by taking the interview (2026-09-03) -------------------------------

def test_incomplete_endings_match_whole_words_not_substrings():
    """endsWith() matched 'is' inside "analysis", 'on' inside "reconciliation",
    'or' inside "vendor" and 'a' inside "data". answerCanAutoSubmit() then
    refused to submit, so a turn ending on the commonest words of the job could
    never end. Reported as "not responding"."""
    body = js_block("answerLooksIncomplete")
    assert "text.endsWith(ending)" not in body, "substring matching blocked real answers"
    assert "split(" in body and "tails" in body


def test_a_request_is_always_deliverable():
    """"could you repeat that" ends on 'that', a listed ending. The server is
    what decides whether something was an answer; the browser must deliver it."""
    assert "function answerLooksLikeRequest" in SOURCE
    gate = js_block("answerCanAutoSubmit")
    assert "answerLooksLikeRequest()" in gate
    assert gate.index("answerLooksLikeRequest") < gate.index("answerLooksIncomplete")


def test_the_interview_javascript_has_no_unescaped_word_boundaries():
    """The page is built inside a Python f-string, so a lone \\b in the source
    renders as a backspace character and the regex silently stops matching.
    \\s survives only because Python does not recognise it as an escape."""
    import re as _re
    start = SOURCE.index("function isSupportedInterviewBrowser")
    region = SOURCE[start:]
    lone = list(_re.finditer(r"(?<!\\)\\b", region))
    assert not lone, f"{len(lone)} unescaped \\b in the interview JS; use \\\\b"


def test_a_wedged_recogniser_is_cycled():
    """Measured live: no recognition result for 7003ms while the microphone had
    sound 1087ms earlier. The turn then either got cut off by the mic ceiling or,
    with nothing transcribed, could never be submitted at all."""
    assert "function startRecognitionWatchdog" in SOURCE
    body = js_block("startRecognitionWatchdog")
    assert "RECOGNITION_STALL_MS" in body
    assert "recognition.stop()" in body
    assert "RECOGNITION_CYCLE_LIMIT" in body, "a watchdog must not thrash"


def test_a_deliberate_cycle_is_not_treated_as_a_failure():
    """recognition.stop() fires onerror with 'aborted'. Treating that as fatal
    set isRecording false and killed the turn the watchdog was rescuing."""
    assert "reason === 'aborted'" in SOURCE
    i = SOURCE.index("reason === 'aborted'")
    branch = SOURCE[i : i + 800]
    assert "return;" in branch, branch[-120:]
    assert "isRecording = false" not in branch.split("return;")[0]


def test_a_failed_start_retries_instead_of_giving_up():
    assert "RECOGNITION_RESTART_LIMIT" in SOURCE
    assert "recognition_start_failed" in SOURCE
