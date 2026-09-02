# Voice interview audit

**Date:** 2026-09-02
**Scope:** the AI voice interview at `/interview/<token>` — the browser room, the turn
loop, the scoring, and what came out the other end.
**Method:** every interview on the production database (30 links, 16 started, 10 with a
report), the stored transcripts read in full, plus the shipped page source. Nothing was
changed and nothing was written to the database.

---

## The short version

Ten people have completed an AI interview. **Nine of the ten scored below the pass
mark**, and one of those nine had "an exceptionally strong CV" by the evaluator's own
account and scored **3 out of 100**.

The interviews are not failing because the candidates are bad. They are failing because
the transcript the model scores is not what the candidate said. Every recent report that
carries a quality field says `poor`, and the evaluator keeps saying so itself:

> "Substance is present; transcription is garbled but process is recognizable." — app 184

The room is doing its job. The words are being lost between the candidate's mouth and
the model, and everything downstream is scoring the damage.

---

## The funnel

```
30  interview links issued
14  never opened                      47%
16  opened
 6  abandoned part-way                37% of those who started
10  completed with a report
 1  scored above the pass mark of 35
```

Scores of the ten completed: **3, 15, 22, 22, 24, 26, 29, 32, 32.5, 70**.

A pass mark of 35 out of 100 is already generous. Nine failures out of ten against a
generous bar is a measurement problem, not a hiring signal.

---

## Finding 1 — speech-to-text is destroying the vocabulary of the job

**Severity: critical. This causes most of the others.**

The room uses the browser's Web Speech API with `rec.lang = 'en-US'` hardcoded
(`recruiter_dashboard.py`), no vocabulary hinting of any kind — `SpeechGrammarList` does
not appear in the file — and no post-correction pass. Candidates are Indian accounting
and tax professionals speaking Indian English about US tax forms.

Real answers, exactly as stored:

| Transcript | What was said |
|---|---|
| `import into 10:40` | import into **1040** |
| `11 today` | **1120** |
| `reconcile the data with our back statements` | **bank** statements |
| `we like a parent aging report` | we **prepare an** aging report |
| `we find the old out sending emails` | the old **outstanding amounts** |
| `can you leave while finalizing sign open real and the balance sheet equipment` | unrecoverable |
| `Ek line hua hai to business a small Corporation` | code-switched, unrecoverable |

A tax interview in which "1040" is transcribed as a time of day cannot be scored. The
model is being handed a document in which every domain term has been replaced by a
homophone, and then asked to judge domain competence.

`en-IN` is a one-line change and is the single highest-value fix available.

---

## Finding 2 — answers are still being cut off mid-sentence

**Severity: critical.**

Answers end in the middle of a clause, on a preposition, mid-word:

```
app 184  "...once that I identified a month hunting we also prepare World"
app 184  "...my team and I are responsible for foundational data and building them"
app 105  "the statement applying"                                    (3 words)
app 105  "I have to"                                                 (3 words)
app 105  "...I used to category as per the as per their"
app 105  "...by using that we"
```

App 105 has 26 turns for what should be a handful of questions, and seven of its answers
are under ten words. Those are not answers, they are fragments of answers that were
submitted while the candidate was still talking.

This was audited before (`INTERVIEW_AUDIT_105.md`) and timing fixes were made. App 184 is
from **28 August**, after those fixes, and still ends two of its three answers
mid-sentence. Whatever is cutting people off is not only the silence threshold.

---

## Finding 3 — every recent interview is handed to a human anyway

**Severity: high.**

Five reports carry `needs_human_review: true`, and in all five cases the sole reason is:

```
["speech-to-text quality was poor"]
```

When that flag is set the recommendation is forced to `hold` and the application parks on
HR. So the current state of the feature is: the candidate spends ten minutes talking to
it, and a person still has to listen to the recording and decide. The interview is adding
work rather than removing it.

---

## Finding 4 — the scoring prompt contradicts itself, and loses

**Severity: high.**

The prompt is explicit:

> "Never write that an answer was 'disjointed', 'unclear', 'incoherent' or 'lacked
> articulation' when the underlying technical content is present."
> "If a transcript is too garbled to judge the substance, say so and set
> needs_human_review to true **rather than scoring it low**."

What the model actually wrote, for a candidate whose four answers ran 90, 81, 24 and 85
words:

> "the interview revealed severe communication gaps. She cannot clearly explain her
> experience" — app 90, scored **15**

And for the candidate with the exceptionally strong CV whose answers were cut off at 6 and
4 words:

> "Answers were incomplete and lacked concrete examples" — app 47, scored **3**

The instruction not to penalise transcription damage is not being followed, and the
instruction to withhold rather than score low is being followed *and* the score is being
written anyway. Both halves fail in the same direction: against the candidate.

Two smaller problems in the same prompt:

- **The scale is never stated.** The schema says `"overall_score": 0` with no range. The
  bands imply 0–100 by referring to 35 and 25, but nothing says so. Scores of 3 and 70 in
  the same dataset suggest the model is not working to a consistent scale.
