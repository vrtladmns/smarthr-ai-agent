import argparse
import json
import mimetypes
import random
import re
from datetime import datetime
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

from config import DB_PROVIDER, OLLAMA_NUM_PREDICT, RECRUITER_INTERVIEW_QUESTION_COUNT
from llm_factory import make_chat_model
from recruiter_agent import (
    RecruiterDatabase,
    candidate_interview_url,
    notify_post_interview_outcome,
    send_final_hr_round_request,
    send_interview_link_for_application,
    send_interview_rejection,
    send_interview_request_after_hr_approval,
    send_teams_link_for_application,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
WEB_INTERVIEW_SESSIONS: dict[str, dict] = {}


def notify_post_interview_outcome_async(application_id: int, report: dict):
    def run():
        try:
            notify_post_interview_outcome(application_id, report)
        except Exception as exc:
            print(f"Post-interview notification failed for application {application_id}: {exc}")

    Thread(target=run, daemon=True).start()


def html_escape(value) -> str:
    return escape("" if value is None else str(value))


def parse_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def parse_decimal(value: str | None):
    value = (value or "").strip()
    return Decimal(value) if value else None


def parse_int(value: str | None):
    value = (value or "").strip()
    return int(value) if value else None


def money(value, currency="INR") -> str:
    if value is None:
        return "-"
    amount = Decimal(value)
    return f"{html_escape(currency)} {amount:,.0f}"


def score(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value):.0f}"


def date_text(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %H:%M")
    return str(value)


def list_text(value, limit: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return html_escape(value)
    if isinstance(value, list):
        parts = []
        for item in value[:limit]:
            if isinstance(item, dict):
                parts.append(", ".join(html_escape(v) for v in item.values() if v))
            else:
                parts.append(html_escape(item))
        more = f" +{len(value) - limit}" if len(value) > limit else ""
        return ", ".join(parts) + more if parts else "-"
    return html_escape(value)


def json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def extract_json_object(response: str) -> str:
    match = re.search(r"\{.*\}", response or "", flags=re.S)
    if not match:
        raise ValueError(f"LLM did not return JSON: {response}")
    return match.group(0)


def looks_like_question(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).lower()
    if not text:
        return False
    question_starters = (
        "can you",
        "could you",
        "would you",
        "will you",
        "what ",
        "why ",
        "how ",
        "when ",
        "where ",
        "which ",
        "tell me",
        "explain",
        "describe",
        "walk me",
    )
    return "?" in text or text.startswith(question_starters)


def closing_interview_reply() -> str:
    return random.choice(
        [
            "Thank you so much for your time today. I really appreciate you joining the interview and sharing your experience with me. I will review everything carefully and get back to you with feedback soon.",
            "Thanks for speaking with me today. I appreciate your time, your patience, and the details you shared during the interview. I will get back to you with feedback soon.",
            "Thank you, that completes our interview for today. I appreciate the time you gave and the answers you shared. I will review everything and get back to you with feedback soon.",
        ]
    )


def status_badge(status: str | None) -> str:
    status = status or "unknown"
    key = status.lower().replace(" ", "_")
    return f'<span class="badge badge-{html_escape(key)}">{html_escape(status.replace("_", " "))}</span>'


def detail_path(path: str, section: str) -> int | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) == 2 and parts[0] == section and parts[1].isdigit():
        return int(parts[1])
    return None


def download_path(path: str, section: str) -> int | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) == 3 and parts[0] == section and parts[1].isdigit() and parts[2] == "cv":
        return int(parts[1])
    return None


def interview_token_path(path: str) -> str | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) == 2 and parts[0] == "interview" and re.fullmatch(r"[A-Za-z0-9_-]{20,200}", parts[1]):
        return parts[1]
    return None


def interview_api_path(path: str) -> tuple[str, str] | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if (
        len(parts) == 4
        and parts[0] == "api"
        and parts[1] == "interview"
        and re.fullmatch(r"[A-Za-z0-9_-]{20,200}", parts[2])
        and parts[3] in {"start", "turn", "complete"}
    ):
        return parts[2], parts[3]
    return None


def safe_download_name(value: str | None, fallback: str) -> str:
    name = (value or fallback).strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:160] or fallback


class DashboardDB:
    def __init__(self):
        self.db = RecruiterDatabase()
        self.db.init_schema()

    def close(self):
        self.db.close()

    def rows(self, query: str, params: tuple = ()):
        return self.db.rows(query, params)

    def one(self, query: str, params: tuple = ()):
        return self.db.one(query, params)

    def execute(self, query: str, params: tuple = ()):
        self.db.execute(query, params)


