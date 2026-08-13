# AI Voice Interview Audit — Application 105

**Candidate:** Akhil Reddy V
**Recording:** `application-105-Akhil Reddy V-20260813-051905.webm` (125 MB, 17 min 07 s)
**Recorded:** 2026-08-13 05:19 UTC
**Method:** full audio decode → webrtcvad segmentation → Whisper transcription with word timings → speaker labelling → turn analysis
**Outcome on the day:** AI recommended *hold*

---

## 1. Headline numbers

| Measure | Value |
|---|---|
| Duration | 17 min 07 s |
| Candidate speaking | 516 s — **50.2 %** |
| AI speaking | 135 s — **13.2 %** |
| …of which filler ("one moment", "I am reviewing") | **61 s across 26 utterances — 45 % of everything the AI said** |
| Silence | 376 s — **36.6 %** (over 6 minutes) |
| Questions asked | 17 |
| **Times the AI cut the candidate off mid-sentence** | **10** (7 of them with a 0.00 s gap) |
| Median answer→next-question latency | 5.0 s |
| Mean | 8.2 s |
| Worst | 55.5 s |
| Turns taking over 5 s | 15 of 29 |

Roughly **one minute in three was nobody speaking**, and nearly **half of the AI's own speech was filler** announcing that it was thinking.

---

## 2. The interruptions

Your description was exact. Ten times the AI began speaking while he was still mid-answer; in seven of those the gap was **zero**.

| Time | Gap | Candidate's speech was cut at | AI said |
|---|---|---|---|
| 01:09 | 0.52 s | "…I used to categorize as per their clients domain" | "One moment, I am thinking through your answer" |
| **01:27** | **0.00 s** | "…a small corporation or corporation or also a client who has a **partnership.**" | "One moment, I am thinking…" |
| **01:57** | **0.00 s** | "…they will receive K-1s. They used to incorporate it in your **partnership.**" | "One moment, I am thinking…" |
| **02:23** | **0.00 s** | "…two years of experience in the tax preparation I use to work with the **import date.**" | "Give me a moment. I am reviewing that." |
| **02:42** | **0.00 s** | "…So I have two years of experience with 1040. I use two **pre-prepared tax.**" | "Thanks. I am just reviewing your response." |
| **07:29** | **0.00 s** | "…based on that I used to prepare the return and if the client is **in the file.**" | "I am thinking through your answer." |
| **07:50** | **0.00 s** | "…if the expense are more than [itemised] then we can be able to claim **standard.**" | "Give me a moment. I am reviewing that." |
| **09:23** | **0.00 s** | "…a C Corp is taxed 21% and you can **pay.**" | "I am thinking through your answer." |
| 10:10 | 0.32 s | "…HSA IRA and student loan interest it will affect AGI like HSA and IRA" | "One moment I am thinking…" |
| 14:28 | 0.16 s | "…if the escalation tries to **try to.**" | "Thanks I am just reviewing your response." |

Several of these are unmistakable mid-phrase cuts. **"we can be able to claim standard"** — he was saying *standard deduction*, the actual answer to the question. **"you can pay"**, **"tries to try to"**, **"pre-prepared tax"** — all severed mid-clause.

He was then scored on those truncated answers.

### 2.1 Root cause — the microphone signal was computed and thrown away

`recruiter_dashboard.py`, `currentSilenceMs()`:

```javascript
const lastActivity = speechStarted ? lastTextActivity
                                   : Math.max(lastSpeechResultAt || 0, lastMicActivityAt || 0);
```

Once any transcript text had appeared, `speechStarted` became `true` and the function consulted **only the transcript timestamps**. `lastMicActivityAt` — the one signal that actually says *this person is still making sound* — was updated every animation frame by `startAudioActivityMonitor()` and then never read again.

Chrome's Web Speech interim results arrive in bursts. They routinely stall for a second or more while someone is speaking, especially mid-phrase or with an accent the recogniser is struggling with. The system treated that recogniser stall as the candidate having finished.

The mic monitor also only recorded activity *after* `speechStarted`:

```javascript
if (rms >= MIC_ACTIVITY_THRESHOLD && speechStarted) { lastMicActivityAt = Date.now(); }
```

so the value was stale precisely when it mattered.

### 2.2 My changes made it considerably worse

This is the part I got wrong. Commit `e713f2d`, deployed before this interview, changed:

| Constant | Was | Became | Effect |
|---|---|---|---|
| `ANSWER_SILENCE_MS` | 2200 | **1200** | turn ends after 1.2 s of apparent quiet |
| `FINAL_TRANSCRIPT_GRACE_MS` | 1400 | **900** | less settling time after a final result |
| `answerCanAutoSubmit()` | required **≥ 10 words** | no word floor | a 3-word fragment can be submitted |