- **Camera monitoring** is fed in as an input to a hiring decision. It is a browser-side
  signal that cannot be verified, and the prompt only says not to "over-penalize".

---

## Finding 5 — the interview is too short to decide anything

**Severity: high.**

```
RECRUITER_INTERVIEW_QUESTION_COUNT=2
```

Two base questions, plus whatever the requirement lists as recommended. Real completed
interviews:

```
app 184   3 turns,  5.2 minutes
app 157   4 turns,  4.2 minutes
app  91   3 turns,  7.8 minutes
app  90   4 turns,  9.2 minutes
```

App 184 was assessed, scored 32.5 and parked on HR on the strength of **three answers in
five minutes**, two of which were cut off. That is not enough evidence to reject anyone,
and it is nowhere near enough to survive a candidate asking why.

---

## Finding 6 — the room turns most browsers away

**Severity: high, and it likely explains much of the 47%.**

```js
const blocked = /Firefox\//.test(ua) || /Edg\//.test(ua) || /OPR\//.test(ua)
                || (/Safari\//.test(ua) && !/Chrome\//.test(ua));
const isChrome = (/Chrome\//.test(ua) || /Chromium\//.test(ua)) && /Google Inc/.test(vendor);
return hasSpeechRecognition && isChrome && !blocked;
```

Edge, Firefox, Opera, Safari and every iPhone are refused. A candidate on a work laptop
with Edge, or on an iPhone, is told to go and find Chrome. **Fourteen of thirty links were
never opened at all**, and this is the most plausible single explanation.

The Web Speech API genuinely is Chrome-only, so the gate is honest — but the consequence
is that half the invitations land somewhere they cannot be used, and the fallback offered
("please type your answers manually") is not what the candidate was promised.

---

## Finding 7 — there is a much better engine in the repository, unused

**Severity: high, because it changes what Finding 1 costs to fix.**

`voice_agent.py` is 108 KB and implements the same interview with **faster-whisper** for
speech-to-text, **webrtcvad** for turn detection and **edge-tts** for the voice. It is
imported by nothing:

```
$ grep -rn "voice_agent" --include=*.py .     # only the file itself
```

Whisper transcribes "1040" and "1120" correctly, is not Chrome-only, and does not care
what browser the candidate has, because the audio is transcribed server-side. Findings 1,
2 and 6 are all consequences of using the browser's recogniser instead.

Whether this file works as written is not something this audit establishes — it has no
tests and nothing imports it. But it means the fix for the worst problem is likely
"finish and wire up what is already here", not "build a speech pipeline".

---

## Finding 8 — two completed interviews were never acted on

**Severity: medium.**

```
app 47  completed, scored 3     status still interview_link_sent
app 49  completed, scored 26    status still interview_link_sent
```

Both have a full report. Neither had its outcome applied, so neither candidate was told
anything and neither appears in any HR queue. They are simply lost.

---

## Finding 9 — session lifetime is unbounded

**Severity: medium.**

```
app 105   1311 minutes   (21.9 hours, 08-12 07:27 -> 08-13 05:18)
app  44     89 minutes
```

An interview that resumes the next morning is not one interview, and its transcript is
scored as though it were. There is no cap on how long a session may stay open, and six
sessions are currently abandoned mid-interview with no expiry.

---

## What to fix, in order

1. **`rec.lang = 'en-IN'`.** One line. Biggest single improvement to every score in the
   system. Follow it with a domain correction pass over the transcript — a small map of
   1040/1120/1065/W-2/QuickBooks/reconciliation and their observed manglings — before the
   text ever reaches the evaluator.
2. **Stop scoring damaged transcripts.** Make `needs_human_review` suppress the numeric
   score rather than accompany it, so nobody is recorded as a 3 because the microphone
   lost the words.
3. **Find what is still truncating answers.** App 184 proves the earlier timing work did
   not finish the job. This needs a real session with the timing instrumented, not
   another threshold guess.
4. **Raise `RECRUITER_INTERVIEW_QUESTION_COUNT`** to at least 4. Three answers is not an
   interview.
5. **Apply the outcome for apps 47 and 49**, and find why it was skipped.
6. **Decide about the browser gate.** Either accept Chrome-only and say so in the
   invitation email so candidates open the link on the right machine, or move
   transcription server-side (Finding 7) and let anyone in.
7. **Expire sessions** after a couple of hours so an interview cannot span two days.
8. **State the score scale** in the prompt, and take camera monitoring out of the hiring
   decision.

## What this audit does not establish

- Whether `voice_agent.py` runs. It is read as source only.
- The true cause of Finding 2. The evidence shows truncation is still happening; it does
  not show which of the several turn-ending paths is responsible.
- Anything about audio quality on the candidate's side. Poor microphones would make
  Finding 1 worse, and nothing here separates the two.
- Whether the six abandoned interviews were abandoned for the reasons above or because
  the candidate simply left.