class RecruiterDashboardHandler(BaseHTTPRequestHandler):
    server_version = "RecruiterDashboard/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path != "/" and parsed.path.endswith("/"):
                target = parsed.path.rstrip("/")
                if parsed.query:
                    target = f"{target}?{parsed.query}"
                self.redirect(target)
                return
            requirement_id = detail_path(parsed.path, "requirements")
            candidate_id = detail_path(parsed.path, "candidates")
            application_id = detail_path(parsed.path, "applications")
            event_id = detail_path(parsed.path, "events")
            candidate_cv_id = download_path(parsed.path, "candidates")
            application_cv_id = download_path(parsed.path, "applications")
            interview_token = interview_token_path(parsed.path)

            if parsed.path == "/":
                self.render_page("overview", self.render_overview())
            elif interview_token:
                self.render_public_interview(interview_token)
            elif candidate_cv_id is not None:
                self.download_candidate_cv(candidate_cv_id)
            elif application_cv_id is not None:
                self.download_application_cv(application_cv_id)
            elif requirement_id is not None:
                self.render_page("requirement_detail", self.render_requirement_detail(requirement_id))
            elif parsed.path == "/requirements":
                self.render_page("requirements", self.render_requirements(query))
            elif candidate_id is not None:
                self.render_page("candidate_detail", self.render_candidate_detail(candidate_id))
            elif parsed.path == "/candidates":
                self.render_page("candidates", self.render_candidates(query))
            elif application_id is not None:
                self.render_page("application_detail", self.render_application_detail(application_id))
            elif parsed.path == "/applications":
                self.render_page("applications", self.render_applications(query))
            elif event_id is not None:
                self.render_page("event_detail", self.render_event_detail(event_id))
            elif parsed.path == "/events":
                self.render_page("events", self.render_events(query))
            else:
                self.send_error(404, "Page not found")
        except Exception as exc:
            self.render_error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            interview_api = interview_api_path(parsed.path)
            if interview_api:
                token, action = interview_api
                payload = self.read_json_body()
                if action == "start":
                    self.api_interview_start(token)
                elif action == "turn":
                    self.api_interview_turn(token, payload)
                else:
                    self.api_interview_complete(token, payload)
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            form = {key: values[0] if values else "" for key, values in parse_qs(payload).items()}

            if parsed.path == "/requirements":
                self.create_requirement(form)
                self.redirect("/requirements")
            elif parsed.path == "/requirements/status":
                self.update_requirement_status(form)
                self.redirect("/requirements")
            elif parsed.path == "/applications/status":
                self.update_application_status(form)
                self.redirect("/applications")
            elif parsed.path == "/applications/hr-approve":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                send_interview_request_after_hr_approval(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/post-interview-approve":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                send_final_hr_round_request(application_id, approved_by_hr=True)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/post-interview-reject":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                send_interview_rejection(application_id, "After HR review of the interview, we will not be moving ahead with the next round at this time.")
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/send-interview-link":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                send_interview_link_for_application(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/send-teams-link":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                send_teams_link_for_application(application_id)
                self.redirect(f"/applications/{application_id}")
            else:
                self.send_error(404, "Page not found")
        except Exception as exc:
            self.render_error(exc)

    def db(self) -> DashboardDB:
        return DashboardDB()

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def application_for_interview_token(self, token: str) -> dict | None:
        database = self.db()
        try:
            return database.db.application_by_interview_token(token)
        finally:
            database.close()

    def web_interview_questions(self, application: dict) -> list[str]:
        fallback = [
            "Tell me briefly about your relevant experience.",
            "Describe one project you worked on and your role.",
            "What was one challenge you solved in that project?",
            "How do you handle mistakes or bugs in your work?",
            "Why are you interested in this role?",
        ]
        llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1200))
        response = llm.invoke(
            f"""
Return one valid JSON object only.
Create {RECRUITER_INTERVIEW_QUESTION_COUNT} short interview questions.
Use the CV and job description.
Questions must be conversational, easy-to-medium level, and under 15 spoken words.
Do not ask trick questions.

JSON schema:
{{
  "questions": ["question"]
}}

Context:
{json.dumps({
    "role": application.get("requirement_position") or application.get("matched_position") or application.get("detected_position"),
    "job_description": (application.get("job_description") or "")[:5000],
    "cv_summary": application.get("cv_summary") or application.get("ai_short_description"),
    "cv_text": (application.get("raw_cv_text") or "")[:7000],
    "screening_details": application.get("screening_details"),
}, default=str)}
"""
        ).content
        try:
            questions = json.loads(extract_json_object(response)).get("questions") or []
        except Exception:
            questions = []
        questions = [" ".join(str(question).split()) for question in questions if str(question).strip()]
        return (questions or fallback)[:RECRUITER_INTERVIEW_QUESTION_COUNT]

    def web_interview_report(self, application: dict, transcript: list[dict]) -> dict:
        llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1800))
        response = llm.invoke(
            f"""
Return one valid JSON object only.
You are an HR technical interviewer. Evaluate this browser voice interview fairly.
Use only the candidate answers, CV, and JD context provided.

JSON schema:
{{
  "overall_score": 0,
  "technical_score": 0,
  "communication_score": 0,
  "role_fit_score": 0,
  "recommendation": "strong_hire/hire/hold/reject",
  "summary": "short HR summary",
  "plus_points": [],
  "negative_points": [],
  "question_reviews": [
    {{"question": "string", "answer_summary": "string", "score": 0, "notes": "string"}}
  ],
  "camera_monitoring": {{
    "available": false,
    "eye_movement_summary": "Not captured in this browser voice interview mode.",
    "unusual_activity": []
  }},
  "final_notes_for_hr": "string"
}}

Application context:
{json.dumps({
    "application_id": application.get("id"),
    "candidate_name": application.get("full_name"),
    "candidate_email": application.get("candidate_email") or application.get("source_email"),
    "role": application.get("requirement_position") or application.get("matched_position") or application.get("detected_position"),
    "job_description": (application.get("job_description") or "")[:5000],
    "cv_summary": application.get("cv_summary") or application.get("ai_short_description"),
    "cv_text": (application.get("raw_cv_text") or "")[:7000],
    "screening_details": application.get("screening_details"),
}, default=str)}

Interview transcript:
{json.dumps(transcript, default=str)}
"""
        ).content
        try:
            report = json.loads(extract_json_object(response))
        except Exception:
            report = {
                "overall_score": 0,
                "technical_score": 0,
                "communication_score": 0,
                "role_fit_score": 0,
                "recommendation": "hold",
                "summary": "Interview completed, but the AI report could not be parsed.",
                "plus_points": [],
                "negative_points": ["Report parsing failed."],
                "question_reviews": [],
                "camera_monitoring": {
                    "available": False,
                    "eye_movement_summary": "Not captured in this browser voice interview mode.",
                    "unusual_activity": [],
                },
                "final_notes_for_hr": "Please review the transcript manually.",
            }
        report["application_id"] = application.get("id")
        report["question_count"] = len(transcript)
        report["transcript"] = transcript
        report["interview_mode"] = "browser_voice_link"
        return report

    def web_interview_turn_decision(self, application: dict, session: dict, answer: str) -> dict:
        current_question = session["current_question"]
        current_index = session["current_index"]
        max_questions = len(session["questions"])
        transcript = session["transcript"]
        answer_text = (answer or "").strip()
        lowered = answer_text.lower()
        word_count = len(re.findall(r"\b\w+\b", lowered))
        repeat_phrases = ["repeat", "come again", "didn't hear", "did not hear", "say that again", "can you repeat"]
        skip_phrases = ["skip", "next question", "move on", "go next", "ask next", "leave this", "i don't know", "no idea"]
        if not answer_text:
            return {
                "action": "clarify",
                "reply": "I could not hear that clearly. Could you answer once more?",
                "question": current_question,
            }
        if any(phrase in lowered for phrase in skip_phrases):
            acknowledgement = random.choice(["No problem.", "That's okay.", "Alright."])
            if current_index + 1 >= max_questions:
                return {"action": "complete", "reply": closing_interview_reply()}
            next_question = session["questions"][current_index + 1]
            return {
                "action": "next_question",
                "reply": acknowledgement,
                "question": next_question,
                "question_number": current_index + 2,
            }
        repeat_requested = any(phrase in lowered for phrase in repeat_phrases)
        mostly_repeat_request = word_count <= 12 or re.search(
            r"(repeat|come again|say that again|can you repeat)\??$",
            lowered,
        )
        if repeat_requested and mostly_repeat_request:
            return {
                "action": "repeat",
                "reply": f"Sure, let me repeat that. {current_question}",
                "question": current_question,
            }

        llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 900))
        response = llm.invoke(
            f"""
Return one valid JSON object only.
You are the same AI HR technical interviewer, now running inside a browser video interview room.
Decide the next conversational action after the candidate answered.

Rules:
- Be human, brief, and professional.
- Do not repeat the question unless the candidate asks.
- Ask at most one short follow-up for this main question.
- If the answer is weak but attempted, either ask one useful follow-up or move on.
- If enough has been answered, acknowledge and move to the next main question.
- If this is the last question and no follow-up is needed, complete the interview warmly.

Allowed action:
follow_up, next_question, complete, clarify

JSON schema:
{{
  "action": "follow_up/next_question/complete/clarify",
  "reply": "short spoken acknowledgement or clarification",
  "question": "next or follow-up question, or null",
  "reason": "short reason"
}}

Application context:
{json.dumps({
    "role": application.get("requirement_position") or application.get("matched_position") or application.get("detected_position"),
    "job_description": (application.get("job_description") or "")[:5000],
    "cv_summary": application.get("cv_summary") or application.get("ai_short_description"),
    "cv_text": (application.get("raw_cv_text") or "")[:7000],
    "screening_details": application.get("screening_details"),
}, default=str)}

Current question number:
{current_index + 1} of {max_questions}

Current question:
{current_question}

Candidate answer:
{answer_text}

Previous transcript:
{json.dumps(transcript, default=str)}
"""
        ).content
        try:
            decision = json.loads(extract_json_object(response))
        except Exception:
            decision = {}

        action = (decision.get("action") or "").lower()
        reply = " ".join(str(decision.get("reply") or "").split())
        question = " ".join(str(decision.get("question") or "").split())
        already_followed_up = bool(session.get("followup_for_current"))

        if action == "follow_up" and question and not already_followed_up:
            session["followup_for_current"] = True
            return {
                "action": "follow_up",
                "reply": reply or random.choice(["Thanks, let me ask one follow-up.", "Got it. One quick follow-up."]),
                "question": question,
            }
        if action == "complete":
            return {
                "action": "complete",
                "reply": closing_interview_reply(),
            }
        if action == "clarify":
            return {
                "action": "clarify",
                "reply": reply or "Could you explain that a little differently?",
                "question": current_question,
            }
        if current_index + 1 >= max_questions:
            return {
                "action": "complete",
                "reply": closing_interview_reply(),
            }
        next_question = session["questions"][current_index + 1]
        return {
            "action": "next_question",
            "reply": reply or random.choice(["Understood.", "Thanks for explaining.", "Got it.", "That makes sense."]),
            "question": next_question,
            "question_number": current_index + 2,
        }

    def api_interview_start(self, token: str):
        application = self.application_for_interview_token(token)
        if not application:
            self.send_json({"error": "Interview link not found."}, status=404)
            return
        report = json_object(application.get("interview_report"))
        if report and application.get("interview_completed_at"):
            self.send_json({"error": "This interview has already been completed."}, status=409)
            return
        database = self.db()
        try:
            database.db.mark_interview_started(application["id"])
        finally:
            database.close()
        questions = self.web_interview_questions(application)
        WEB_INTERVIEW_SESSIONS[token] = {
            "application_id": application["id"],
            "questions": questions,
            "current_index": 0,
            "current_question": questions[0] if questions else "",
            "transcript": [],
            "followup_for_current": False,
            "last_client_turn_id": 0,
        }
        self.send_json(
            {
                "application_id": application["id"],
                "candidate_name": application.get("full_name") or "there",
                "role": application.get("requirement_position") or application.get("matched_position") or application.get("detected_position") or "this role",
                "question": questions[0] if questions else "",
                "question_number": 1,
                "total_questions": len(questions),
            }
        )

    def api_interview_turn(self, token: str, payload: dict):
        application = self.application_for_interview_token(token)
        if not application:
            self.send_json({"error": "Interview link not found."}, status=404)
            return
        session = WEB_INTERVIEW_SESSIONS.get(token)
        if not session:
            questions = self.web_interview_questions(application)
            session = {
                "application_id": application["id"],
                "questions": questions,
                "current_index": 0,
                "current_question": questions[0] if questions else "",
                "transcript": [],
                "followup_for_current": False,
                "last_client_turn_id": 0,
            }
            WEB_INTERVIEW_SESSIONS[token] = session
        answer = str(payload.get("answer") or "").strip()
        try:
            client_turn_id = int(payload.get("turn_id") or 0)
        except (TypeError, ValueError):
            client_turn_id = 0
        last_client_turn_id = int(session.get("last_client_turn_id") or 0)
        if client_turn_id and client_turn_id <= last_client_turn_id:
            self.send_json(
                {
                    "action": "ignored",
                    "reply": "",
                    "question": session.get("current_question"),
                    "question_number": int(session.get("current_index") or 0) + 1,
                    "reason": "duplicate_or_stale_turn",
                }
            )
            return
        if client_turn_id:
            session["last_client_turn_id"] = client_turn_id
        decision = self.web_interview_turn_decision(application, session, answer)
        action = decision.get("action")
        if action not in {"clarify", "repeat"}:
            session["transcript"].append(
                {
                    "question_number": session["current_index"] + 1,
                    "question": session["current_question"],
                    "answer": answer,
                    "status": "answered" if answer else "empty",
                }
            )
        if action == "next_question":
            session["current_index"] += 1
            session["current_question"] = decision.get("question") or session["questions"][session["current_index"]]
            session["followup_for_current"] = False
        elif action == "follow_up":
            session["current_question"] = decision.get("question") or session["current_question"]
        elif action == "complete":
            report = self.web_interview_report(application, session["transcript"])
            database = self.db()
            try:
                database.db.update_interview_report(application["id"], report)
            finally:
                database.close()
            notify_post_interview_outcome_async(application["id"], report)
            WEB_INTERVIEW_SESSIONS.pop(token, None)
            decision["report_saved"] = True
        self.send_json(decision)

    def api_interview_complete(self, token: str, payload: dict):
        application = self.application_for_interview_token(token)
        if not application:
            self.send_json({"error": "Interview link not found."}, status=404)
            return
        transcript = payload.get("transcript") or []
        if not isinstance(transcript, list) or not transcript:
            self.send_json({"error": "Interview transcript is required."}, status=400)
            return
        cleaned = []
        for index, item in enumerate(transcript, start=1):
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "question_number": index,
                    "question": str(item.get("question") or "").strip(),
                    "answer": str(item.get("answer") or "").strip(),
                    "status": "answered" if str(item.get("answer") or "").strip() else "empty",
                }
            )
        report = self.web_interview_report(application, cleaned)
        database = self.db()
        try:
            database.db.update_interview_report(application["id"], report)
        finally:
            database.close()
        notify_post_interview_outcome_async(application["id"], report)
        self.send_json({"ok": True, "report": report})

    def create_requirement(self, form: dict[str, str]):
        database = self.db()
        try:
            values = (
                form.get("position_title", "").strip(),
                parse_decimal(form.get("experience_min_years")),
                parse_decimal(form.get("experience_max_years")),
                parse_decimal(form.get("budget_min")),
                parse_decimal(form.get("budget_max")),
                form.get("currency", "INR").strip() or "INR",
                form.get("job_description", "").strip(),
                parse_bool(form.get("urgently_required")),
                parse_int(form.get("needed_within_days")),
                form.get("status", "open").strip() or "open",
            )
            if database.db.is_mssql():
                existing = database.one(
                    "SELECT id FROM recruitment_requirements WHERE LOWER(position_title) = LOWER(%s) LIMIT 1",
                    (values[0],),
                )
                if existing:
                    database.execute(
                        """
                        UPDATE recruitment_requirements
                        SET
                            experience_min_years = %s,
                            experience_max_years = %s,
                            budget_min = %s,
                            budget_max = %s,
                            currency = %s,
                            job_description = %s,
                            urgently_required = %s,
                            needed_within_days = %s,
                            status = %s,
                            updated_at = SYSDATETIMEOFFSET()
                        WHERE id = %s
                        """,
                        values[1:] + (existing["id"],),
                    )
                    return
                database.execute(
                    """
                    INSERT INTO recruitment_requirements
                    (
                        position_title, experience_min_years, experience_max_years,
                        budget_min, budget_max, currency, job_description,
                        urgently_required, needed_within_days, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
                return

            database.execute(
                """
                INSERT INTO recruitment_requirements
                (
                    position_title, experience_min_years, experience_max_years,
                    budget_min, budget_max, currency, job_description,
                    urgently_required, needed_within_days, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT ((LOWER(position_title)))
                DO UPDATE SET
                    experience_min_years = EXCLUDED.experience_min_years,
                    experience_max_years = EXCLUDED.experience_max_years,
                    budget_min = EXCLUDED.budget_min,
                    budget_max = EXCLUDED.budget_max,
                    currency = EXCLUDED.currency,
                    job_description = EXCLUDED.job_description,
                    urgently_required = EXCLUDED.urgently_required,
                    needed_within_days = EXCLUDED.needed_within_days,
                    status = EXCLUDED.status,
                    updated_at = NOW()
                """,
                values,
            )
        finally:
            database.close()

    def update_requirement_status(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruitment_requirements
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (form.get("status", "closed"), parse_int(form.get("id"))),
            )
        finally:
            database.close()

    def update_application_status(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruiter_applications
                SET application_status = %s
                WHERE id = %s
                """,
                (form.get("application_status", "reviewed"), parse_int(form.get("id"))),
            )
        finally:
            database.close()

    def download_application_cv(self, application_id: int):
        database = self.db()
        try:
            row = database.one(
                """
                SELECT
                    ra.id, ra.attachment_filename, ra.attachment_payload,
                    rc.full_name, rc.raw_cv_text
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                WHERE ra.id = %s
                """,
                (application_id,),
            )
        finally:
            database.close()
        if not row:
            self.send_error(404, "CV not found")
            return
        self.send_cv_download(row, f"application-{application_id}-cv.txt")

    def download_candidate_cv(self, candidate_id: int):
        database = self.db()
        try:
            row = database.one(
                """
                SELECT
                    ra.id, ra.attachment_filename, ra.attachment_payload,
                    rc.full_name, rc.raw_cv_text
                FROM recruiter_candidates rc
                LEFT JOIN recruiter_applications ra ON ra.candidate_id = rc.id
                WHERE rc.id = %s
                ORDER BY ra.created_at DESC
                LIMIT 1
                """,
                (candidate_id,),
            )
        finally:
            database.close()
        if not row:
            self.send_error(404, "CV not found")
            return
        self.send_cv_download(row, f"candidate-{candidate_id}-cv.txt")

    def send_cv_download(self, row: dict, fallback_name: str):
        payload = row.get("attachment_payload")
        filename = row.get("attachment_filename")
        if payload:
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            elif not isinstance(payload, bytes):
                payload = bytes(payload)
            download_name = safe_download_name(filename, fallback_name)
            content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        else:
            text = row.get("raw_cv_text")
            if not text:
                self.send_error(404, "No CV file or extracted CV text is available for this record")
                return
            candidate_name = safe_download_name(row.get("full_name"), "candidate")
            download_name = safe_download_name(f"{candidate_name}-cv.txt", fallback_name)
            payload = str(text).encode("utf-8")
            content_type = "text/plain; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(payload)

    def render_overview(self) -> str:
        database = self.db()
        try:
            summary = database.one(
                """
                SELECT
                    (SELECT COUNT(*) FROM recruitment_requirements WHERE LOWER(status) = 'open') AS open_roles,
                    (SELECT COUNT(*) FROM recruiter_candidates) AS candidates,
                    (SELECT COUNT(*) FROM recruiter_applications) AS applications,
                    (SELECT COUNT(*) FROM recruiter_applications WHERE application_status = 'matched_requirement') AS matched,
                    (SELECT COUNT(*) FROM recruiter_applications WHERE application_status = 'no_open_requirement') AS saved_for_later,
                    (SELECT COUNT(*) FROM recruiter_applications WHERE application_status = 'withdrawn') AS withdrawn,
                    (SELECT ROUND(AVG(ats_score), 1) FROM recruiter_applications WHERE ats_score IS NOT NULL) AS avg_ats,
                    (SELECT ROUND(AVG(jd_match_score), 1) FROM recruiter_applications WHERE jd_match_score IS NOT NULL) AS avg_jd
                """
            )
            recent = database.rows(
                """
                SELECT
                    ra.id, ra.application_status, ra.ats_score, ra.jd_match_score,
                    ra.detected_position, ra.matched_position, ra.ai_short_description,
                    ra.created_at, rc.full_name, rc.candidate_email, rc.source_email
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                ORDER BY ra.created_at DESC
                LIMIT 8
                """
            )
            urgent = database.rows(
                """
                SELECT id, position_title, needed_within_days, status, urgently_required
                FROM recruitment_requirements
                WHERE LOWER(status) = 'open'
                ORDER BY urgently_required DESC, needed_within_days NULLS LAST, created_at DESC
                LIMIT 6
                """
            )
        finally:
            database.close()

        cards = [
            ("Open Roles", summary["open_roles"], "Roles accepting CVs"),
            ("Candidates", summary["candidates"], "Profiles saved"),
            ("Applications", summary["applications"], "CVs processed"),
            ("Matched", summary["matched"], "Linked to open roles"),
            ("Saved Later", summary["saved_for_later"], "No open role now"),
            ("Avg ATS", score(summary["avg_ats"]), "Across applications"),
        ]
        card_html = "".join(
            f"""
            <article class="metric">
                <span>{html_escape(label)}</span>
                <strong>{html_escape(value)}</strong>
                <small>{html_escape(detail)}</small>
            </article>
            """
            for label, value, detail in cards
        )

        urgent_html = "".join(
            f"""
            <tr>
                <td><a href="/requirements/{html_escape(row["id"])}">{html_escape(row["position_title"])}</a></td>
                <td>{status_badge(row["status"])}</td>
                <td>{'Yes' if row["urgently_required"] else 'No'}</td>
                <td>{html_escape(row["needed_within_days"] or '-')}</td>
            </tr>
            """
            for row in urgent
        ) or '<tr><td colspan="4" class="empty">No open requirements yet.</td></tr>'

        recent_html = "".join(
            f"""
            <tr>
                <td>
                    <a href="/applications/{html_escape(row["id"])}"><strong>{html_escape(row["full_name"] or "Unnamed candidate")}</strong></a>
                    <small>{html_escape(row["candidate_email"] or row["source_email"] or "")}</small>
                </td>
                <td>{html_escape(row["matched_position"] or row["detected_position"] or "-")}</td>
                <td>{status_badge(row["application_status"])}</td>
                <td>{score(row["ats_score"])}</td>
                <td>{score(row["jd_match_score"])}</td>
                <td>{html_escape(row["ai_short_description"] or "-")}</td>
                <td>{date_text(row["created_at"])}</td>
            </tr>
            """
            for row in recent
        ) or '<tr><td colspan="7" class="empty">No applications processed yet.</td></tr>'

        return f"""
        <section class="metrics">{card_html}</section>
        <section class="grid-two">
            <div class="panel">
                <div class="panel-head">
                    <h2>Open Requirements</h2>
                    <a class="link-button" href="/requirements">Manage</a>
                </div>
                <table>
                    <thead><tr><th>Position</th><th>Status</th><th>Urgent</th><th>Days</th></tr></thead>
                    <tbody>{urgent_html}</tbody>
                </table>
            </div>
            <div class="panel">
                <div class="panel-head">
                    <h2>Health</h2>
                    <a class="link-button" href="/events">Events</a>
                </div>
                <div class="health">
                    <div><span>Withdrawn</span><strong>{html_escape(summary["withdrawn"])}</strong></div>
                    <div><span>Average JD Match</span><strong>{score(summary["avg_jd"])}</strong></div>
                    <div><span>Mailbox Mode</span><strong>DB Dashboard</strong></div>
                </div>
            </div>
        </section>
        <section class="panel">
            <div class="panel-head">
                <h2>Recent Applications</h2>
                <a class="link-button" href="/applications">View all</a>
            </div>
            <table>
                <thead><tr><th>Candidate</th><th>Role</th><th>Status</th><th>ATS</th><th>JD</th><th>AI Summary</th><th>Created</th></tr></thead>
                <tbody>{recent_html}</tbody>
            </table>
        </section>
        """

    def render_requirements(self, query: dict[str, list[str]]) -> str:
        q = (query.get("q", [""])[0] or "").strip()
        params: tuple = ()
        where = ""
        if q:
            where = "WHERE LOWER(position_title) LIKE %s OR LOWER(job_description) LIKE %s"
            term = f"%{q.lower()}%"
            params = (term, term)

        database = self.db()
        try:
            rows = database.rows(
                f"""
                SELECT *
                FROM recruitment_requirements
                {where}
                ORDER BY LOWER(status) = 'open' DESC, urgently_required DESC, updated_at DESC
                LIMIT 100
                """,
                params,
            )
        finally:
            database.close()

        table_rows = "".join(
            f"""
            <tr>
                <td><a href="/requirements/{html_escape(row["id"])}"><strong>{html_escape(row["position_title"])}</strong></a><small>{html_escape(row["currency"] or "INR")}</small></td>
                <td>{html_escape(row["experience_min_years"] or "-")} - {html_escape(row["experience_max_years"] or "-")} yrs</td>
                <td>{money(row["budget_min"], row["currency"])} - {money(row["budget_max"], row["currency"])}</td>
                <td>{'Yes' if row["urgently_required"] else 'No'}</td>
                <td>{html_escape(row["needed_within_days"] or "-")}</td>
                <td>{status_badge(row["status"])}</td>
                <td class="description">{html_escape(row["job_description"])}</td>
                <td>
                    <div class="action-stack">
                    <a class="link-button" href="/requirements/{html_escape(row["id"])}">View</a>
                    <form method="post" action="/requirements/status" class="inline-form">
                        <input type="hidden" name="id" value="{html_escape(row["id"])}">
                        <input type="hidden" name="status" value="{'closed' if row["status"] == 'open' else 'open'}">
                        <button type="submit">{'Close' if row["status"] == 'open' else 'Open'}</button>
                    </form>
                    </div>
                </td>
            </tr>
            """
            for row in rows
        ) or '<tr><td colspan="8" class="empty">No requirements found.</td></tr>'

        return f"""
        {self.search_form("/requirements", q, "Search roles or job descriptions")}
        <section class="panel">
            <div class="panel-head"><h2>Add Requirement</h2></div>
            <form method="post" action="/requirements" class="requirement-form">
                <label>Position<input name="position_title" required placeholder="Python Developer"></label>
                <label>Min Exp<input name="experience_min_years" type="number" step="0.1"></label>
                <label>Max Exp<input name="experience_max_years" type="number" step="0.1"></label>
                <label>Budget Min<input name="budget_min" type="number"></label>
                <label>Budget Max<input name="budget_max" type="number"></label>
                <label>Currency<input name="currency" value="INR"></label>
                <label>Needed Days<input name="needed_within_days" type="number"></label>
                <label>Status<select name="status"><option value="open">Open</option><option value="closed">Closed</option></select></label>
                <label class="check"><input name="urgently_required" type="checkbox"> Urgent</label>
                <label class="wide">Job Description<textarea name="job_description" required rows="4"></textarea></label>
                <button type="submit">Save Requirement</button>
            </form>
        </section>
        <section class="panel">
            <div class="panel-head"><h2>Requirements</h2><span>{len(rows)} shown</span></div>
            <table>
                <thead><tr><th>Position</th><th>Experience</th><th>Budget</th><th>Urgent</th><th>Days</th><th>Status</th><th>Job Description</th><th>Action</th></tr></thead>
                <tbody>{table_rows}</tbody>
            </table>
        </section>
        """

    def render_candidates(self, query: dict[str, list[str]]) -> str:
        q = (query.get("q", [""])[0] or "").strip()
        params: tuple = ()
        where = ""
        if q:
            where = """
            WHERE LOWER(COALESCE(full_name, '')) LIKE %s
               OR LOWER(COALESCE(candidate_email, '')) LIKE %s
               OR LOWER(COALESCE(source_email, '')) LIKE %s
               OR LOWER(COALESCE(current_title, '')) LIKE %s
            """
            term = f"%{q.lower()}%"
            params = (term, term, term, term)

        database = self.db()
        try:
            rows = database.rows(
                f"""
                SELECT *
                FROM recruiter_candidates
                {where}
                ORDER BY created_at DESC
                LIMIT 100
                """,
                params,
            )
        finally:
            database.close()

        table_rows = "".join(
            f"""
            <tr>
                <td><a href="/candidates/{html_escape(row["id"])}"><strong>{html_escape(row["full_name"] or "Unnamed candidate")}</strong></a><small>{html_escape(row["candidate_email"] or row["source_email"] or "")}</small></td>
                <td>{html_escape(row["current_title"] or "-")}<small>{html_escape(row["current_company"] or "")}</small></td>
                <td>{html_escape(row["total_experience_years"] or "-")}</td>
                <td>{score(row["ats_score"])}</td>
                <td>{list_text(row["skills"])}</td>
                <td>{html_escape(row["cv_summary"] or "-")}</td>
                <td>{status_badge(row["submission_type"])}</td>
                <td>{date_text(row["created_at"])}</td>
                <td>
                    <div class="action-stack">
                        <a class="link-button" href="/candidates/{html_escape(row["id"])}">View</a>
                        <a class="link-button" href="/candidates/{html_escape(row["id"])}/cv">Download CV</a>
                    </div>
                </td>
            </tr>
            """
            for row in rows
        ) or '<tr><td colspan="9" class="empty">No candidates found.</td></tr>'

        return f"""
        {self.search_form("/candidates", q, "Search candidate, email, or title")}
        <section class="panel">
            <div class="panel-head"><h2>Candidates</h2><span>{len(rows)} shown</span></div>
            <table>
                <thead><tr><th>Candidate</th><th>Current Role</th><th>Exp</th><th>ATS</th><th>Skills</th><th>AI Summary</th><th>Source</th><th>Created</th><th>Action</th></tr></thead>
                <tbody>{table_rows}</tbody>
            </table>
        </section>
        """

    def render_applications(self, query: dict[str, list[str]]) -> str:
        q = (query.get("q", [""])[0] or "").strip()
        params: tuple = ()
        where = ""
        if q:
            where = """
            WHERE LOWER(COALESCE(rc.full_name, '')) LIKE %s
               OR LOWER(COALESCE(ra.candidate_email, '')) LIKE %s
               OR LOWER(COALESCE(ra.detected_position, '')) LIKE %s
               OR LOWER(COALESCE(ra.matched_position, '')) LIKE %s
               OR LOWER(COALESCE(ra.application_status, '')) LIKE %s
            """
            term = f"%{q.lower()}%"
            params = (term, term, term, term, term)

        database = self.db()
        try:
            rows = database.rows(
                f"""
                SELECT
                    ra.*, rc.full_name, rc.current_title, rr.position_title AS requirement_position
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
                {where}
                ORDER BY ra.created_at DESC
                LIMIT 100
                """,
                params,
            )
        finally:
            database.close()

        table_rows = "".join(
            f"""
            <tr>
                <td><a href="/applications/{html_escape(row["id"])}"><strong>{html_escape(row["full_name"] or "Unnamed candidate")}</strong></a><small>{html_escape(row["candidate_email"] or row["source_email"] or "")}</small></td>
                <td>{html_escape(row["matched_position"] or row["detected_position"] or row["requirement_position"] or "-")}</td>
                <td>{status_badge(row["application_status"])}</td>
                <td>{score(row["ats_score"])}</td>
                <td>{score(row["jd_match_score"])}</td>
                <td>{list_text(row["strengths"], 3)}</td>
                <td>{list_text(row["risks"], 3)}</td>
                <td>{html_escape(row["ai_short_description"] or "-")}</td>
                <td>
                    <div class="action-stack">
                    <a class="link-button" href="/applications/{html_escape(row["id"])}">View</a>
                    <a class="link-button" href="/applications/{html_escape(row["id"])}/cv">Download CV</a>
                    <form method="post" action="/applications/status" class="inline-form">
                        <input type="hidden" name="id" value="{html_escape(row["id"])}">
                        <select name="application_status">
                            {self.status_options(row["application_status"])}
                        </select>
                        <button type="submit">Update</button>
                    </form>
                    </div>
                </td>
            </tr>
            """
            for row in rows
        ) or '<tr><td colspan="9" class="empty">No applications found.</td></tr>'

        return f"""
        {self.search_form("/applications", q, "Search application, role, email, or status")}
        <section class="panel">
            <div class="panel-head"><h2>Applications</h2><span>{len(rows)} shown</span></div>
            <table>
                <thead><tr><th>Candidate</th><th>Role</th><th>Status</th><th>ATS</th><th>JD</th><th>Strengths</th><th>Risks</th><th>AI Summary</th><th>Action</th></tr></thead>
                <tbody>{table_rows}</tbody>
            </table>
        </section>
        """

    def render_events(self, query: dict[str, list[str]]) -> str:
        q = (query.get("q", [""])[0] or "").strip()
        params: tuple = ()
        where = ""
        if q:
            where = """
            WHERE LOWER(COALESCE(source_email, '')) LIKE %s
               OR LOWER(COALESCE(email_subject, '')) LIKE %s
               OR LOWER(COALESCE(event_type, '')) LIKE %s
            """
            term = f"%{q.lower()}%"
            params = (term, term, term)

        database = self.db()
        try:
            rows = database.rows(
                f"""
                SELECT *
                FROM recruiter_email_events
                {where}
                ORDER BY created_at DESC
                LIMIT 150
                """,
                params,
            )
        finally:
            database.close()

        table_rows = "".join(
            f"""
            <tr>
                <td>{date_text(row["created_at"])}</td>
                <td>{status_badge(row["event_type"])}</td>
                <td>{html_escape(row["source_email"] or "-")}</td>
                <td>{html_escape(row["email_subject"] or "-")}</td>
                <td><pre>{html_escape(json.dumps(row["details"], indent=2, default=str))}</pre></td>
                <td><a class="link-button" href="/events/{html_escape(row["id"])}">View</a></td>
            </tr>
            """
            for row in rows
        ) or '<tr><td colspan="6" class="empty">No email events found.</td></tr>'

        return f"""
        {self.search_form("/events", q, "Search event, sender, or subject")}
        <section class="panel">
            <div class="panel-head"><h2>Email Events</h2><span>{len(rows)} shown</span></div>
            <table>
                <thead><tr><th>Time</th><th>Event</th><th>Sender</th><th>Subject</th><th>Details</th><th>Action</th></tr></thead>
                <tbody>{table_rows}</tbody>
            </table>
        </section>
        """

    def render_requirement_detail(self, requirement_id: int) -> str:
        database = self.db()
        try:
            row = database.one("SELECT * FROM recruitment_requirements WHERE id = %s", (requirement_id,))
            if not row:
                return self.not_found_panel("Requirement", "/requirements")
            applications = database.rows(
                """
                SELECT ra.id, ra.application_status, ra.ats_score, ra.jd_match_score,
                       ra.created_at, rc.full_name, rc.candidate_email, rc.source_email
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                WHERE ra.requirement_id = %s
                ORDER BY ra.created_at DESC
                LIMIT 50
                """,
                (requirement_id,),
            )
        finally:
            database.close()

        application_rows = "".join(
            f"""
            <tr>
                <td><a href="/applications/{html_escape(app["id"])}">{html_escape(app["full_name"] or "Unnamed candidate")}</a><small>{html_escape(app["candidate_email"] or app["source_email"] or "")}</small></td>
                <td>{status_badge(app["application_status"])}</td>
                <td>{score(app["ats_score"])}</td>
                <td>{score(app["jd_match_score"])}</td>
                <td>{date_text(app["created_at"])}</td>
            </tr>
            """
            for app in applications
        ) or '<tr><td colspan="5" class="empty">No applications linked to this requirement yet.</td></tr>'

        fields = [
            ("Position", row["position_title"]),
            ("Status", status_badge(row["status"]), "html"),
            ("Experience", f'{row["experience_min_years"] or "-"} - {row["experience_max_years"] or "-"} years'),
            ("Budget", f'{money(row["budget_min"], row["currency"])} - {money(row["budget_max"], row["currency"])}'),
            ("Currency", row["currency"]),
            ("Urgently Required", "Yes" if row["urgently_required"] else "No"),
            ("Needed Within Days", row["needed_within_days"]),
            ("Created", date_text(row["created_at"])),
            ("Updated", date_text(row["updated_at"])),
            ("Job Description", row["job_description"], "pre-wide"),
        ]
        return f"""
        {self.back_link("/requirements", "Back to requirements")}
        <section class="panel">
            <div class="panel-head"><h2>{html_escape(row["position_title"])}</h2>{status_badge(row["status"])}</div>
            {self.detail_grid(fields)}
        </section>
        <section class="panel">
            <div class="panel-head"><h2>Linked Applications</h2><span>{len(applications)} shown</span></div>
            <table>
                <thead><tr><th>Candidate</th><th>Status</th><th>ATS</th><th>JD</th><th>Created</th></tr></thead>
                <tbody>{application_rows}</tbody>
            </table>
        </section>
        """

    def render_candidate_detail(self, candidate_id: int) -> str:
        database = self.db()
        try:
            row = database.one("SELECT * FROM recruiter_candidates WHERE id = %s", (candidate_id,))
            if not row:
                return self.not_found_panel("Candidate", "/candidates")
            applications = database.rows(
                """
                SELECT ra.*, rr.position_title AS requirement_position
                FROM recruiter_applications ra
                LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
                WHERE ra.candidate_id = %s
                ORDER BY ra.created_at DESC
                """,
                (candidate_id,),
            )
        finally:
            database.close()

        application_rows = "".join(
            f"""
            <tr>
                <td><a href="/applications/{html_escape(app["id"])}">{html_escape(app["matched_position"] or app["detected_position"] or app["requirement_position"] or "-")}</a></td>
                <td>{status_badge(app["application_status"])}</td>
                <td>{score(app["ats_score"])}</td>
                <td>{score(app["jd_match_score"])}</td>
                <td>{html_escape(app["attachment_filename"] or "-")}</td>
                <td><a class="link-button" href="/applications/{html_escape(app["id"])}/cv">Download CV</a></td>
                <td>{date_text(app["created_at"])}</td>
            </tr>
            """
            for app in applications
        ) or '<tr><td colspan="7" class="empty">No applications found for this candidate.</td></tr>'

        fields = [
            ("Full Name", row["full_name"]),
            ("Candidate Email", row["candidate_email"]),
            ("Source Email", row["source_email"]),
            ("Referrer Email", row["referrer_email"]),
            ("Submission Type", status_badge(row["submission_type"]), "html"),
            ("Phone", row["phone"]),
            ("Location", row["location"]),
            ("LinkedIn", self.link_value(row["linkedin_url"]), "html"),
            ("Portfolio", self.link_value(row["portfolio_url"]), "html"),
            ("Current Title", row["current_title"]),
            ("Current Company", row["current_company"]),
            ("Total Experience", row["total_experience_years"]),
            ("ATS Score", score(row["ats_score"])),
            ("Created", date_text(row["created_at"])),
            ("Updated", date_text(row["updated_at"])),
            ("Skills", row["skills"], "json"),
            ("Education", row["education"], "json"),
            ("Work History", row["work_history"], "json"),
            ("Certifications", row["certifications"], "json"),
            ("CV Summary", row["cv_summary"], "pre-wide"),
            ("AI Evaluation", row["ai_evaluation"], "json-wide"),
            ("Raw CV Text", row["raw_cv_text"], "pre-wide"),
        ]
        title = row["full_name"] or row["candidate_email"] or row["source_email"] or "Candidate"
        return f"""
        {self.back_link("/candidates", "Back to candidates")}
        <section class="panel">
            <div class="panel-head">
                <h2>{html_escape(title)}</h2>
                <div class="action-stack">
                    {status_badge(row["submission_type"])}
                    <a class="link-button" href="/candidates/{html_escape(candidate_id)}/cv">Download CV</a>
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        <section class="panel">
            <div class="panel-head"><h2>Applications</h2><span>{len(applications)} shown</span></div>
            <table>
                <thead><tr><th>Role</th><th>Status</th><th>ATS</th><th>JD</th><th>Attachment</th><th>CV</th><th>Created</th></tr></thead>
                <tbody>{application_rows}</tbody>
            </table>
        </section>
        """

    def render_application_detail(self, application_id: int) -> str:
        database = self.db()
        try:
            row = database.one(
                """
                SELECT
                    ra.*, rc.full_name, rc.current_title, rc.current_company, rc.phone,
                    rc.location, rc.linkedin_url, rc.portfolio_url,
                    rr.position_title AS requirement_position, rr.job_description, rr.currency
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
                WHERE ra.id = %s
                """,
                (application_id,),
            )
            if not row:
                return self.not_found_panel("Application", "/applications")
        finally:
            database.close()

        interview_report = json_object(row.get("interview_report"))
        interview_link = candidate_interview_url(row.get("interview_link_token")) if row.get("interview_link_token") else None
        fields = [
            ("Candidate", f'<a href="/candidates/{html_escape(row["candidate_id"])}">{html_escape(row["full_name"] or "Unnamed candidate")}</a>', "html"),
            ("Candidate Email", row["candidate_email"]),
            ("Source Email", row["source_email"]),
            ("Referrer Email", row["referrer_email"]),
            ("Submission Type", status_badge(row["submission_type"]), "html"),
            ("Status", status_badge(row["application_status"]), "html"),
            ("Detected Position", row["detected_position"]),
            ("Matched Position", row["matched_position"] or row["requirement_position"]),
            ("Requirement", self.requirement_link(row["requirement_id"], row["requirement_position"]), "html"),
            ("ATS Score", score(row["ats_score"])),
            ("JD Match Score", score(row["jd_match_score"])),
            ("Email Subject", row["email_subject"]),
            ("Email Message ID", row["email_message_id"]),
            ("Attachment", row["attachment_filename"]),
            ("Attachment SHA256", row["attachment_sha256"]),
            ("Current Salary", money(row.get("screening_current_salary"), row.get("currency") or "INR")),
            ("Expected Salary", money(row.get("screening_expected_salary"), row.get("currency") or "INR")),
            ("Current Location", row.get("screening_current_location")),
            ("Joining Duration Days", row.get("screening_joining_days")),
            ("Screening Details", row.get("screening_details"), "json-wide"),
            ("HR Escalation Reason", row.get("hr_escalation_reason"), "pre-wide"),
            ("HR Escalated At", date_text(row.get("hr_escalated_at"))),
            ("HR Approved At", date_text(row.get("hr_approved_at"))),
            ("Interview Availability", row.get("interview_availability"), "pre-wide"),
            ("Interview Scheduled At", date_text(row.get("interview_scheduled_at"))),
            ("Interview Link", interview_link),
            ("Interview Started At", date_text(row.get("interview_started_at"))),
            ("Interview Completed At", date_text(row.get("interview_completed_at"))),
            ("Teams Event ID", row.get("teams_event_id")),
            ("Teams Join URL", row.get("teams_join_url")),
            ("Interview Score", interview_report.get("overall_score")),
            ("Interview Recommendation", interview_report.get("recommendation")),
            ("Interview Summary", interview_report.get("summary"), "pre-wide"),
            ("Interview Plus Points", interview_report.get("plus_points"), "json"),
            ("Interview Negative Points", interview_report.get("negative_points"), "json"),
            ("Interview Report", row.get("interview_report"), "json-wide"),
            ("Received At", date_text(row["received_at"])),
            ("Created", date_text(row["created_at"])),
            ("Strengths", row["strengths"], "json"),
            ("Risks", row["risks"], "json"),
            ("Missing Requirements", row["missing_requirements"], "json"),
            ("AI Short Description", row["ai_short_description"], "pre-wide"),
            ("AI Evaluation", row["ai_evaluation"], "json-wide"),
            ("Job Description", row["job_description"], "pre-wide"),
        ]
        title = row["full_name"] or row["candidate_email"] or f"Application {application_id}"
        hr_approve_form = ""
        if (row["application_status"] or "").lower() == "hr_escalated":
            hr_approve_form = f"""
            <form method="post" action="/applications/hr-approve" class="inline-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Approve For Interview</button>
            </form>
            """
        post_interview_review_forms = ""
        if (row["application_status"] or "").lower() == "interview_on_hold_hr_review":
            post_interview_review_forms = f"""
            <form method="post" action="/applications/post-interview-approve" class="inline-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Approve For HR Round</button>
            </form>
            <form method="post" action="/applications/post-interview-reject" class="inline-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit" class="danger">Reject Candidate</button>
            </form>
            """
        send_teams_form = ""
        if row.get("interview_scheduled_at") and not row.get("teams_join_url"):
            send_teams_form = f"""
            <form method="post" action="/applications/send-teams-link" class="inline-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Send Teams Link</button>
            </form>
            """
        send_interview_form = f"""
            <form method="post" action="/applications/send-interview-link" class="inline-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Send Interview Link</button>
            </form>
            """
        return f"""
        {self.back_link("/applications", "Back to applications")}
        <section class="panel">
            <div class="panel-head">
                <h2>{html_escape(title)}</h2>
                <div class="action-stack">
                    {status_badge(row["application_status"])}
                    <a class="link-button" href="/applications/{html_escape(application_id)}/cv">Download CV</a>
                    {hr_approve_form}
                    {post_interview_review_forms}
                    {send_interview_form}
                    {send_teams_form}
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        """

    def render_public_interview(self, token: str):
        application = self.application_for_interview_token(token)
        if not application:
            self.send_error(404, "Interview link not found")
            return
        completed = bool(application.get("interview_completed_at"))
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position") or "this role"
        candidate = application.get("full_name") or "there"
        disabled_notice = ""
        if completed:
            disabled_notice = "<p class=\"notice\">This interview has already been completed. Thank you.</p>"
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Interview - {html_escape(role)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --ink: #f8fafc;
      --muted: #a7b0c0;
      --line: #293241;
      --brand: #2e90fa;
      --danger: #f04438;
      --ok: #12b76a;
      --bg: #0b111d;
      --panel: #111827;
      --tile: #172033;
    }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 22px 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 18px 42px rgba(0, 0, 0, .32);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
    }}
    .muted, .status {{
      color: var(--muted);
      line-height: 1.5;
    }}
    .call-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 16px;
      margin-top: 18px;
    }}
    .stage {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      min-height: 430px;
    }}
    .video-tile {{
      position: relative;
      overflow: hidden;
      min-height: 330px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--tile);
    }}
    .ai-tile {{
      display: grid;
      place-items: center;
      text-align: center;
      padding: 18px;
    }}
    .avatar {{
      width: 104px;
      height: 104px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: linear-gradient(145deg, #1849a9, #2e90fa);
      color: #fff;
      font-size: 34px;
      font-weight: 700;
      margin: 0 auto 16px;
    }}
    .candidate-video {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      transform: scaleX(-1);
      background: #050914;
    }}
    .tile-label {{
      position: absolute;
      left: 12px;
      bottom: 12px;
      padding: 6px 9px;
      border-radius: 6px;
      background: rgba(8, 13, 24, .78);
      color: #fff;
      font-size: 13px;
    }}
    .permission-overlay {{
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 18px;
      text-align: center;
      background: rgba(8, 13, 24, .72);
      color: var(--muted);
    }}
    .side-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #0f1728;
      padding: 16px;
    }}
    .question {{
      margin: 0 0 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 18px;
      line-height: 1.35;
      background: #111b2f;
    }}
    .answer {{
      min-height: 180px;
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      font-size: 16px;
      line-height: 1.5;
      resize: vertical;
      background: #0b1220;
      color: var(--ink);
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 16px;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      background: var(--brand);
      color: #fff;
      padding: 11px 16px;
      font-size: 15px;
      cursor: pointer;
    }}
    button.secondary {{
      background: #23314d;
      color: #d6e4ff;
    }}
    button.danger {{
      background: var(--danger);
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      color: var(--muted);
      font-size: 13px;
      margin: 8px 8px 0 0;
    }}
    .dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--muted);
    }}
    .dot.ok {{
      background: var(--ok);
    }}
    .dot.live {{
      background: var(--danger);
    }}
    button:disabled {{
      opacity: .55;
      cursor: not-allowed;
    }}
    .notice {{
      padding: 12px 14px;
      border-radius: 6px;
      background: #ecfdf3;
      color: #067647;
      border: 1px solid #abefc6;
    }}
    .error {{
      padding: 12px 14px;
      border-radius: 6px;
      background: #fff1f3;
      color: #c01048;
      border: 1px solid #fea3b4;
    }}
    .hidden {{
      display: none;
    }}
    @media (max-width: 900px) {{
      .call-layout {{
        grid-template-columns: 1fr;
      }}
      .stage {{
        grid-template-columns: 1fr;
      }}
      .video-tile {{
        min-height: 260px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="panel">
      <h1>Interview for {html_escape(role)}</h1>
      <p class="muted">Hi {html_escape(candidate)}, this will feel like a short video interview. Please allow camera and microphone access when your browser asks.</p>
      {disabled_notice}
      <p id="message" class="status"></p>
      <div id="interviewBox" class="{html_escape('hidden' if completed else '')}">
        <div>
          <span class="pill"><span id="micDot" class="dot"></span><span id="micStatus">Mic not connected</span></span>
          <span class="pill"><span id="camDot" class="dot"></span><span id="camStatus">Camera not connected</span></span>
          <span class="pill"><span id="callDot" class="dot"></span><span id="callStatus">Waiting to start</span></span>
        </div>
        <div class="call-layout">
          <div class="stage">
            <div class="video-tile ai-tile">
              <div>
                <div class="avatar">HR</div>
                <h2>AI HR Interviewer</h2>
                <p id="aiStatus" class="muted">Ready when you are.</p>
              </div>
              <div class="tile-label">Interviewer</div>
            </div>
            <div class="video-tile">
              <video id="candidateVideo" class="candidate-video" autoplay muted playsinline></video>
              <div id="permissionOverlay" class="permission-overlay">Your camera preview will appear here after permission is allowed.</div>
              <div class="tile-label">{html_escape(candidate)}</div>
            </div>
          </div>
          <aside class="side-panel">
            <div id="question" class="question">Click Start Interview when you are ready.</div>
            <textarea id="answer" class="answer" placeholder="Your answer transcript will appear here." readonly></textarea>
            <div class="actions">
              <button id="startBtn">Start Interview</button>
              <button id="listenBtn" class="secondary hidden" disabled>Answer</button>
              <button id="replayBtn" class="secondary hidden" disabled>Replay</button>
              <button id="nextBtn" class="hidden" disabled>Next</button>
              <button id="endBtn" class="danger hidden" disabled>End</button>
            </div>
          </aside>
        </div>
      </div>
    </section>
  </main>
  <script>
    const token = {json.dumps(token)};
    let questions = [];
    let index = -1;
    let transcript = [];
    let currentQuestion = '';
    let interviewPhase = 'idle';
    let recognition = null;
    let mediaStream = null;
    let finalTranscript = '';
    let isRecording = false;
    let autoAdvanceTimer = null;
    let speechStarted = false;
    let isSubmitting = false;
    let processingNudgeTimer = null;
    let processingNudgeSpoken = false;
    let activeSpeechId = 0;
    let flowVersion = 0;
    let interviewClosed = false;
    let clientTurnId = 0;
    const ANSWER_SILENCE_MS = 2800;

    const questionEl = document.getElementById('question');
    const answerEl = document.getElementById('answer');
    const messageEl = document.getElementById('message');
    const interviewBox = document.getElementById('interviewBox');
    const startBtn = document.getElementById('startBtn');
    const listenBtn = document.getElementById('listenBtn');
    const replayBtn = document.getElementById('replayBtn');
    const nextBtn = document.getElementById('nextBtn');
    const endBtn = document.getElementById('endBtn');
    const candidateVideo = document.getElementById('candidateVideo');
    const permissionOverlay = document.getElementById('permissionOverlay');
    const micDot = document.getElementById('micDot');
    const camDot = document.getElementById('camDot');
    const callDot = document.getElementById('callDot');
    const micStatus = document.getElementById('micStatus');
    const camStatus = document.getElementById('camStatus');
    const callStatus = document.getElementById('callStatus');
    const aiStatus = document.getElementById('aiStatus');

    function setMessage(text, isError=false) {{
      messageEl.textContent = text || '';
      messageEl.className = isError ? 'error' : 'status';
    }}

    function setDeviceStatus(type, ok, text) {{
      const dot = type === 'mic' ? micDot : camDot;
      const label = type === 'mic' ? micStatus : camStatus;
      dot.className = ok ? 'dot ok' : 'dot';
      label.textContent = text;
    }}

    function setCallStatus(text, live=false) {{
      callStatus.textContent = text;
      callDot.className = live ? 'dot live' : 'dot ok';
    }}

    function speak(text, onend) {{
      const speechId = ++activeSpeechId;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 0.95;
      utterance.pitch = 1;
      aiStatus.textContent = 'Speaking';
      utterance.onend = () => {{
        if (speechId !== activeSpeechId) return;
        aiStatus.textContent = 'Ready';
        if (onend) onend();
      }};
      utterance.onerror = () => {{
        if (speechId !== activeSpeechId) return;
        aiStatus.textContent = 'Ready';
        if (onend) onend();
      }};
      window.speechSynthesis.speak(utterance);
    }}

    function afterCurrentSpeech(callback) {{
      if (window.speechSynthesis.speaking || window.speechSynthesis.pending) {{
        window.setTimeout(() => afterCurrentSpeech(callback), 120);
        return;
      }}
      callback();
    }}

    function clearAutoAdvanceTimer() {{
      if (autoAdvanceTimer) {{
        window.clearTimeout(autoAdvanceTimer);
        autoAdvanceTimer = null;
      }}
    }}

    function clearProcessingNudge() {{
      if (processingNudgeTimer) {{
        window.clearTimeout(processingNudgeTimer);
        processingNudgeTimer = null;
      }}
    }}

    function processingNudge() {{
      const options = [
        'Give me a moment, I am reviewing that.',
        'One moment, I am thinking through your answer.',
        'Thanks, I am just reviewing your response.'
      ];
      return options[Math.floor(Math.random() * options.length)];
    }}

    function startProcessingNudge() {{
      clearProcessingNudge();
      processingNudgeSpoken = false;
      processingNudgeTimer = window.setTimeout(() => {{
        processingNudgeTimer = null;
        processingNudgeSpoken = true;
        setMessage('The interviewer is thinking...');
        speak(processingNudge());
      }}, 3500);
    }}

    function closeInterviewScreen() {{
      if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
      if (interviewBox) interviewBox.classList.add('hidden');
      setCallStatus('Completed', false);
      setMessage('Interview completed. Thank you for your time today. I will get back to you with feedback soon.');
    }}

    async function ensureMediaAccess() {{
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
        setMessage('This browser does not support camera/microphone access. Please use Chrome.', true);
        return false;
      }}
      try {{
        mediaStream = await navigator.mediaDevices.getUserMedia({{audio: true, video: true}});
      }} catch (firstError) {{
        try {{
          mediaStream = await navigator.mediaDevices.getUserMedia({{audio: true, video: false}});
          setDeviceStatus('cam', false, 'Camera blocked');
          permissionOverlay.textContent = 'Camera is blocked, but microphone is connected.';
        }} catch (secondError) {{
          setDeviceStatus('mic', false, 'Mic blocked');
          setDeviceStatus('cam', false, 'Camera blocked');
          const reason = secondError && secondError.name ? secondError.name : 'Permission denied';
          setMessage(`Microphone access failed: ${{reason}}. In Chrome, click the lock icon in the address bar and allow microphone access.`, true);
          return false;
        }}
      }}
      const hasAudio = mediaStream.getAudioTracks().some(track => track.readyState === 'live');
      const hasVideo = mediaStream.getVideoTracks().some(track => track.readyState === 'live');
      setDeviceStatus('mic', hasAudio, hasAudio ? 'Mic connected' : 'Mic unavailable');
      setDeviceStatus('cam', hasVideo, hasVideo ? 'Camera connected' : 'Camera unavailable');
      if (hasVideo) {{
        candidateVideo.srcObject = mediaStream;
        permissionOverlay.classList.add('hidden');
      }}
      return hasAudio;
    }}

    function setupRecognition() {{
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {{
        setMessage('Speech recognition is not supported in this browser. Please type your answers manually.', true);
        return null;
      }}
      const rec = new SpeechRecognition();
      rec.lang = 'en-US';
      rec.interimResults = true;
      rec.continuous = true;
      rec.onresult = (event) => {{
        let interim = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {{
          const text = event.results[i][0].transcript;
          if (event.results[i].isFinal) finalTranscript += text + ' ';
          else interim += text;
        }}
        answerEl.value = (finalTranscript + interim).trim();
        if (answerEl.value.trim()) {{
          speechStarted = true;
          nextBtn.disabled = false;
          setMessage('Listening. Please continue naturally.');
          clearAutoAdvanceTimer();
          autoAdvanceTimer = window.setTimeout(() => {{
            const words = answerEl.value.trim().split(/\\s+/).filter(Boolean).length;
            if (isRecording && (words >= 3 || (interviewPhase === 'greeting' && words >= 1))) {{
              stopListeningAndAdvance();
            }}
          }}, ANSWER_SILENCE_MS);
        }}
      }};
      rec.onerror = (event) => {{
        isRecording = false;
        listenBtn.textContent = 'Answer';
        nextBtn.disabled = false;
        const reason = event && event.error ? event.error : 'unknown error';
        setMessage(`Speech recognition issue: ${{reason}}. You can type the answer and continue.`, true);
      }};
      rec.onend = () => {{
        isRecording = false;
        listenBtn.textContent = 'Answer';
        aiStatus.textContent = 'Ready';
        nextBtn.disabled = false;
        clearAutoAdvanceTimer();
      }};
      return rec;
    }}

    async function startInterview() {{
      startBtn.disabled = true;
      setMessage('Checking camera and microphone permissions...');
      const mediaOk = await ensureMediaAccess();
      if (!mediaOk) {{
        startBtn.disabled = false;
        return;
      }}
      setMessage('Preparing your interview...');
      const response = await fetch(`/api/interview/${{token}}/start`, {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}});
      const data = await response.json();
      if (!response.ok) {{
        setMessage(data.error || 'Could not start interview.', true);
        startBtn.disabled = false;
        return;
      }}
      questions = data.questions || [];
      currentQuestion = data.question || '';
      questions = currentQuestion ? [currentQuestion] : [];
      recognition = setupRecognition();
      setMessage('The interviewer will check your audio first.');
      setCallStatus('In interview', true);
      endBtn.disabled = false;
      startOpeningCheck(data.question_number || 1, currentQuestion);
    }}

    function startOpeningCheck(questionNumber, firstQuestion) {{
      interviewPhase = 'greeting';
      index = 0;
      currentQuestion = 'Opening audio check';
      questions[0] = firstQuestion;
      answerEl.value = '';
      finalTranscript = '';
      questionEl.textContent = 'Opening audio check';
      speak('Hello, can you hear me clearly?', () => beginListening());
    }}

    function showQuestion(questionNumber, text) {{
      if (interviewClosed) return;
      const expectedFlow = ++flowVersion;
      index = questionNumber - 1;
      interviewPhase = 'question';
      currentQuestion = text;
      answerEl.value = '';
      finalTranscript = '';
      speechStarted = false;
      clearAutoAdvanceTimer();
      nextBtn.disabled = true;
      listenBtn.disabled = false;
      replayBtn.disabled = false;
      questionEl.textContent = `Question ${{index + 1}}: ${{text}}`;
      speak(text, () => {{
        if (interviewClosed || expectedFlow !== flowVersion) return;
        beginListening();
      }});
    }}

    function beginListening() {{
      if (interviewClosed || isSubmitting) return;
      if (!recognition) {{
        nextBtn.disabled = false;
        return;
      }}
      if (isRecording) {{
        return;
      }}
      finalTranscript = answerEl.value.trim() ? answerEl.value.trim() + ' ' : '';
      listenBtn.textContent = 'Stop Recording';
      nextBtn.disabled = true;
      isRecording = true;
      aiStatus.textContent = 'Listening';
      setMessage('Listening. Please answer now.');
      try {{
        recognition.start();
      }} catch (error) {{
        isRecording = false;
        listenBtn.textContent = 'Answer';
        nextBtn.disabled = false;
        setMessage('Speech recognition is already active. Please continue speaking.', true);
      }}
    }}

    function recordAnswer() {{
      if (isRecording) {{
        stopListeningAndAdvance();
        return;
      }}
      beginListening();
    }}

    function stopListeningOnly() {{
      clearAutoAdvanceTimer();
      if (recognition && isRecording) {{
        try {{
          recognition.stop();
        }} catch (error) {{
          isRecording = false;
        }}
      }}
      isRecording = false;
      listenBtn.textContent = 'Answer';
      aiStatus.textContent = 'Ready';
      nextBtn.disabled = false;
    }}

    function stopListeningAndAdvance() {{
      if (isSubmitting || interviewClosed) return;
      stopListeningOnly();
      if (!answerEl.value.trim()) {{
        setMessage('I could not hear an answer yet. Please answer again.');
        return;
      }}
      submitTurn();
    }}

    function transitionAcknowledgement() {{
      const options = ['Alright.', 'Thanks for explaining.', 'Got it.', 'Understood.'];
      return options[Math.floor(Math.random() * options.length)];
    }}

    function cleanTransitionReply(reply, nextQuestionText) {{
      const fallback = transitionAcknowledgement();
      const text = (reply || '').trim();
      if (!text) return fallback;
      const lower = text.toLowerCase();
      const currentLower = (currentQuestion || '').toLowerCase();
      const nextLower = (nextQuestionText || '').toLowerCase();
      if ((currentLower && lower.includes(currentLower)) || (nextLower && lower.includes(nextLower))) {{
        return fallback;
      }}
      if (text.length > 90 || text.includes('?')) {{
        return fallback;
      }}
      return text;
    }}

    async function submitTurn() {{
      if (isSubmitting || interviewClosed) return;
      isSubmitting = true;
      const submitFlow = ++flowVersion;
      if (recognition && isRecording) stopListeningOnly();
      clearAutoAdvanceTimer();
      if (!answerEl.value.trim()) {{
        isSubmitting = false;
        setMessage('I could not hear you yet. Please speak again.');
        beginListening();
        return;
      }}
      const answer = answerEl.value.trim();
      if (interviewPhase === 'greeting') {{
        transcript.push({{question: 'Opening audio check', answer, status: 'greeting'}});
        const firstQuestion = questions[0];
        setMessage('Great, thank you. Starting the interview now.');
        isSubmitting = false;
        speak('Great, thank you. Let us begin.', () => {{
          if (interviewClosed || submitFlow !== flowVersion) return;
          showQuestion(1, firstQuestion);
        }});
        return;
      }}
      transcript.push({{question: currentQuestion, answer}});
      startBtn.disabled = true;
      listenBtn.disabled = true;
      replayBtn.disabled = true;
      nextBtn.disabled = true;
      aiStatus.textContent = 'Thinking';
      setMessage('Processing your answer...');
      startProcessingNudge();
      const turnId = ++clientTurnId;
      let response;
      let data;
      try {{
        response = await fetch(`/api/interview/${{token}}/turn`, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{answer, turn_id: turnId}})
        }});
        data = await response.json();
      }} catch (error) {{
        clearProcessingNudge();
        isSubmitting = false;
        listenBtn.disabled = false;
        replayBtn.disabled = false;
        nextBtn.disabled = false;
        aiStatus.textContent = 'Ready';
        setMessage('I had trouble processing that response. Please answer once more.', true);
        speak('Sorry, I had trouble processing that. Could you please answer once more?', () => beginListening());
        return;
      }}
      clearProcessingNudge();
      if (!response.ok) {{
        isSubmitting = false;
        setMessage(data.error || 'Could not process answer.', true);
        listenBtn.disabled = false;
        replayBtn.disabled = false;
        nextBtn.disabled = false;
        return;
      }}
      const action = data.action || 'next_question';
      if (action === 'ignored') {{
        isSubmitting = false;
        aiStatus.textContent = 'Ready';
        return;
      }}
      const reply = data.reply || transitionAcknowledgement();
      if (action === 'complete') {{
        interviewClosed = true;
        ++flowVersion;
        window.speechSynthesis.cancel();
        questionEl.textContent = 'Interview completed';
        answerEl.classList.add('hidden');
        setCallStatus('Completed', false);
        listenBtn.disabled = true;
        replayBtn.disabled = true;
        nextBtn.disabled = true;
        endBtn.disabled = true;
        isSubmitting = false;
        setMessage('Thank you for your time today. We will review everything and get back to you with feedback soon.');
        const finalReply = reply || 'Thank you so much for your time today. I appreciate you sharing your experience with me. I will review everything and get back to you with feedback soon.';
        const finish = () => speak(finalReply, closeInterviewScreen);
        if (processingNudgeSpoken) afterCurrentSpeech(finish);
        else finish();
        return;
      }}
      const nextQuestionText = data.question || currentQuestion;
      const nextNumber = data.question_number || (action === 'follow_up' ? index + 1 : index + 2);
      const deliverResponse = () => {{
        if (interviewClosed || submitFlow !== flowVersion) return;
        if (action === 'repeat') {{
          setMessage('Repeating the question.');
          questionEl.textContent = `Question ${{index + 1}}: ${{currentQuestion}}`;
          isSubmitting = false;
          speak(reply || `Sure, let me repeat that. ${{currentQuestion}}`, () => {{
            if (interviewClosed || submitFlow !== flowVersion) return;
            beginListening();
          }});
          return;
        }}
        if (action === 'clarify') {{
          setMessage('The interviewer is clarifying before continuing.');
          isSubmitting = false;
          speak(reply || 'Could you explain that a little differently?', () => {{
            if (interviewClosed || submitFlow !== flowVersion) return;
            beginListening();
          }});
          return;
        }}
        setMessage(action === 'follow_up' ? 'The interviewer has one follow-up.' : 'Moving to the next question.');
        isSubmitting = false;
        speak(cleanTransitionReply(reply, nextQuestionText), () => {{
          if (interviewClosed || submitFlow !== flowVersion) return;
          showQuestion(nextNumber, nextQuestionText);
        }});
      }};
      if (processingNudgeSpoken) afterCurrentSpeech(deliverResponse);
      else deliverResponse();
    }}

    startBtn.addEventListener('click', startInterview);
    listenBtn.addEventListener('click', recordAnswer);
    replayBtn.addEventListener('click', () => {{
      stopListeningOnly();
      speak(currentQuestion || '', () => beginListening());
    }});
    nextBtn.addEventListener('click', submitTurn);
    endBtn.addEventListener('click', () => {{
      if (recognition && isRecording) recognition.stop();
      if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
      setCallStatus('Ended', false);
      setMessage('The interview has been ended in this browser window.');
      startBtn.disabled = true;
      listenBtn.disabled = true;
      replayBtn.disabled = true;
      nextBtn.disabled = true;
      endBtn.disabled = true;
    }});
  </script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_event_detail(self, event_id: int) -> str:
        database = self.db()
        try:
            row = database.one("SELECT * FROM recruiter_email_events WHERE id = %s", (event_id,))
            if not row:
                return self.not_found_panel("Email event", "/events")
        finally:
            database.close()

        fields = [
            ("Event Type", status_badge(row["event_type"]), "html"),
            ("Source Email", row["source_email"]),
            ("Email Subject", row["email_subject"]),
            ("Email Message ID", row["email_message_id"]),
            ("Created", date_text(row["created_at"])),
            ("Details", row["details"], "json-wide"),
        ]
        return f"""
        {self.back_link("/events", "Back to events")}
        <section class="panel">
            <div class="panel-head"><h2>Email Event #{html_escape(event_id)}</h2>{status_badge(row["event_type"])}</div>
            {self.detail_grid(fields)}
        </section>
        """

    def status_options(self, selected: str | None) -> str:
        options = [
            "matched_requirement",
            "screening_questions_sent",
            "screening_negotiation",
            "hr_escalated",
            "interview_time_requested",
            "interview_availability_received",
            "interview_scheduled",
            "interview_completed",
            "hr_round_time_requested",
            "interview_on_hold_hr_review",
            "interview_rejected",
            "no_open_requirement",
            "reviewed",
            "shortlisted",
            "rejected",
            "withdrawn",
        ]
        return "".join(
            f'<option value="{html_escape(option)}" {"selected" if option == selected else ""}>{html_escape(option.replace("_", " "))}</option>'
            for option in options
        )

    def detail_grid(self, fields: list[tuple]) -> str:
        rendered = []
        for field in fields:
            label = field[0]
            value = field[1]
            mode = field[2] if len(field) > 2 else "text"
            wide = " wide" if mode.endswith("-wide") else ""
            base_mode = mode.replace("-wide", "")
            rendered.append(
                f"""
                <div class="field{wide}">
                    <span>{html_escape(label)}</span>
                    {self.detail_value(value, base_mode)}
                </div>
                """
            )
        return f'<div class="detail-grid">{"".join(rendered)}</div>'

    def detail_value(self, value, mode: str) -> str:
        if value is None or value == "":
            return '<strong class="muted">-</strong>'
        if mode == "html":
            return f"<strong>{value}</strong>"
        if mode == "json":
            return f"<pre>{self.json_text(value)}</pre>"
        if mode == "pre":
            return f"<pre>{html_escape(value)}</pre>"
        return f"<strong>{html_escape(value)}</strong>"

    def json_text(self, value) -> str:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return html_escape(value)
        return html_escape(json.dumps(value, indent=2, default=str))

    def link_value(self, value: str | None) -> str:
        if not value:
            return "-"
        return f'<a href="{html_escape(value)}" target="_blank" rel="noreferrer">{html_escape(value)}</a>'

    def requirement_link(self, requirement_id, label) -> str:
        if not requirement_id:
            return "-"
        return f'<a href="/requirements/{html_escape(requirement_id)}">{html_escape(label or requirement_id)}</a>'

    def back_link(self, href: str, label: str) -> str:
        return f'<div class="back-row"><a class="link-button" href="{html_escape(href)}">{html_escape(label)}</a></div>'

    def not_found_panel(self, record_name: str, back_href: str) -> str:
        return f"""
        {self.back_link(back_href, f"Back to {record_name.lower()}s")}
        <section class="panel">
            <div class="empty">{html_escape(record_name)} not found.</div>
        </section>
        """

    def search_form(self, action: str, value: str, placeholder: str) -> str:
        return f"""
        <form method="get" action="{html_escape(action)}" class="search">
            <input name="q" value="{html_escape(value)}" placeholder="{html_escape(placeholder)}">
            <button type="submit">Search</button>
            <a href="{html_escape(action)}">Clear</a>
        </form>
        """

    def render_page(self, active: str, content: str):
        body = f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Recruiter Dashboard</title>
            <style>{CSS}</style>
        </head>
        <body>
            <aside class="sidebar">
                <div class="brand">
                    <span class="mark">HR</span>
                    <div><strong>Recruiter</strong><small>AI mailbox dashboard</small></div>
                </div>
                <nav>
                    {self.nav_link("/", "Overview", active == "overview")}
                    {self.nav_link("/requirements", "Requirements", active == "requirements")}
                    {self.nav_link("/applications", "Applications", active == "applications")}
                    {self.nav_link("/candidates", "Candidates", active == "candidates")}
                    {self.nav_link("/events", "Email Events", active == "events")}
                </nav>
            </aside>
            <main>
                <header class="topbar">
                    <div>
                        <h1>{html_escape(active.replace("_", " ").title())}</h1>
                        <p>Review CV processing, role matching, and recruiter activity.</p>
                    </div>
                    <a class="primary" href="/requirements">Add Role</a>
                </header>
                {content}
            </main>
        </body>
        </html>
        """
        self.send_html(body)

    def render_error(self, exc: Exception):
        if DB_PROVIDER in {"mssql", "sqlserver", "sql_server"}:
            help_text = (
                "SQL Server mode is enabled. Make sure Microsoft ODBC Driver 18 for SQL Server "
                "and unixODBC are installed, and `MSSQL_CONNECTION_STRING` is set."
            )
            command = "sudo ./scripts/install_mssql_odbc_ubuntu.sh"
        else:
            help_text = "PostgreSQL mode is enabled. Make sure PostgreSQL is running and `DATABASE_URL` is set."
            command = "docker compose up -d recruiter-postgres"
        content = f"""
        <!doctype html>
        <html lang="en">
        <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Dashboard Error</title><style>{CSS}</style></head>
        <body class="error-page">
            <section class="error-box">
                <h1>Dashboard could not load</h1>
                <p>{html_escape(exc)}</p>
                <p>{html_escape(help_text)}</p>
                <code>{html_escape(command)}</code>
            </section>
        </body>
        </html>
        """
        self.send_html(content, status=500)

    def nav_link(self, href: str, label: str, active: bool) -> str:
        return f'<a class="{"active" if active else ""}" href="{href}">{html_escape(label)}</a>'

    def redirect(self, path: str):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def send_html(self, content: str, status: int = 200):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args):
        print(f"[dashboard] {self.address_string()} - {format % args}")


CSS = """
:root {
    color-scheme: light;
    --ink: #17202a;
    --muted: #627084;
    --line: #d9e1ea;
    --panel: #ffffff;
    --page: #f4f7fb;
    --nav: #102033;
    --nav-soft: #1d334d;
    --accent: #0f766e;
    --accent-2: #b45309;
    --danger: #b42318;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    min-height: 100vh;
    display: grid;
    grid-template-columns: 240px 1fr;
    background: var(--page);
    color: var(--ink);
    font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: inherit; }