I removed the word floor for a good reason — it made short repeat requests undeliverable (§8.2 of the main audit). But the floor had been *accidentally* protecting against premature submission, and I took it away while simultaneously halving the silence threshold, on top of a silence signal that was already blind to the microphone.

A candidate drawing breath mid-answer had 1.2 s before the system decided he was done.

### 2.3 Fix applied

```javascript
// Silence means the transcript AND the microphone have both gone quiet.
const lastActivity = Math.max(
  lastTextActivity,
  lastSpeechResultAt || 0,
  lastMicActivityAt || 0
);
```

- mic energy is now tracked continuously, not only after `speechStarted`
- `ANSWER_SILENCE_MS` 1200 → **1800**, `LONG_ANSWER_SILENCE_MS` 2000 → **2600**, `FINAL_TRANSCRIPT_GRACE_MS` 900 → **1200**
- `answerLooksIncomplete()` extended from 15 trailing markers to ~45, so answers ending on articles, prepositions, pronouns and auxiliaries ("…and if the client is", "…you can") hold the turn open for `INCOMPLETE_ANSWER_SILENCE_MS` (7 s) instead of being submitted

The word floor stays removed — length was never the right signal. Silence is, and it now reflects whether the candidate is actually still making sound.

---

## 3. The lag

Median 5.0 s and mean 8.2 s from the candidate finishing to the next real question, with 15 of 29 turns over 5 seconds.

Fourteen turns exceeded 8 seconds:

```
00:33  11.5s      08:55  10.3s      12:47  16.5s
01:57  10.3s      09:47   8.1s      13:23  10.1s
05:46   9.6s      10:38   8.1s      14:03  10.5s
06:30   9.7s      11:44  12.6s      15:59  55.5s
08:21  12.2s      12:07  11.2s
```

The dead-air distribution:

```
gaps >=  2s :  48   totalling  229s
gaps >=  3s :  28   totalling  179s
gaps >=  5s :   8   totalling  102s
gaps >=  8s :   5   totalling   82s
gaps >= 15s :   2   totalling   52s
```

Contributors, in order of size:

1. **The turn LLM re-sent the full CV and job description on every turn.** Up to 5 000 chars of JD plus 7 000 chars of CV plus the entire transcript, 29 times over. Fixed — the session now caches a compact `role_context` built once at start and sends only the current question, the latest answer and a bounded digest.
2. **The filler blocked the real reply.** `if (processingNudgeSpoken) afterCurrentSpeech(deliverResponse)` waited for "one moment, I am thinking through your answer" to finish speaking before delivering the answer that was already ready. Fixed — the real reply now cancels the filler.
3. **Every generated question was a TTS cache miss**, so each turn paid a full edge-tts round trip on the critical path. Fixed — questions are pre-generated in the background at session start.

**26 filler utterances totalling 61 seconds.** The candidate heard "one moment, I am thinking through your answer" or "give me a moment, I am reviewing that" **26 times** in 17 minutes.

---

## 4. How the interview ended

This is the worst sequence in the recording.

```
14:39–14:53  Candidate:  "…I will choose which is the easiest thing to complete it
                          as soon as possible and which is the complex thing raised
                          by client. So if they client."          ← cut mid-word
14:53–15:52  (59 seconds of silence)
15:52        AI:         "Sorry I had trouble processing that.
                          Could you please answer once more?"
15:55–16:54  (59 seconds of silence)
16:54        AI:         "Thank you so much for your time today. I really appreciate
                          you joining the interview and sharing your experience…"
```

He was cut off mid-word, the turn request failed, the client spoke its generic error, and then — with no answer received — the interview simply **closed**. Two full minutes of the recording are the system failing and then giving up.

That final error path is `speak('Sorry, I had trouble processing that…', () => beginListening())`, which fires when the turn POST fails. Nothing logged what the underlying failure was, and nothing prevented the session completing without an answer to its last question.

**Recommended follow-up:** the turn endpoint needs failure logging with the exception surfaced, and a session must not complete while the final question is unanswered — it should retry or mark the interview `needs_human_review`.

---

## 5. Speech-to-text quality

Better than application 20, but still corrupting domain vocabulary:

| Transcribed | Almost certainly |
|---|---|
| "experience in the **attack** preparation" | tax preparation |
| "I use two **pre-prepared attack**" | prepare tax |
| "I used to prepare the **written**" (x13) | return |
| "expense are more than **generated**" | itemised |
| "work with the **import date**" | unclear |

`1040`, `K-1`, `S-corp` and `AGI` came through correctly, so the recogniser is not uniformly bad — it fails on exactly the words that carry the technical assessment.

The scoring prompt now tells the evaluator the text is machine-transcribed and to score substance rather than fluency, with real examples. That mitigates but does not solve it; the underlying recogniser has no domain vocabulary hinting.

---

## 6. Interview shape

17 questions in 17 minutes, several of them fragments the model emitted as standalone questions ("How do you react?", "What do you do?", "How do you handle them?"). With `RECRUITER_INTERVIEW_QUESTION_COUNT=2` plus recommended questions plus one follow-up each, the count is drifting well past what was configured.

Worth reviewing separately: whether 17 short questions serves the assessment better than 5–6 substantive ones with proper follow-ups.

---

## 7. What this means for the hold recommendation

The AI recommended *hold*. On this evidence that recommendation is not trustworthy:

- **8 of his answers were truncated mid-sentence**, including at least two where the cut removed the substantive part ("claim standard…", "taxed 21% and you can pay…")
- his final answer was never captured at all
- domain terms were mangled in the transcript the evaluator read

He may well be a hold. But the interview did not give him a fair chance to demonstrate otherwise, and the transcript the judge scored was materially incomplete.

**Recommendation: re-interview rather than decide on this recording.** With the fixes applied he should get a materially different run — and the `needs_human_review` flag added in the main audit would now catch a session like this one automatically.

---

## 8. Changes made in response to this audit

| Fix | File |
|---|---|
| Silence detection uses microphone energy, not just transcript | `recruiter_dashboard.py` `currentSilenceMs()` |
| Mic activity tracked continuously | `startAudioActivityMonitor()` |
| `ANSWER_SILENCE_MS` 1200 → 1800, grace 900 → 1200 | constants |
| `answerLooksIncomplete()` widened to ~45 continuation markers | client JS |
| Turn prompt no longer carries CV + JD every turn | `web_interview_turn_decision` |
| Filler no longer blocks the real reply | `deliverResponse` |
| Questions pre-generated into the TTS cache | `pregenerate_interview_speech` |
| Judge told the text is machine-transcribed | `web_interview_report` |
| Low-signal interviews flagged `needs_human_review` | `web_interview_report` |

### 8.1 Second round — everything above plus the following

| Fix | Detail |
|---|---|
| **Second cut-off path closed** | `scheduleFinalResultAutoAdvance()` submitted on transcript timing alone, bypassing the microphone check entirely. Chrome finalises a phrase whenever the speaker draws breath, so this path could cut someone off 1.2 s later while they were still talking — probably the dominant one. It now requires `currentSilenceMs()` too. |
| **Interview API errors return JSON and are logged** | A failure fell through to the HTML error page, which arrived at a `fetch().json()` call as markup. That is exactly the "Sorry I had trouble processing that" at 15:52, and nothing anywhere recorded the cause. Failures now log a traceback plus an `interview_api_failed` event and return a JSON 500. |
| **Sessions survive a dashboard restart** | `WEB_INTERVIEW_SESSIONS` was in-memory only. A restart mid-interview silently threw the candidate back to question one against a **freshly generated** question list, losing every answer. State is now snapshotted to `recruiter_applications.interview_session` after each turn and resumed on the next one. |
| **Attempt cap actually enforced** | It lived in a process dictionary that reset on restart. Moved to `interview_attempts` in the database — and doing so exposed that `bump_interview_attempts` used `one()`, which never commits, so the counter always read back as 1. Same defect class as the message-claim bug. |
| **Early completion flagged** | A session completing before every planned question is answered now records `completion_context` and forces `needs_human_review` with the reason, instead of writing a confident score over a partial interview. An empty answer can no longer complete a session at all. |
| **Question count capped** | `MAX_INTERVIEW_QUESTIONS` (default 6), recommended questions take priority, and fragments under four words are dropped so "How do you react?" is never read as a main question. |
| **Report generation moved off the response path** | Scoring is an LLM call and was running before the closing line was sent — part of the 55 s tail. It now runs in a background thread; the stored session is cleared only once the report is written. |

### 8.2 Testing

81 tests, including a new integration suite (`tests/test_interview_api.py`) that
drives the real HTTP handler over a real socket against the real database with
only the LLM stubbed. It covers: a repeat request not being scored, thinking
aloud holding the question open, question echo not counting as an answer,
duplicate turn ids, session survival across a simulated restart, the attempt cap
holding across restarts, API failures returning JSON, empty answers never
completing a session, and early completion being flagged.

`tests/test_interview_client_timing.py` asserts the shipped page keeps the
timing invariants — silence consulting the microphone on both submit paths, the
thresholds staying above the trigger-happy range, barge-in wired up, and the
filler not blocking the real reply.

### 8.3 Still open

- **Domain vocabulary hinting for the recogniser.** Chrome's `SpeechGrammarList`
  is a no-op in practice, so improving "tax" being heard as "attack" means moving
  speech-to-text server-side (Whisper `small`/`medium` with an `initial_prompt`
  seeded from the JD). That is a larger change than anything above and is not
  done.
- **Re-interview Akhil Reddy V (application 105)** rather than deciding on this
  recording.