.sidebar {
    background: var(--nav);
    color: white;
    padding: 20px 16px;
    position: sticky;
    top: 0;
    height: 100vh;
}
.brand {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 28px;
}
.brand small, .topbar p, td small {
    display: block;
    color: var(--muted);
}
.brand small { color: #b8c7d8; }
.mark {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border-radius: 8px;
    background: #0f766e;
    font-weight: 800;
}
nav { display: grid; gap: 6px; }
nav a {
    text-decoration: none;
    padding: 10px 12px;
    border-radius: 8px;
    color: #dbe7f3;
}
nav a.active, nav a:hover { background: var(--nav-soft); color: white; }
main {
    min-width: 0;
    padding: 24px;
}
.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 18px;
}
h1, h2, p { margin: 0; }
h1 { font-size: 26px; letter-spacing: 0; }
h2 { font-size: 16px; }
.primary, .link-button, button, .search a {
    border: 1px solid var(--line);
    background: white;
    color: var(--ink);
    border-radius: 8px;
    padding: 9px 12px;
    text-decoration: none;
    cursor: pointer;
    font: inherit;
}
.primary, button[type="submit"] {
    border-color: var(--accent);
    background: var(--accent);
    color: white;
}
.metrics {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 16px;
}
.metric, .panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
}
.metric {
    padding: 16px;
}
.metric span, .metric small { color: var(--muted); }
.metric strong {
    display: block;
    font-size: 28px;
    margin: 4px 0;
}
.grid-two {
    display: grid;
    grid-template-columns: 1.3fr .7fr;
    gap: 16px;
    margin-bottom: 16px;
}
.panel {
    overflow: hidden;
    margin-bottom: 16px;
}
.panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
}
.panel-head span { color: var(--muted); }
table {
    width: 100%;
    border-collapse: collapse;
}
th, td {
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    vertical-align: top;
}
th {
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
    background: #f8fafc;
}
td.description {
    min-width: 320px;
    max-width: 520px;
    white-space: pre-wrap;
}
.empty {
    color: var(--muted);
    text-align: center;
    padding: 28px;
}
.badge {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 3px 8px;
    border-radius: 999px;
    background: #e8eef5;
    color: #41516a;
    text-transform: capitalize;
    white-space: nowrap;
}
.badge-open, .badge-matched_requirement, .badge-shortlisted {
    background: #dff5ef;
    color: #0f6b58;
}
.badge-closed, .badge-rejected, .badge-withdrawn {
    background: #fde8e6;
    color: var(--danger);
}
.badge-no_open_requirement, .badge-review, .badge-reviewed {
    background: #fff3d8;
    color: var(--accent-2);
}
.search {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
}
input, textarea, select {
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 9px 10px;
    font: inherit;
    background: white;
}
textarea { resize: vertical; }
.search input { max-width: 440px; }
.requirement-form {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    padding: 16px;
}
label {
    display: grid;
    gap: 6px;
    color: var(--muted);
}
label.wide { grid-column: 1 / -1; }
label.check {
    display: flex;
    align-items: center;
    gap: 8px;
}
label.check input { width: auto; }
.requirement-form button { justify-self: start; }
.inline-form {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 220px;
}
.inline-form select { min-width: 150px; }
.action-stack {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.action-stack .inline-form {
    min-width: 0;
}
pre {
    margin: 0;
    white-space: pre-wrap;
    max-width: 520px;
    font-size: 12px;
    color: #334155;
}
.health {
    display: grid;
    gap: 12px;
    padding: 16px;
}
.health div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 10px;
}
.health div:last-child { border-bottom: 0; padding-bottom: 0; }
.health span { color: var(--muted); }
.back-row {
    margin-bottom: 16px;
}
.detail-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0;
}
.field {
    min-width: 0;
    padding: 14px 16px;
    border-right: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
}
.field:nth-child(3n) {
    border-right: 0;
}
.field.wide {
    grid-column: 1 / -1;
    border-right: 0;
}
.field span {
    display: block;
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
    margin-bottom: 6px;
}
.field strong {
    display: block;
    overflow-wrap: anywhere;
}
.field pre {
    max-width: none;
    max-height: 520px;
    overflow: auto;
    background: #f8fafc;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 12px;
}
.muted {
    color: var(--muted);
}
.error-page {
    display: grid;
    grid-template-columns: 1fr;
    place-items: center;
    padding: 24px;
}
.error-box {
    max-width: 680px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 24px;
}
code {
    display: block;
    margin-top: 12px;
    padding: 12px;
    border-radius: 8px;
    background: #eef2f7;
}
@media (max-width: 1100px) {
    body { grid-template-columns: 1fr; }
    .sidebar {
        position: static;
        height: auto;
    }
    nav { grid-template-columns: repeat(5, minmax(0, 1fr)); }
    .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid-two, .requirement-form, .detail-grid { grid-template-columns: 1fr; }
    .field, .field:nth-child(3n) { border-right: 0; }
    main { padding: 16px; }
    table { min-width: 900px; }
    .panel { overflow-x: auto; }
}
@media (max-width: 640px) {
    nav { grid-template-columns: 1fr 1fr; }
    .topbar, .search { align-items: stretch; flex-direction: column; }
    .metrics { grid-template-columns: 1fr; }
}
"""


def run(host: str, port: int):
    server = ThreadingHTTPServer((host, port), RecruiterDashboardHandler)
    print(f"Recruiter dashboard running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping recruiter dashboard.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Run the AI recruiter dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run(args.host, args.port)


if __name__ == "__main__":
    main()
