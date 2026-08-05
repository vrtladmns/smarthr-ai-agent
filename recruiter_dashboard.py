import argparse
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import random
import re
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, quote, urlencode, urlparse

from config import (
    CV_STORAGE_DIR,
    DB_PROVIDER,
    OLLAMA_NUM_PREDICT,
    RECRUITER_DASHBOARD_LOGIN_EMAIL,
    RECRUITER_DASHBOARD_LOGIN_PASSWORD,
    RECRUITER_DASHBOARD_SESSION_SECRET,
    RECRUITER_BROWSER_PROCESSING_NUDGE_MS,
    RECRUITER_BROWSER_TTS_VOICE_HINTS,
    RECRUITER_INTERVIEW_HOLD_MIN_SCORE,
    RECRUITER_INTERVIEW_PASS_SCORE,
    RECRUITER_INTERVIEW_QUESTION_COUNT,
    ONEDRIVE_RECORDINGS_ENABLED,
    ONEDRIVE_RECORDINGS_FOLDER,
    ONEDRIVE_RECORDINGS_USER,
)
from llm_factory import make_chat_model
from recruiter_agent import (
    LOGGER,
    MicrosoftGraphProvider,
    RecruiterDatabase,
    candidate_interview_url,
    final_hr_interviewers,
    hold_candidate_after_hr_round,
    mark_due_final_hr_rounds_pending,
    notify_post_interview_outcome,
    reject_candidate_after_hr_round,
    request_hr_round_reschedule,
    recruiter_tz,
    revoke_jd_score_rejection,
    select_candidate_after_hr_round,
    send_final_hr_round_request,
    send_interview_link_for_application,
    send_interview_rejection,
    send_interview_request_after_hr_approval,
    send_teams_link_for_application,
    log_json,
    safe_onedrive_path_part,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
WEB_INTERVIEW_SESSIONS: dict[str, dict] = {}
FINAL_HR_CHECK_SECONDS = 300
DASHBOARD_SESSION_COOKIE = "recruiter_dashboard_session"
DASHBOARD_SESSION_SECONDS = 12 * 60 * 60


def notify_post_interview_outcome_async(application_id: int, report: dict):
    def run():
        try:
            notify_post_interview_outcome(application_id, report)
        except Exception as exc:
            LOGGER.exception("Post-interview notification failed for application %s: %s", application_id, exc)

    Thread(target=run, daemon=True).start()


def start_final_hr_round_monitor():
    def run():
        while True:
            try:
                marked = mark_due_final_hr_rounds_pending()
                if marked:
                    log_json(logging.INFO, "final_hr_rounds_marked_pending", count=marked)
            except Exception as exc:
                LOGGER.exception("Final HR round monitor failed: %s", exc)
            time.sleep(FINAL_HR_CHECK_SECONDS)

    Thread(target=run, daemon=True).start()


def normalize_camera_monitoring(value, fallback=None) -> dict:
    source = value if isinstance(value, dict) else fallback if isinstance(fallback, dict) else {}
    unusual = source.get("unusual_activity") or []
    if not isinstance(unusual, list):
        unusual = []
    unusual = unusual[-25:]
    sample_count = int(source.get("sample_count") or 0)
    face_present_samples = int(source.get("face_present_samples") or 0)
    no_face_samples = int(source.get("no_face_samples") or 0)
    low_light_samples = int(source.get("low_light_samples") or 0)
    high_motion_events = int(source.get("high_motion_events") or 0)
    multiple_face_samples = int(source.get("multiple_face_samples") or 0)
    available = bool(source.get("available"))
    if not available:
        eye_summary = "Camera was not available or permission was not granted."
    elif source.get("face_detection_supported"):
        visible_ratio = round((face_present_samples / sample_count) * 100, 1) if sample_count else 0
        eye_summary = (
            f"Camera active. Face visible in about {visible_ratio}% of sampled frames. "
            "Exact eye-gaze tracking is not enabled in this browser mode."
        )
    else:
        eye_summary = (
            "Camera active. Browser did not support built-in face detection, "
            "so monitoring used video activity and lighting checks only."
        )
    return {
        "available": available,
        "started_at": source.get("started_at"),
        "ended_at": source.get("ended_at"),
        "method": source.get("method") or "browser_camera_sampling",
        "face_detection_supported": bool(source.get("face_detection_supported")),
        "sample_count": sample_count,
        "face_present_samples": face_present_samples,
        "no_face_samples": no_face_samples,
        "multiple_face_samples": multiple_face_samples,
        "low_light_samples": low_light_samples,
        "high_motion_events": high_motion_events,
        "eye_movement_summary": eye_summary,
        "unusual_activity": unusual,
    }


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


def parse_dashboard_datetime(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=recruiter_tz())
    return parsed


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


def parse_date_filter(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


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


def list_plain_text(value, limit: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, list):
        selected = value if limit is None else value[:limit]
        parts = []
        for item in selected:
            if isinstance(item, dict):
                parts.append(", ".join(str(v) for v in item.values() if v))
            else:
                parts.append(str(item))
        return ", ".join(part for part in parts if part)
    return str(value)


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


def cv_view_path(path: str, section: str) -> int | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) == 4 and parts[0] == section and parts[1].isdigit() and parts[2] == "cv" and parts[3] == "view":
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
        and parts[3] in {"start", "turn", "complete", "recording"}
    ):
        return parts[2], parts[3]
    return None


def safe_download_name(value: str | None, fallback: str) -> str:
    name = (value or fallback).strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:160] or fallback


def stored_cv_path(value: str | None) -> Path | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.startswith(("http://", "https://")):
        return None

    candidates = []
    path = Path(raw)
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(Path.cwd() / path)
        candidates.append(Path.cwd() / CV_STORAGE_DIR / raw)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def dashboard_auth_enabled() -> bool:
    return bool(RECRUITER_DASHBOARD_LOGIN_EMAIL and RECRUITER_DASHBOARD_LOGIN_PASSWORD)


def dashboard_session_secret() -> str:
    return (
        RECRUITER_DASHBOARD_SESSION_SECRET
        or RECRUITER_DASHBOARD_LOGIN_PASSWORD
        or "recruiter-dashboard-local-secret"
    )


def sign_dashboard_session(email_address: str, expires_at: int) -> str:
    payload = f"{email_address}|{expires_at}"
    signature = hmac.new(
        dashboard_session_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{signature}"


def verify_dashboard_session(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split("|")
    if len(parts) != 3:
        return False
    email_address, expires_text, signature = parts
    if email_address.lower() != RECRUITER_DASHBOARD_LOGIN_EMAIL.lower():
        return False
    try:
        expires_at = int(expires_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected = sign_dashboard_session(email_address, expires_at).rsplit("|", 1)[-1]
    return hmac.compare_digest(signature, expected)


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
        log_json(
            logging.INFO,
            "dashboard_request_started",
            method="GET",
            path=parsed.path,
            query=parsed.query,
            remote=self.client_address[0] if self.client_address else None,
        )
        try:
            if parsed.path == "/login":
                self.render_login()
                return
            if parsed.path == "/logout":
                self.clear_session()
                return
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
            candidate_cv_view_id = cv_view_path(parsed.path, "candidates")
            application_cv_view_id = cv_view_path(parsed.path, "applications")
            interview_token = interview_token_path(parsed.path)

            if interview_token:
                self.render_public_interview(interview_token)
            elif not self.require_dashboard_login():
                return
            elif parsed.path == "/":
                self.render_page("overview", self.render_overview())
            elif candidate_cv_view_id is not None:
                self.view_candidate_cv(candidate_cv_view_id)
            elif application_cv_view_id is not None:
                self.view_application_cv(application_cv_view_id)
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
            elif parsed.path == "/applications/export":
                self.export_applications(query)
            elif parsed.path == "/applications":
                self.render_page("applications", self.render_applications(query))
            elif event_id is not None:
                self.render_page("event_detail", self.render_event_detail(event_id))
            elif parsed.path == "/events":
                self.render_page("events", self.render_events(query))
            else:
                self.send_error(404, "Page not found")
        except Exception as exc:
            LOGGER.exception(
                "dashboard_get_failed %s",
                json.dumps(
                    {
                        "path": parsed.path,
                        "query": parsed.query,
                        "remote": self.client_address[0] if self.client_address else None,
                        "error": str(exc),
                    },
                    default=str,
                ),
            )
            self.render_error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        log_json(
            logging.INFO,
            "dashboard_request_started",
            method="POST",
            path=parsed.path,
            remote=self.client_address[0] if self.client_address else None,
        )
        try:
            interview_api = interview_api_path(parsed.path)
            if interview_api:
                token, action = interview_api
                if action == "recording":
                    self.api_interview_recording(token)
                    return
                payload = self.read_json_body()
                if action == "start":
                    self.api_interview_start(token)
                elif action == "turn":
                    self.api_interview_turn(token, payload)
                else:
                    self.api_interview_complete(token, payload)
                return

            if parsed.path == "/login":
                self.handle_login()
                return

            if not self.require_dashboard_login():
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            form = {key: values[0] if values else "" for key, values in parse_qs(payload).items()}
            log_json(
                logging.INFO,
                "dashboard_form_received",
                path=parsed.path,
                form_keys=sorted(form.keys()),
                application_id=form.get("id"),
                remote=self.client_address[0] if self.client_address else None,
            )

            if parsed.path == "/requirements":
                self.create_requirement(form)
                self.redirect("/requirements")
            elif parsed.path == "/requirements/update":
                requirement_id = parse_int(form.get("id"))
                if requirement_id is None:
                    raise RuntimeError("Requirement id is required.")
                self.update_requirement(form)
                self.redirect(f"/requirements/{requirement_id}")
            elif parsed.path == "/requirements/status":
                self.update_requirement_status(form)
                self.redirect("/requirements")
            elif parsed.path == "/requirements/delete":
                self.delete_requirement(form)
                self.redirect("/requirements")
            elif parsed.path == "/candidates/update":
                candidate_id = parse_int(form.get("id"))
                if candidate_id is None:
                    raise RuntimeError("Candidate id is required.")
                self.update_candidate(form)
                self.redirect(f"/candidates/{candidate_id}")
            elif parsed.path == "/candidates/delete":
                self.delete_candidate(form)
                self.redirect("/candidates")
            elif parsed.path == "/applications/status":
                self.update_application_status(form)
                self.redirect("/applications")
            elif parsed.path == "/applications/update":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                self.update_application(form)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/delete":
                self.delete_application(form)
                self.redirect("/applications")
            elif parsed.path == "/events/update":
                event_id = parse_int(form.get("id"))
                if event_id is None:
                    raise RuntimeError("Event id is required.")
                self.update_event(form)
                self.redirect(f"/events/{event_id}")
            elif parsed.path == "/events/delete":
                self.delete_event(form)
                self.redirect("/events")
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
            elif parsed.path == "/applications/revoke-jd-rejection":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                revoke_jd_score_rejection(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/final-select":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                select_candidate_after_hr_round(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/final-reject":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                reject_candidate_after_hr_round(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/final-hold":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                hold_candidate_after_hr_round(application_id)
                self.redirect(f"/applications/{application_id}")
            elif parsed.path == "/applications/final-reschedule":
                application_id = parse_int(form.get("id"))
                if application_id is None:
                    raise RuntimeError("Application id is required.")
                reschedule_mode = (form.get("reschedule_mode") or "ask_candidate").strip()
                scheduled_at = parse_dashboard_datetime(form.get("scheduled_at"))
                interviewer_email = (form.get("interviewer_email") or "").strip() or None
                request_hr_round_reschedule(
                    application_id,
                    scheduled_at=scheduled_at,
                    interviewer_email=interviewer_email,
                    ask_candidate=reschedule_mode != "schedule_now",
                )
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
            LOGGER.exception(
                "dashboard_post_failed %s",
                json.dumps(
                    {
                        "path": parsed.path,
                        "remote": self.client_address[0] if self.client_address else None,
                        "error": str(exc),
                    },
                    default=str,
                ),
            )
            self.render_error(exc)

    def db(self) -> DashboardDB:
        return DashboardDB()

    def dashboard_cookie_value(self) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if name == DASHBOARD_SESSION_COOKIE:
                return value
        return None

    def is_dashboard_logged_in(self) -> bool:
        if not dashboard_auth_enabled():
            return True
        return verify_dashboard_session(self.dashboard_cookie_value())

    def require_dashboard_login(self) -> bool:
        if self.is_dashboard_logged_in():
            return True
        self.redirect(f"/login?next={quote(self.path or '/', safe='')}")
        return False

    def set_session(self, email_address: str):
        expires_at = int(time.time()) + DASHBOARD_SESSION_SECONDS
        session_value = sign_dashboard_session(email_address, expires_at)
        self.send_header(
            "Set-Cookie",
            (
                f"{DASHBOARD_SESSION_COOKIE}={session_value}; "
                f"Max-Age={DASHBOARD_SESSION_SECONDS}; Path=/; HttpOnly; SameSite=Lax"
            ),
        )

    def clear_session(self):
        self.send_response(303)
        self.send_header("Set-Cookie", f"{DASHBOARD_SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Location", "/login")
        self.end_headers()

    def render_login(self, error: str = ""):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        next_path = query.get("next", ["/"])[0] or "/"
        body = f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Recruiter Dashboard Login</title>
            <style>{CSS}</style>
        </head>
        <body class="login-page">
            <main class="login-wrap">
                <section class="login-card">
                    <div class="brand login-brand">
                        <span class="mark">HR</span>
                        <div><strong>Recruiter</strong><small>Dashboard login</small></div>
                    </div>
                    <h1>Sign In</h1>
                    <p class="muted">Use your dashboard credentials to continue.</p>
                    {f'<p class="error">{html_escape(error)}</p>' if error else ''}
                    <form method="post" action="/login" class="login-form">
                        <input type="hidden" name="next" value="{html_escape(next_path)}">
                        <label>Email<input name="email" type="email" required autofocus></label>
                        <label>Password<input name="password" type="password" required></label>
                        <button type="submit">Sign In</button>
                    </form>
                </section>
            </main>
        </body>
        </html>
        """
        self.send_html(body)

    def handle_login(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length).decode("utf-8")
        form = {key: values[0] if values else "" for key, values in parse_qs(payload).items()}
        email_address = (form.get("email") or "").strip()
        password = form.get("password") or ""
        next_path = form.get("next") or "/"
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        if (
            dashboard_auth_enabled()
            and hmac.compare_digest(email_address.lower(), RECRUITER_DASHBOARD_LOGIN_EMAIL.lower())
            and hmac.compare_digest(password, RECRUITER_DASHBOARD_LOGIN_PASSWORD)
        ):
            self.send_response(303)
            self.set_session(email_address)
            self.send_header("Location", next_path)
            self.end_headers()
            return
        self.render_login("Invalid email or password.")

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

    def read_raw_body(self, max_bytes: int = 300_000_000) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return b""
        if length > max_bytes:
            raise ValueError(f"Request body too large: {length} bytes")
        return self.rfile.read(length)

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

    def web_interview_report(self, application: dict, transcript: list[dict], camera_monitoring: dict | None = None) -> dict:
        normalized_camera_monitoring = normalize_camera_monitoring(camera_monitoring)
        llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1800))
        response = llm.invoke(
            f"""
Return one valid JSON object only.
You are an HR technical interviewer. Evaluate this browser voice interview fairly.
Use only the candidate answers, CV, and JD context provided.
Camera monitoring is a browser-side signal only. Do not over-penalize camera issues unless unusual activity is repeated.
Use these final recommendation bands:
- overall_score > {RECRUITER_INTERVIEW_PASS_SCORE}: hire or strong_hire
- {RECRUITER_INTERVIEW_HOLD_MIN_SCORE} <= overall_score <= {RECRUITER_INTERVIEW_PASS_SCORE}: hold
- overall_score < {RECRUITER_INTERVIEW_HOLD_MIN_SCORE}: reject

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
    "eye_movement_summary": "short summary of camera visibility and activity",
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

Camera monitoring:
{json.dumps(normalized_camera_monitoring, default=str)}
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
                "camera_monitoring": normalized_camera_monitoring,
                "final_notes_for_hr": "Please review the transcript manually.",
            }
        report["camera_monitoring"] = normalize_camera_monitoring(camera_monitoring, report.get("camera_monitoring"))
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
            "camera_monitoring": {},
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

    def api_interview_recording(self, token: str):
        application = self.application_for_interview_token(token)
        if not application:
            self.send_json({"error": "Interview link not found."}, status=404)
            return
        if not ONEDRIVE_RECORDINGS_ENABLED:
            self.send_json({"ok": False, "error": "OneDrive recording upload is disabled."}, status=400)
            return
        try:
            recording_bytes = self.read_raw_body()
            if not recording_bytes:
                self.send_json({"ok": False, "error": "Recording file was empty."}, status=400)
                return
            content_type = self.headers.get("Content-Type", "video/webm") or "video/webm"
            extension = ".webm"
            if "mp4" in content_type.lower():
                extension = ".mp4"
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            candidate_label = safe_onedrive_path_part(application.get("full_name") or application.get("candidate_email") or "candidate")
            filename = f"application-{application['id']}-{candidate_label}-{timestamp}{extension}"
            folder = f"{ONEDRIVE_RECORDINGS_FOLDER}/Application {application['id']}"
            uploaded = MicrosoftGraphProvider(ONEDRIVE_RECORDINGS_USER).upload_onedrive_file(
                recording_bytes,
                filename,
                folder=folder,
                content_type=content_type,
                user_email=ONEDRIVE_RECORDINGS_USER,
            )
            recording_info = {
                "filename": uploaded.get("name") or filename,
                "onedrive_id": uploaded.get("id"),
                "web_url": uploaded.get("webUrl"),
                "size": uploaded.get("size") or len(recording_bytes),
                "content_type": content_type,
                "uploaded_at": datetime.utcnow().isoformat() + "Z",
                "onedrive_user": ONEDRIVE_RECORDINGS_USER,
            }
            report = json_object(application.get("interview_report"))
            report["recording"] = recording_info
            database = self.db()
            try:
                database.db.execute(
                    """
                    UPDATE recruiter_applications
                    SET interview_report = %s::jsonb
                    WHERE id = %s
                    """,
                    (json.dumps(report), application["id"]),
                )
            finally:
                database.close()
            log_json(
                logging.INFO,
                "interview_recording_uploaded",
                application_id=application["id"],
                filename=recording_info["filename"],
                size=recording_info["size"],
                web_url=recording_info["web_url"],
            )
            self.send_json({"ok": True, "recording": recording_info})
        except Exception as exc:
            LOGGER.exception("Interview recording upload failed: %s", exc)
            self.send_json({"ok": False, "error": str(exc)}, status=500)

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
                "camera_monitoring": {},
                "followup_for_current": False,
                "last_client_turn_id": 0,
            }
            WEB_INTERVIEW_SESSIONS[token] = session
        answer = str(payload.get("answer") or "").strip()
        camera_monitoring = payload.get("camera_monitoring")
        if isinstance(camera_monitoring, dict):
            session["camera_monitoring"] = normalize_camera_monitoring(camera_monitoring, session.get("camera_monitoring"))
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
            if isinstance(session.get("camera_monitoring"), dict) and not session["camera_monitoring"].get("ended_at"):
                session["camera_monitoring"]["ended_at"] = datetime.utcnow().isoformat() + "Z"
            report = self.web_interview_report(application, session["transcript"], session.get("camera_monitoring"))
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
        camera_monitoring = payload.get("camera_monitoring")
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
        normalized_camera_monitoring = normalize_camera_monitoring(camera_monitoring) if isinstance(camera_monitoring, dict) else None
        if isinstance(normalized_camera_monitoring, dict) and not normalized_camera_monitoring.get("ended_at"):
            normalized_camera_monitoring["ended_at"] = datetime.utcnow().isoformat() + "Z"
        report = self.web_interview_report(
            application,
            cleaned,
            normalized_camera_monitoring,
        )
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

    def update_requirement(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruitment_requirements
                SET position_title = %s,
                    experience_min_years = %s,
                    experience_max_years = %s,
                    budget_min = %s,
                    budget_max = %s,
                    currency = %s,
                    job_description = %s,
                    urgently_required = %s,
                    needed_within_days = %s,
                    status = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
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
                    parse_int(form.get("id")),
                ),
            )
        finally:
            database.close()

    def delete_requirement(self, form: dict[str, str]):
        requirement_id = parse_int(form.get("id"))
        database = self.db()
        try:
            database.execute("UPDATE recruiter_applications SET requirement_id = NULL WHERE requirement_id = %s", (requirement_id,))
            database.execute("DELETE FROM recruitment_requirements WHERE id = %s", (requirement_id,))
        finally:
            database.close()

    def update_candidate(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruiter_candidates
                SET full_name = %s,
                    candidate_email = %s,
                    source_email = %s,
                    referrer_email = %s,
                    submission_type = %s,
                    phone = %s,
                    location = %s,
                    linkedin_url = %s,
                    portfolio_url = %s,
                    current_title = %s,
                    current_company = %s,
                    total_experience_years = %s,
                    cv_summary = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    form.get("full_name", "").strip() or None,
                    form.get("candidate_email", "").strip() or None,
                    form.get("source_email", "").strip() or None,
                    form.get("referrer_email", "").strip() or None,
                    form.get("submission_type", "self_application").strip() or "self_application",
                    form.get("phone", "").strip() or None,
                    form.get("location", "").strip() or None,
                    form.get("linkedin_url", "").strip() or None,
                    form.get("portfolio_url", "").strip() or None,
                    form.get("current_title", "").strip() or None,
                    form.get("current_company", "").strip() or None,
                    parse_decimal(form.get("total_experience_years")),
                    form.get("cv_summary", "").strip() or None,
                    parse_int(form.get("id")),
                ),
            )
        finally:
            database.close()

    def delete_candidate(self, form: dict[str, str]):
        candidate_id = parse_int(form.get("id"))
        database = self.db()
        try:
            database.execute("DELETE FROM recruiter_applications WHERE candidate_id = %s", (candidate_id,))
            database.execute("DELETE FROM recruiter_candidates WHERE id = %s", (candidate_id,))
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

    def update_application(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruiter_applications
                SET candidate_email = %s,
                    source_email = %s,
                    referrer_email = %s,
                    submission_type = %s,
                    requirement_id = %s,
                    detected_position = %s,
                    matched_position = %s,
                    application_status = %s,
                    ats_score = %s,
                    jd_match_score = %s,
                    ai_short_description = %s,
                    screening_current_salary = %s,
                    screening_expected_salary = %s,
                    screening_current_location = %s,
                    screening_joining_days = %s,
                    hr_escalation_reason = %s,
                    interview_availability = %s,
                    hr_interviewer_email = %s,
                    hr_interviewer_name = %s
                WHERE id = %s
                """,
                (
                    form.get("candidate_email", "").strip() or None,
                    form.get("source_email", "").strip() or None,
                    form.get("referrer_email", "").strip() or None,
                    form.get("submission_type", "self_application").strip() or "self_application",
                    parse_int(form.get("requirement_id")),
                    form.get("detected_position", "").strip() or None,
                    form.get("matched_position", "").strip() or None,
                    form.get("application_status", "reviewed").strip() or "reviewed",
                    parse_decimal(form.get("ats_score")),
                    parse_decimal(form.get("jd_match_score")),
                    form.get("ai_short_description", "").strip() or None,
                    parse_decimal(form.get("screening_current_salary")),
                    parse_decimal(form.get("screening_expected_salary")),
                    form.get("screening_current_location", "").strip() or None,
                    parse_int(form.get("screening_joining_days")),
                    form.get("hr_escalation_reason", "").strip() or None,
                    form.get("interview_availability", "").strip() or None,
                    form.get("hr_interviewer_email", "").strip() or None,
                    form.get("hr_interviewer_name", "").strip() or None,
                    parse_int(form.get("id")),
                ),
            )
        finally:
            database.close()

    def delete_application(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute("DELETE FROM recruiter_applications WHERE id = %s", (parse_int(form.get("id")),))
        finally:
            database.close()

    def update_event(self, form: dict[str, str]):
        raw_details = form.get("details", "").strip() or "{}"
        try:
            details = json.dumps(json.loads(raw_details))
        except json.JSONDecodeError:
            details = json.dumps({"note": raw_details})
        database = self.db()
        try:
            database.execute(
                """
                UPDATE recruiter_email_events
                SET event_type = %s,
                    source_email = %s,
                    email_subject = %s,
                    details = %s::jsonb
                WHERE id = %s
                """,
                (
                    form.get("event_type", "").strip() or "manual_event",
                    form.get("source_email", "").strip() or None,
                    form.get("email_subject", "").strip() or None,
                    details,
                    parse_int(form.get("id")),
                ),
            )
        finally:
            database.close()

    def delete_event(self, form: dict[str, str]):
        database = self.db()
        try:
            database.execute("DELETE FROM recruiter_email_events WHERE id = %s", (parse_int(form.get("id")),))
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
        self.send_cv_file(row, f"application-{application_id}-cv.txt", inline=False)

    def view_application_cv(self, application_id: int):
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
        self.send_cv_file(row, f"application-{application_id}-cv.txt", inline=True)

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
        self.send_cv_file(row, f"candidate-{candidate_id}-cv.txt", inline=False)

    def view_candidate_cv(self, candidate_id: int):
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
        self.send_cv_file(row, f"candidate-{candidate_id}-cv.txt", inline=True)

    def send_cv_file(self, row: dict, fallback_name: str, inline: bool = False):
        payload = row.get("attachment_payload")
        filename = row.get("attachment_filename")
        if payload:
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            elif not isinstance(payload, bytes):
                payload = bytes(payload)
            download_name = safe_download_name(filename, fallback_name)
            content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        elif local_path := stored_cv_path(filename):
            payload = local_path.read_bytes()
            download_name = safe_download_name(local_path.name, fallback_name)
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
        disposition = "inline" if inline else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{download_name}"')
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
                    (SELECT COUNT(*) FROM recruiter_applications WHERE requirement_id IS NOT NULL) AS matched,
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
            notifications = self.pending_operator_notifications(database)
            schedule_start = datetime.now(recruiter_tz()).replace(second=0, microsecond=0)
            schedule_end = schedule_start + timedelta(days=7)
            hr_meetings = database.db.final_hr_scheduled_applications(schedule_start, schedule_end)
        finally:
            database.close()

        cards = [
            ("Open Roles", summary["open_roles"], "Roles accepting CVs", "/requirements"),
            ("Candidates", summary["candidates"], "Profiles saved", "/candidates"),
            ("Applications", summary["applications"], "CVs processed", "/applications"),
            ("Matched", summary["matched"], "Linked to requirements", "/applications?matched=1"),
            ("Saved Later", summary["saved_for_later"], "No open role now", "/applications?status=no_open_requirement"),
            ("Avg ATS", score(summary["avg_ats"]), "Across applications", "/applications"),
        ]
        card_html = "".join(
            f"""
            <a class="metric metric-link" href="{html_escape(href)}">
                <span>{html_escape(label)}</span>
                <strong>{html_escape(value)}</strong>
                <small>{html_escape(detail)}</small>
            </a>
            """
            for label, value, detail, href in cards
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

        notification_html = self.render_notifications_panel(notifications)
        hr_schedule_html = self.render_hr_schedule_summary(hr_meetings)

        return f"""
        <section class="metrics">{card_html}</section>
        {notification_html}
        {hr_schedule_html}
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

    def render_hr_schedule_summary(self, meetings: list[dict]) -> str:
        interviewers = final_hr_interviewers()
        grouped = {interviewer["email"].lower(): {"interviewer": interviewer, "meetings": []} for interviewer in interviewers}
        unassigned = {"interviewer": {"name": "Unassigned", "email": ""}, "meetings": []}
        for meeting in meetings:
            email_address = (meeting.get("hr_interviewer_email") or "").lower()
            if email_address in grouped:
                grouped[email_address]["meetings"].append(meeting)
            else:
                unassigned["meetings"].append(meeting)

        cards = []
        for group in list(grouped.values()) + ([unassigned] if unassigned["meetings"] else []):
            interviewer = group["interviewer"]
            rows = group["meetings"]
            next_rows = "".join(
                f"""
                <li>
                    <a href="/applications/{html_escape(row["id"])}">{html_escape(row["full_name"] or "Candidate")}</a>
                    <span>{html_escape(row.get("role") or "-")} · {date_text(row.get("interview_scheduled_at"))}</span>
                    {status_badge(row.get("application_status"))}
                </li>
                """
                for row in rows[:4]
            ) or '<li class="empty">No meetings scheduled.</li>'
            cards.append(
                f"""
                <article class="schedule-card">
                    <div>
                        <strong>{html_escape(interviewer["name"])}</strong>
                        <small>{html_escape(interviewer["email"] or "Not assigned")}</small>
                    </div>
                    <b>{len(rows)}</b>
                    <ul>{next_rows}</ul>
                </article>
                """
            )
        return f"""
        <section class="panel">
            <div class="panel-head">
                <h2>Final HR Schedule</h2>
                <span>Next 7 days · Mon-Fri, 6 PM-1 AM IST</span>
            </div>
            <div class="schedule-grid">{"".join(cards)}</div>
        </section>
        """

    def pending_operator_notifications(self, database: DashboardDB) -> list[dict]:
        tasks = [
            {
                "status": "hr_escalated",
                "title": "HR approval required",
                "detail": "Screening needs HR review before moving forward.",
                "action": "Review",
            },
            {
                "status": "interview_on_hold_hr_review",
                "title": "AI interview on hold",
                "detail": "AI interview report needs HR approval or rejection.",
                "action": "Decide",
            },
            {
                "status": "rejected_jd_score",
                "title": "JD-score rejection",
                "detail": "AI rejected a CV based on low JD match. HR can revoke and continue.",
                "action": "Review",
            },
            {
                "status": "manual_hr_review",
                "title": "Manual HR review",
                "detail": "The agent stopped replying because the thread needs human handling.",
                "action": "Review",
            },
            {
                "status": "final_hr_round_completed_pending_decision",
                "title": "Final HR decision pending",
                "detail": "Final HR round is complete. Select, reject, hold, or reschedule.",
                "action": "Decide",
            },
            {
                "status": "interview_availability_received",
                "title": "Teams link pending",
                "detail": "Candidate shared timing but no Teams link has been sent yet.",
                "action": "Schedule",
            },
            {
                "status": "hr_round_time_requested",
                "title": "Waiting for HR round availability",
                "detail": "Candidate needs to share final HR round slots.",
                "action": "Track",
            },
            {
                "status": "screening_negotiation",
                "title": "Screening negotiation active",
                "detail": "Salary or joining details need follow-up.",
                "action": "Review",
            },
        ]
        notifications = []
        for task in tasks:
            row = database.one(
                """
                SELECT COUNT(*) AS count
                FROM recruiter_applications
                WHERE application_status = %s
                """,
                (task["status"],),
            )
            count = int(row["count"] or 0) if row else 0
            if count:
                notifications.append({**task, "count": count, "href": f"/applications?status={task['status']}"})
        return notifications

    def pending_operator_count(self) -> int:
        database = self.db()
        try:
            row = database.one(
                """
                SELECT COUNT(*) AS count
                FROM recruiter_applications
                WHERE application_status IN (
                    'hr_escalated',
                    'interview_on_hold_hr_review',
                    'rejected_jd_score',
                    'manual_hr_review',
                    'final_hr_round_completed_pending_decision',
                    'interview_availability_received',
                    'hr_round_time_requested',
                    'screening_negotiation'
                )
                """
            )
            return int(row["count"] or 0) if row else 0
        except Exception:
            return 0
        finally:
            database.close()

    def render_notifications_panel(self, notifications: list[dict]) -> str:
        if not notifications:
            return """
            <section class="panel notice-panel notice-panel-ok">
                <div class="panel-head">
                    <h2>Operator Notifications</h2>
                    <span>All clear</span>
                </div>
                <p class="muted">No pending recruiter actions right now.</p>
            </section>
            """
        items = "".join(
            f"""
            <a class="notification-card" href="{html_escape(item["href"])}">
                <strong>{html_escape(item["title"])}</strong>
                <span>{html_escape(item["count"])} pending</span>
                <small>{html_escape(item["detail"])}</small>
                <em>{html_escape(item["action"])}</em>
            </a>
            """
            for item in notifications
        )
        total = sum(int(item["count"]) for item in notifications)
        return f"""
        <section class="panel notice-panel">
            <div class="panel-head">
                <h2>Operator Notifications</h2>
                <span>{html_escape(total)} pending</span>
            </div>
            <div class="notification-grid">{items}</div>
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
                    {self.delete_form("/requirements/delete", row["id"], "Delete", "Delete this requirement? Linked applications will stay, but will be detached from this role.")}
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
                        <a class="link-button" href="/candidates/{html_escape(row["id"])}/cv/view" target="_blank" rel="noopener">View CV</a>
                        <a class="link-button" href="/candidates/{html_escape(row["id"])}/cv">Download CV</a>
                        {self.delete_form("/candidates/delete", row["id"], "Delete", "Delete this candidate and all linked applications?")}
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

    def application_filter_state(self, query: dict[str, list[str]]) -> dict:
        q = (query.get("q", [""])[0] or "").strip()
        status_filter = (query.get("status", [""])[0] or "").strip()
        matched_filter = (query.get("matched", [""])[0] or "").strip().lower() in {"1", "true", "yes"}
        created_from_text = (query.get("created_from", [""])[0] or "").strip()
        created_to_text = (query.get("created_to", [""])[0] or "").strip()
        created_from = parse_date_filter(created_from_text)
        created_to = parse_date_filter(created_to_text)
        return {
            "q": q,
            "status": status_filter,
            "matched": matched_filter,
            "created_from_text": created_from_text if created_from else "",
            "created_to_text": created_to_text if created_to else "",
            "created_from": created_from,
            "created_to": created_to,
        }

    def application_filter_conditions(self, state: dict) -> tuple[str, tuple]:
        conditions = []
        params_list = []
        q = state["q"]
        if q:
            conditions.append(
                """
                (
                    LOWER(COALESCE(rc.full_name, '')) LIKE %s
                    OR LOWER(COALESCE(ra.candidate_email, '')) LIKE %s
                    OR LOWER(COALESCE(ra.source_email, '')) LIKE %s
                    OR LOWER(COALESCE(ra.detected_position, '')) LIKE %s
                    OR LOWER(COALESCE(ra.matched_position, '')) LIKE %s
                    OR LOWER(COALESCE(ra.application_status, '')) LIKE %s
                )
                """
            )
            term = f"%{q.lower()}%"
            params_list.extend([term, term, term, term, term, term])
        if state["status"]:
            conditions.append("ra.application_status = %s")
            params_list.append(state["status"])
        if state["matched"]:
            conditions.append("ra.requirement_id IS NOT NULL")
        if state["created_from"]:
            conditions.append("ra.created_at >= %s")
            params_list.append(state["created_from"])
        if state["created_to"]:
            conditions.append("ra.created_at < %s")
            params_list.append(state["created_to"] + timedelta(days=1))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, tuple(params_list)

    def fetch_application_rows(self, query: dict[str, list[str]], limit: int | None = 100) -> tuple[list[dict], dict]:
        state = self.application_filter_state(query)
        where, params = self.application_filter_conditions(state)
        limit_clause = f"LIMIT {limit}" if limit else ""
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
                {limit_clause}
                """,
                params,
            )
        finally:
            database.close()
        return rows, state

    def application_filter_url(self, state: dict, path: str = "/applications", status: str | None = None) -> str:
        params = {}
        if state["q"]:
            params["q"] = state["q"]
        selected_status = status if status is not None else state["status"]
        if selected_status:
            params["status"] = selected_status
        if state["matched"]:
            params["matched"] = "1"
        if state["created_from_text"]:
            params["created_from"] = state["created_from_text"]
        if state["created_to_text"]:
            params["created_to"] = state["created_to_text"]
        return f"{path}?{urlencode(params)}" if params else path

    def render_applications(self, query: dict[str, list[str]]) -> str:
        rows, state = self.fetch_application_rows(query)
        q = state["q"]
        status_filter = state["status"]
        matched_filter = state["matched"]

        def filtered_applications_url(status: str | None = None) -> str:
            return self.application_filter_url(state, status=status)

        table_rows = "".join(
            f"""
            <tr>
                <td><a href="/applications/{html_escape(row["id"])}"><strong>{html_escape(row["full_name"] or "Unnamed candidate")}</strong></a><small>{html_escape(row["candidate_email"] or row["source_email"] or "")}</small></td>
                <td>{html_escape(row["matched_position"] or row["detected_position"] or row["requirement_position"] or "-")}</td>
                <td><a class="status-filter-link" href="{html_escape(filtered_applications_url(row["application_status"]))}" title="Filter by this status">{status_badge(row["application_status"])}</a></td>
                <td>{score(row["ats_score"])}</td>
                <td>{score(row["jd_match_score"])}</td>
                <td>{list_text(row["strengths"], 3)}</td>
                <td>{list_text(row["risks"], 3)}</td>
                <td>{html_escape(row["ai_short_description"] or "-")}</td>
                <td>{date_text(row["created_at"])}</td>
                <td>
                    <div class="action-stack">
                    <a class="link-button" href="/applications/{html_escape(row["id"])}">View</a>
                    <a class="link-button" href="/applications/{html_escape(row["id"])}/cv/view" target="_blank" rel="noopener">View CV</a>
                    <a class="link-button" href="/applications/{html_escape(row["id"])}/cv">Download CV</a>
                    <form method="post" action="/applications/status" class="inline-form">
                        <input type="hidden" name="id" value="{html_escape(row["id"])}">
                        <select name="application_status">
                            {self.status_options(row["application_status"])}
                        </select>
                        <button type="submit">Update</button>
                    </form>
                    {self.delete_form("/applications/delete", row["id"], "Delete", "Delete this application?")}
                    </div>
                </td>
            </tr>
            """
            for row in rows
        ) or '<tr><td colspan="10" class="empty">No applications found.</td></tr>'

        filter_parts = []
        if status_filter:
            filter_parts.append(f"status: {status_badge(status_filter)}")
        if matched_filter:
            filter_parts.append("matched to requirement")
        if state["created_from_text"] or state["created_to_text"]:
            start = state["created_from_text"] or "start"
            end = state["created_to_text"] or "today"
            filter_parts.append(f"created: {html_escape(start)} to {html_escape(end)}")
        filter_label = (
            f'<p class="filter-note">Filtered by {" and ".join(filter_parts)} <a href="/applications">Clear</a></p>'
            if filter_parts
            else ""
        )
        status_filter_options = self.status_filter_options(status_filter)
        matched_checked = "checked" if matched_filter else ""
        export_url = self.application_filter_url(state, path="/applications/export")
        filter_bar = f"""
        <form method="get" action="/applications" class="filter-bar">
            <input type="hidden" name="q" value="{html_escape(q)}">
            <label>Status
                <select name="status">
                    {status_filter_options}
                </select>
            </label>
            <label class="checkbox-label">
                <input type="checkbox" name="matched" value="1" {matched_checked}>
                Matched only
            </label>
            <label>Created from
                <input type="date" name="created_from" value="{html_escape(state["created_from_text"])}">
            </label>
            <label>Created to
                <input type="date" name="created_to" value="{html_escape(state["created_to_text"])}">
            </label>
            <button type="submit">Apply Filter</button>
            <a class="link-button" href="{html_escape(export_url)}">Export Excel</a>
            <a href="/applications">Clear</a>
        </form>
        """
        return f"""
        {self.search_form("/applications", q, "Search application, role, email, or status")}
        {filter_bar}
        {filter_label}
        <section class="panel applications-panel">
            <div class="panel-head"><h2>Applications</h2><span>{len(rows)} shown</span></div>
            <div class="table-scroll">
            <table class="sticky-table">
                <thead><tr><th>Candidate</th><th>Role</th><th>Status</th><th>ATS</th><th>JD</th><th>Strengths</th><th>Risks</th><th>AI Summary</th><th>Created At</th><th>Action</th></tr></thead>
                <tbody>{table_rows}</tbody>
            </table>
            </div>
        </section>
        """

    def export_applications(self, query: dict[str, list[str]]):
        rows, state = self.fetch_application_rows(query, limit=None)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Application ID",
                "Candidate",
                "Candidate Email",
                "Source Email",
                "Role",
                "Status",
                "ATS Score",
                "JD Match Score",
                "Strengths",
                "Risks",
                "AI Summary",
                "Created At",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("id"),
                    row.get("full_name") or "Unnamed candidate",
                    row.get("candidate_email") or "",
                    row.get("source_email") or "",
                    row.get("matched_position") or row.get("detected_position") or row.get("requirement_position") or "",
                    row.get("application_status") or "",
                    score(row.get("ats_score")),
                    score(row.get("jd_match_score")),
                    list_plain_text(row.get("strengths")),
                    list_plain_text(row.get("risks")),
                    row.get("ai_short_description") or "",
                    date_text(row.get("created_at")),
                ]
            )

        suffix_parts = []
        if state["status"]:
            suffix_parts.append(state["status"])
        if state["created_from_text"] or state["created_to_text"]:
            suffix_parts.append(f"{state['created_from_text'] or 'start'}-to-{state['created_to_text'] or 'today'}")
        suffix = "-" + safe_download_name("-".join(suffix_parts), "filtered") if suffix_parts else ""
        filename = f"applications{suffix}.csv"
        payload = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

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
                <td>
                    <div class="action-stack">
                        <a class="link-button" href="/events/{html_escape(row["id"])}">View</a>
                        {self.delete_form("/events/delete", row["id"], "Delete", "Delete this email event log?")}
                    </div>
                </td>
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
        edit_form = f"""
        <section class="panel">
            <div class="panel-head"><h2>Edit Requirement</h2></div>
            <form method="post" action="/requirements/update" class="requirement-form">
                <input type="hidden" name="id" value="{html_escape(requirement_id)}">
                <label>Position<input name="position_title" required value="{html_escape(row["position_title"])}"></label>
                <label>Min Exp<input name="experience_min_years" type="number" step="0.1" value="{html_escape(row["experience_min_years"] or "")}"></label>
                <label>Max Exp<input name="experience_max_years" type="number" step="0.1" value="{html_escape(row["experience_max_years"] or "")}"></label>
                <label>Budget Min<input name="budget_min" type="number" value="{html_escape(row["budget_min"] or "")}"></label>
                <label>Budget Max<input name="budget_max" type="number" value="{html_escape(row["budget_max"] or "")}"></label>
                <label>Currency<input name="currency" value="{html_escape(row["currency"] or "INR")}"></label>
                <label>Needed Days<input name="needed_within_days" type="number" value="{html_escape(row["needed_within_days"] or "")}"></label>
                <label>Status<select name="status"><option value="open" {"selected" if row["status"] == "open" else ""}>Open</option><option value="closed" {"selected" if row["status"] == "closed" else ""}>Closed</option></select></label>
                <label class="check"><input name="urgently_required" type="checkbox" {"checked" if row["urgently_required"] else ""}> Urgent</label>
                <label class="wide">Job Description<textarea name="job_description" required rows="6">{html_escape(row["job_description"] or "")}</textarea></label>
                <button type="submit">Save Changes</button>
            </form>
        </section>
        """
        return f"""
        {self.back_link("/requirements", "Back to requirements")}
        <section class="panel">
            <div class="panel-head">
                <h2>{html_escape(row["position_title"])}</h2>
                <div class="action-stack">
                    {status_badge(row["status"])}
                    {self.delete_form("/requirements/delete", requirement_id, "Delete Requirement", "Delete this requirement? Linked applications will stay, but will be detached from this role.")}
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        {edit_form}
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
                <td>
                    <div class="action-stack">
                        <a class="link-button" href="/applications/{html_escape(app["id"])}/cv/view" target="_blank" rel="noopener">View CV</a>
                        <a class="link-button" href="/applications/{html_escape(app["id"])}/cv">Download CV</a>
                    </div>
                </td>
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
        edit_form = f"""
        <section class="panel">
            <div class="panel-head"><h2>Edit Candidate</h2></div>
            <form method="post" action="/candidates/update" class="requirement-form">
                <input type="hidden" name="id" value="{html_escape(candidate_id)}">
                <label>Full Name<input name="full_name" value="{html_escape(row["full_name"] or "")}"></label>
                <label>Candidate Email<input name="candidate_email" type="email" value="{html_escape(row["candidate_email"] or "")}"></label>
                <label>Source Email<input name="source_email" type="email" value="{html_escape(row["source_email"] or "")}"></label>
                <label>Referrer Email<input name="referrer_email" type="email" value="{html_escape(row["referrer_email"] or "")}"></label>
                <label>Submission Type<select name="submission_type"><option value="self_application" {"selected" if row["submission_type"] == "self_application" else ""}>Self Application</option><option value="referral" {"selected" if row["submission_type"] == "referral" else ""}>Referral</option></select></label>
                <label>Phone<input name="phone" value="{html_escape(row["phone"] or "")}"></label>
                <label>Location<input name="location" value="{html_escape(row["location"] or "")}"></label>
                <label>LinkedIn<input name="linkedin_url" value="{html_escape(row["linkedin_url"] or "")}"></label>
                <label>Portfolio<input name="portfolio_url" value="{html_escape(row["portfolio_url"] or "")}"></label>
                <label>Current Title<input name="current_title" value="{html_escape(row["current_title"] or "")}"></label>
                <label>Current Company<input name="current_company" value="{html_escape(row["current_company"] or "")}"></label>
                <label>Total Experience<input name="total_experience_years" type="number" step="0.1" value="{html_escape(row["total_experience_years"] or "")}"></label>
                <label class="wide">CV Summary<textarea name="cv_summary" rows="4">{html_escape(row["cv_summary"] or "")}</textarea></label>
                <button type="submit">Save Changes</button>
            </form>
        </section>
        """
        return f"""
        {self.back_link("/candidates", "Back to candidates")}
        <section class="panel">
            <div class="panel-head">
                <h2>{html_escape(title)}</h2>
                <div class="action-stack">
                    {status_badge(row["submission_type"])}
                    <a class="link-button" href="/candidates/{html_escape(candidate_id)}/cv/view" target="_blank" rel="noopener">View CV</a>
                    <a class="link-button" href="/candidates/{html_escape(candidate_id)}/cv">Download CV</a>
                    {self.delete_form("/candidates/delete", candidate_id, "Delete Candidate", "Delete this candidate and all linked applications?")}
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        {edit_form}
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
            requirements = database.rows(
                """
                SELECT id, position_title, status
                FROM recruitment_requirements
                ORDER BY LOWER(status) = 'open' DESC, position_title ASC
                LIMIT 200
                """
            )
        finally:
            database.close()

        interview_report = json_object(row.get("interview_report"))
        recording = json_object(interview_report.get("recording"))
        recording_url = recording.get("web_url")
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
            ("Interview Recording", self.link_value(recording_url) if recording_url else None, "html"),
            ("Interview Started At", date_text(row.get("interview_started_at"))),
            ("Interview Completed At", date_text(row.get("interview_completed_at"))),
            ("HR Interviewer", row.get("hr_interviewer_name") or row.get("hr_interviewer_email")),
            ("HR Interviewer Email", row.get("hr_interviewer_email")),
            ("Teams Event ID", row.get("teams_event_id")),
            ("Teams Join URL", row.get("teams_join_url")),
            ("Interview Score", interview_report.get("overall_score")),
            ("Interview Recommendation", interview_report.get("recommendation")),
            ("Interview Summary", interview_report.get("summary"), "pre-wide"),
            ("Interview Plus Points", interview_report.get("plus_points"), "json"),
            ("Interview Negative Points", interview_report.get("negative_points"), "json"),
            ("Camera Monitoring", interview_report.get("camera_monitoring"), "json"),
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
        requirement_options = '<option value="">No requirement</option>' + "".join(
            f'<option value="{html_escape(requirement["id"])}" {"selected" if requirement["id"] == row.get("requirement_id") else ""}>{html_escape(requirement["position_title"])} ({html_escape(requirement["status"])})</option>'
            for requirement in requirements
        )
        edit_form = f"""
        <section class="panel">
            <div class="panel-head"><h2>Edit Application</h2></div>
            <form method="post" action="/applications/update" class="requirement-form">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <label>Candidate Email<input name="candidate_email" type="email" value="{html_escape(row["candidate_email"] or "")}"></label>
                <label>Source Email<input name="source_email" type="email" value="{html_escape(row["source_email"] or "")}"></label>
                <label>Referrer Email<input name="referrer_email" type="email" value="{html_escape(row["referrer_email"] or "")}"></label>
                <label>Submission Type<select name="submission_type"><option value="self_application" {"selected" if row["submission_type"] == "self_application" else ""}>Self Application</option><option value="referral" {"selected" if row["submission_type"] == "referral" else ""}>Referral</option></select></label>
                <label>Requirement<select name="requirement_id">{requirement_options}</select></label>
                <label>Detected Position<input name="detected_position" value="{html_escape(row["detected_position"] or "")}"></label>
                <label>Matched Position<input name="matched_position" value="{html_escape(row["matched_position"] or "")}"></label>
                <label>Status<select name="application_status">{self.status_options(row["application_status"])}</select></label>
                <label>ATS Score<input name="ats_score" type="number" step="0.1" value="{html_escape(row["ats_score"] or "")}"></label>
                <label>JD Match Score<input name="jd_match_score" type="number" step="0.1" value="{html_escape(row["jd_match_score"] or "")}"></label>
                <label>Current Salary<input name="screening_current_salary" type="number" value="{html_escape(row.get("screening_current_salary") or "")}"></label>
                <label>Expected Salary<input name="screening_expected_salary" type="number" value="{html_escape(row.get("screening_expected_salary") or "")}"></label>
                <label>Current Location<input name="screening_current_location" value="{html_escape(row.get("screening_current_location") or "")}"></label>
                <label>Joining Days<input name="screening_joining_days" type="number" value="{html_escape(row.get("screening_joining_days") or "")}"></label>
                <label>HR Interviewer Email<input name="hr_interviewer_email" type="email" value="{html_escape(row.get("hr_interviewer_email") or "")}"></label>
                <label>HR Interviewer Name<input name="hr_interviewer_name" value="{html_escape(row.get("hr_interviewer_name") or "")}"></label>
                <label class="wide">AI Short Description<textarea name="ai_short_description" rows="3">{html_escape(row["ai_short_description"] or "")}</textarea></label>
                <label class="wide">HR Escalation Reason<textarea name="hr_escalation_reason" rows="3">{html_escape(row.get("hr_escalation_reason") or "")}</textarea></label>
                <label class="wide">Interview Availability<textarea name="interview_availability" rows="3">{html_escape(row.get("interview_availability") or "")}</textarea></label>
                <button type="submit">Save Changes</button>
            </form>
        </section>
        """
        hr_approve_form = ""
        if (row["application_status"] or "").lower() == "hr_escalated":
            hr_approve_form = f"""
            <form method="post" action="/applications/hr-approve" class="inline-form" onsubmit="return confirm('Approve this candidate and send the interview email?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Approve For Interview</button>
            </form>
            """
        post_interview_review_forms = ""
        if (row["application_status"] or "").lower() == "interview_on_hold_hr_review":
            post_interview_review_forms = f"""
            <form method="post" action="/applications/post-interview-approve" class="inline-form" onsubmit="return confirm('Approve this candidate for the final HR round and email the candidate?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Approve For HR Round</button>
            </form>
            <form method="post" action="/applications/post-interview-reject" class="inline-form" onsubmit="return confirm('Reject this candidate and send the rejection email?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit" class="danger">Reject Candidate</button>
            </form>
            """
        jd_rejection_form = ""
        if (row["application_status"] or "").lower() == "rejected_jd_score":
            jd_rejection_form = f"""
            <form method="post" action="/applications/revoke-jd-rejection" class="inline-form" onsubmit="return confirm('Revoke the JD-score rejection and send screening questions to this candidate?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Revoke JD Rejection</button>
            </form>
            """
        final_hr_decision_forms = ""
        if (row["application_status"] or "").lower() == "final_hr_round_completed_pending_decision":
            interviewer_options = '<option value="">Auto assign</option>' + "".join(
                f'<option value="{html_escape(interviewer["email"])}">{html_escape(interviewer["name"])} ({html_escape(interviewer["email"])})</option>'
                for interviewer in final_hr_interviewers()
            )
            final_hr_decision_forms = f"""
            <form method="post" action="/applications/final-select" class="inline-form" onsubmit="return confirm('Select this candidate and send the onboarding documents email?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Select Candidate</button>
            </form>
            <form method="post" action="/applications/final-reject" class="inline-form" onsubmit="return confirm('Reject this candidate after the final HR round and send the rejection email?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit" class="danger">Reject Candidate</button>
            </form>
            <form method="post" action="/applications/final-hold" class="inline-form" onsubmit="return confirm('Put this candidate on hold and send the update email?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit" class="secondary">Put On Hold</button>
            </form>
            <form method="post" action="/applications/final-reschedule" class="inline-form" onsubmit="return configureHrReschedule(this);">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <input type="hidden" name="reschedule_mode" value="ask_candidate">
                <input type="hidden" name="scheduled_at" value="">
                <select name="interviewer_email" class="hr-interviewer-select">
                    {interviewer_options}
                </select>
                <button type="submit" class="secondary">Reschedule HR Round</button>
            </form>
            """
        send_teams_form = ""
        if row.get("interview_scheduled_at") and not row.get("teams_join_url"):
            send_teams_form = f"""
            <form method="post" action="/applications/send-teams-link" class="inline-form" onsubmit="return confirm('Create or send the Teams meeting link to this candidate?');">
                <input type="hidden" name="id" value="{html_escape(application_id)}">
                <button type="submit">Send Teams Link</button>
            </form>
            """
        send_interview_form = f"""
            <form method="post" action="/applications/send-interview-link" class="inline-form" onsubmit="return confirm('Send the AI interview link to this candidate?');">
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
                    <a class="link-button" href="/applications/{html_escape(application_id)}/cv/view" target="_blank" rel="noopener">View CV</a>
                    <a class="link-button" href="/applications/{html_escape(application_id)}/cv">Download CV</a>
                    {self.delete_form("/applications/delete", application_id, "Delete Application", "Delete this application?")}
                    {hr_approve_form}
                    {post_interview_review_forms}
                    {jd_rejection_form}
                    {final_hr_decision_forms}
                    {send_interview_form}
                    {send_teams_form}
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        {edit_form}
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
    .browser-gate {{
      padding: 16px;
      border-radius: 8px;
      background: #fff7ed;
      color: #9a3412;
      border: 1px solid #fed7aa;
      line-height: 1.5;
      margin-top: 14px;
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
      <div id="browserGate" class="browser-gate hidden">
        Please open this interview link in Google Chrome. This voice interview needs Chrome speech recognition for the microphone and live transcript.
      </div>
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
    let screenStream = null;
    let recordingStream = null;
    let interviewRecorder = null;
    let interviewRecordingChunks = [];
    let recordingUploadStarted = false;
    let finalTranscript = '';
    let isRecording = false;
    let autoAdvanceTimer = null;
    let speechStarted = false;
    let isSubmitting = false;
    let processingNudgeTimer = null;
    let processingNudgeSpoken = false;
    let preferredInterviewVoice = null;
    let activeSpeechId = 0;
    let flowVersion = 0;
    let interviewClosed = false;
    let clientTurnId = 0;
    let audioContext = null;
    let audioMonitorId = null;
    let lastSpeechResultAt = 0;
    let lastMicActivityAt = 0;
    let lastTranscriptChangeAt = 0;
    let lastFinalTranscriptAt = 0;
    let finalResultAutoAdvanceTimer = null;
    let cameraCanvas = null;
    let cameraContext = null;
    let cameraMonitorTimer = null;
    let faceDetector = null;
    let faceDetectionSupported = false;
    let lastCameraFrame = null;
    let noFaceStreak = 0;
    let lowLightStreak = 0;
    let cameraMonitoring = {{
      available: false,
      started_at: null,
      ended_at: null,
      method: 'browser_camera_sampling',
      face_detection_supported: false,
      sample_count: 0,
      face_present_samples: 0,
      no_face_samples: 0,
      multiple_face_samples: 0,
      low_light_samples: 0,
      high_motion_events: 0,
      unusual_activity: []
    }};
    const ANSWER_SILENCE_MS = 2200;
    const LONG_ANSWER_SILENCE_MS = 3200;
    const INCOMPLETE_ANSWER_SILENCE_MS = 7000;
    const FINAL_TRANSCRIPT_GRACE_MS = 1400;
    const PROCESSING_NUDGE_MS = {int(RECRUITER_BROWSER_PROCESSING_NUDGE_MS)};
    const MIC_ACTIVITY_THRESHOLD = 0.026;
    const RECORDING_MIME_TYPE = 'video/webm;codecs=vp8,opus';
    const FEMALE_VOICE_HINTS = {json.dumps([hint.strip().lower() for hint in RECRUITER_BROWSER_TTS_VOICE_HINTS.split(",") if hint.strip()])};

    const questionEl = document.getElementById('question');
    const answerEl = document.getElementById('answer');
    const messageEl = document.getElementById('message');
    const browserGateEl = document.getElementById('browserGate');
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

    function isSupportedInterviewBrowser() {{
      const ua = navigator.userAgent || '';
      const vendor = navigator.vendor || '';
      const hasSpeechRecognition = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
      const blocked = /Firefox\\//.test(ua) || /Edg\\//.test(ua) || /OPR\\//.test(ua) || (/Safari\\//.test(ua) && !/Chrome\\//.test(ua));
      const isChrome = (/Chrome\\//.test(ua) || /Chromium\\//.test(ua)) && /Google Inc/.test(vendor);
      return hasSpeechRecognition && isChrome && !blocked;
    }}

    function enforceSupportedBrowser() {{
      if (isSupportedInterviewBrowser()) return true;
      if (browserGateEl) browserGateEl.classList.remove('hidden');
      if (interviewBox) interviewBox.classList.add('hidden');
      setMessage('');
      startBtn.disabled = true;
      return false;
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

    function chooseInterviewVoice() {{
      const voices = window.speechSynthesis ? window.speechSynthesis.getVoices() : [];
      if (!voices.length) return null;
      const englishVoices = voices.filter(voice => (voice.lang || '').toLowerCase().startsWith('en'));
      const candidates = englishVoices.length ? englishVoices : voices;
      return candidates.find(voice => {{
        const name = `${{voice.name}} ${{voice.voiceURI}}`.toLowerCase();
        return FEMALE_VOICE_HINTS.some(hint => name.includes(hint));
      }}) || candidates[0] || null;
    }}

    function refreshInterviewVoice() {{
      preferredInterviewVoice = chooseInterviewVoice();
    }}

    if (window.speechSynthesis) {{
      refreshInterviewVoice();
      window.speechSynthesis.onvoiceschanged = refreshInterviewVoice;
    }}

    function speak(text, onend) {{
      const speechId = ++activeSpeechId;
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      if (!preferredInterviewVoice) refreshInterviewVoice();
      if (preferredInterviewVoice) utterance.voice = preferredInterviewVoice;
      utterance.rate = 0.95;
      utterance.pitch = 1.08;
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
      if (finalResultAutoAdvanceTimer) {{
        window.clearTimeout(finalResultAutoAdvanceTimer);
        finalResultAutoAdvanceTimer = null;
      }}
    }}

    function clearProcessingNudge() {{
      if (processingNudgeTimer) {{
        window.clearTimeout(processingNudgeTimer);
        processingNudgeTimer = null;
      }}
    }}

    function answerWordCount() {{
      return answerEl.value.trim().split(/\\s+/).filter(Boolean).length;
    }}

    function currentSilenceMs() {{
      const now = Date.now();
      const lastTextActivity = Math.max(lastTranscriptChangeAt || 0, lastFinalTranscriptAt || 0);
      const lastActivity = speechStarted ? lastTextActivity : Math.max(lastSpeechResultAt || 0, lastMicActivityAt || 0);
      return lastActivity ? now - lastActivity : 0;
    }}

    function answerLooksIncomplete() {{
      const text = answerEl.value.trim().toLowerCase();
      if (!text) return true;
      const incompleteEndings = [
        'and', 'or', 'but', 'so', 'because', 'like', 'then', 'actually',
        'for example', 'such as', 'i mean', 'let me think', 'one second',
        'wait', 'just a moment'
      ];
      return incompleteEndings.some(ending => text.endsWith(ending));
    }}

    function answerHasExplicitCompletion() {{
      const text = answerEl.value.trim().toLowerCase();
      const completionPhrases = [
        "that's all", "that is all", "that's it", "that is it",
        "i'm done", "i am done", "that's my answer", "that is my answer",
        "next question", "skip this", "skip question", "i don't know", "i do not know"
      ];
      return completionPhrases.some(phrase => text.includes(phrase));
    }}

    function answerCanAutoSubmit() {{
      const words = answerWordCount();
      if (interviewPhase === 'greeting') return words >= 1;
      if (answerHasExplicitCompletion()) return true;
      if (answerLooksIncomplete()) return false;
      return words >= 10;
    }}

    function answerSilenceThresholdMs() {{
      if (answerLooksIncomplete()) return INCOMPLETE_ANSWER_SILENCE_MS;
      return answerWordCount() >= 45 ? LONG_ANSWER_SILENCE_MS : ANSWER_SILENCE_MS;
    }}

    function scheduleFinalResultAutoAdvance() {{
      if (finalResultAutoAdvanceTimer) window.clearTimeout(finalResultAutoAdvanceTimer);
      finalResultAutoAdvanceTimer = window.setTimeout(() => {{
        if (
          isRecording &&
          !isSubmitting &&
          !interviewClosed &&
          answerCanAutoSubmit() &&
          Date.now() - lastFinalTranscriptAt >= FINAL_TRANSCRIPT_GRACE_MS
        ) {{
          stopListeningAndAdvance();
        }}
      }}, FINAL_TRANSCRIPT_GRACE_MS);
    }}

    function scheduleAutoAdvance() {{
      clearAutoAdvanceTimer();
      autoAdvanceTimer = window.setTimeout(() => {{
        const threshold = answerSilenceThresholdMs();
        const silentFor = currentSilenceMs();
        if (
          isRecording &&
          !isSubmitting &&
          !interviewClosed &&
          silentFor >= threshold &&
          answerCanAutoSubmit()
        ) {{
          stopListeningAndAdvance();
          return;
        }}
        if (isRecording && !isSubmitting && !interviewClosed) {{
          scheduleAutoAdvance();
        }}
      }}, Math.max(900, Math.min(answerSilenceThresholdMs(), 1400)));
    }}

    function startAudioActivityMonitor() {{
      if (!mediaStream || audioMonitorId) return;
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      try {{
        audioContext = audioContext || new AudioContext();
        const source = audioContext.createMediaStreamSource(mediaStream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        const data = new Uint8Array(analyser.fftSize);
        const tick = () => {{
          analyser.getByteTimeDomainData(data);
          let sum = 0;
          for (let i = 0; i < data.length; i++) {{
            const value = (data[i] - 128) / 128;
            sum += value * value;
          }}
          const rms = Math.sqrt(sum / data.length);
          if (rms >= MIC_ACTIVITY_THRESHOLD && speechStarted) {{
            lastMicActivityAt = Date.now();
          }}
          audioMonitorId = window.requestAnimationFrame(tick);
        }};
        tick();
      }} catch (error) {{
        audioMonitorId = null;
      }}
    }}

    function pushCameraActivity(type, detail) {{
      const last = cameraMonitoring.unusual_activity[cameraMonitoring.unusual_activity.length - 1];
      if (last && last.type === type && Date.now() - new Date(last.at).getTime() < 12000) return;
      cameraMonitoring.unusual_activity.push({{
        at: new Date().toISOString(),
        type,
        detail
      }});
      if (cameraMonitoring.unusual_activity.length > 25) {{
        cameraMonitoring.unusual_activity = cameraMonitoring.unusual_activity.slice(-25);
      }}
    }}

    async function sampleCameraFrame() {{
      if (!mediaStream || !candidateVideo || candidateVideo.readyState < 2 || interviewClosed) return;
      try {{
        cameraCanvas = cameraCanvas || document.createElement('canvas');
        cameraCanvas.width = 160;
        cameraCanvas.height = 90;
        cameraContext = cameraContext || cameraCanvas.getContext('2d', {{willReadFrequently: true}});
        if (!cameraContext) return;
        cameraContext.drawImage(candidateVideo, 0, 0, cameraCanvas.width, cameraCanvas.height);
        const frame = cameraContext.getImageData(0, 0, cameraCanvas.width, cameraCanvas.height).data;
        let brightnessSum = 0;
        let diffSum = 0;
        for (let i = 0; i < frame.length; i += 4) {{
          const brightness = (frame[i] + frame[i + 1] + frame[i + 2]) / 3;
          brightnessSum += brightness;
          if (lastCameraFrame) {{
            diffSum += Math.abs(frame[i] - lastCameraFrame[i]);
            diffSum += Math.abs(frame[i + 1] - lastCameraFrame[i + 1]);
            diffSum += Math.abs(frame[i + 2] - lastCameraFrame[i + 2]);
          }}
        }}
        const pixels = frame.length / 4;
        const avgBrightness = brightnessSum / pixels;
        const avgMotion = lastCameraFrame ? diffSum / (pixels * 3) : 0;
        lastCameraFrame = new Uint8ClampedArray(frame);
        cameraMonitoring.sample_count += 1;

        if (avgBrightness < 28) {{
          cameraMonitoring.low_light_samples += 1;
          lowLightStreak += 1;
          if (lowLightStreak >= 3) pushCameraActivity('low_light', 'Candidate video was too dark for several samples.');
        }} else {{
          lowLightStreak = 0;
        }}
        if (avgMotion > 42) {{
          cameraMonitoring.high_motion_events += 1;
          pushCameraActivity('high_motion', 'Large camera movement or visual change detected.');
        }}

        if (faceDetector) {{
          const faces = await faceDetector.detect(candidateVideo);
          if (faces.length === 0) {{
            cameraMonitoring.no_face_samples += 1;
            noFaceStreak += 1;
            if (noFaceStreak >= 3) pushCameraActivity('no_face_visible', 'No face was visible for several consecutive samples.');
          }} else {{
            cameraMonitoring.face_present_samples += 1;
            noFaceStreak = 0;
            if (faces.length > 1) {{
              cameraMonitoring.multiple_face_samples += 1;
              pushCameraActivity('multiple_faces', 'More than one face was visible in the camera frame.');
            }}
          }}
        }}
      }} catch (error) {{
        pushCameraActivity('camera_monitor_error', 'Camera monitoring sample failed in the browser.');
      }}
    }}

    function startCameraMonitoring() {{
      const hasVideo = mediaStream && mediaStream.getVideoTracks().some(track => track.readyState === 'live');
      cameraMonitoring.available = Boolean(hasVideo);
      cameraMonitoring.started_at = cameraMonitoring.started_at || new Date().toISOString();
      if (!hasVideo || cameraMonitorTimer) return;
      const BrowserFaceDetector = window.FaceDetector;
      faceDetectionSupported = Boolean(BrowserFaceDetector);
      cameraMonitoring.face_detection_supported = faceDetectionSupported;
      cameraMonitoring.method = faceDetectionSupported ? 'browser_face_detector_and_video_sampling' : 'browser_video_sampling';
      if (faceDetectionSupported && !faceDetector) {{
        try {{
          faceDetector = new BrowserFaceDetector({{fastMode: true, maxDetectedFaces: 3}});
        }} catch (error) {{
          faceDetector = null;
          cameraMonitoring.face_detection_supported = false;
          cameraMonitoring.method = 'browser_video_sampling';
        }}
      }}
      sampleCameraFrame();
      cameraMonitorTimer = window.setInterval(sampleCameraFrame, 2500);
    }}

    function preferredRecordingMimeType() {{
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(RECORDING_MIME_TYPE)) return RECORDING_MIME_TYPE;
      if (window.MediaRecorder && MediaRecorder.isTypeSupported('video/webm')) return 'video/webm';
      return '';
    }}

    async function startInterviewRecording() {{
      if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {{
        setMessage('Screen recording is required for this interview. Please open this link in Chrome on a laptop or desktop.', true);
        return false;
      }}
      try {{
        screenStream = await navigator.mediaDevices.getDisplayMedia({{
          video: true,
          audio: true
        }});
      }} catch (error) {{
        setMessage('Screen recording is required. Please click Start again and share your screen when Chrome asks.', true);
        return false;
      }}
      const tracks = [
        ...screenStream.getVideoTracks(),
        ...screenStream.getAudioTracks(),
        ...mediaStream.getAudioTracks()
      ];
      recordingStream = new MediaStream(tracks);
      interviewRecordingChunks = [];
      try {{
        const mimeType = preferredRecordingMimeType();
        interviewRecorder = mimeType
          ? new MediaRecorder(recordingStream, {{mimeType, videoBitsPerSecond: 900000, audioBitsPerSecond: 64000}})
          : new MediaRecorder(recordingStream);
      }} catch (error) {{
        setMessage('Could not start screen recording in this browser. Please use Google Chrome.', true);
        return false;
      }}
      interviewRecorder.ondataavailable = event => {{
        if (event.data && event.data.size > 0) interviewRecordingChunks.push(event.data);
      }};
      interviewRecorder.onerror = () => setMessage('Screen recording had an issue. Please keep the interview tab open.', true);
      interviewRecorder.start(1000);
      setMessage('Screen recording started. Please keep sharing until the interview is complete.');
      return true;
    }}

    async function stopAndUploadInterviewRecording() {{
      if (recordingUploadStarted) return;
      recordingUploadStarted = true;
      if (!interviewRecorder) return;
      const stopped = new Promise(resolve => {{
        if (interviewRecorder.state === 'inactive') {{
          resolve();
          return;
        }}
        interviewRecorder.onstop = resolve;
        try {{
          interviewRecorder.stop();
        }} catch (error) {{
          resolve();
        }}
      }});
      await stopped;
      if (screenStream) screenStream.getTracks().forEach(track => track.stop());
      if (recordingStream) recordingStream.getTracks().forEach(track => track.stop());
      const mimeType = interviewRecorder.mimeType || 'video/webm';
      const blob = new Blob(interviewRecordingChunks, {{type: mimeType}});
      if (!blob.size) return;
      setMessage('Saving interview recording to OneDrive...');
      const response = await fetch(`/api/interview/${{token}}/recording`, {{
        method: 'POST',
        headers: {{'Content-Type': mimeType}},
        body: blob
      }});
      const data = await response.json();
      if (!response.ok || !data.ok) {{
        throw new Error(data.error || 'Could not save interview recording.');
      }}
      setMessage('Interview recording saved successfully.');
    }}

    function stopCameraMonitoring() {{
      if (cameraMonitorTimer) window.clearInterval(cameraMonitorTimer);
      cameraMonitorTimer = null;
      cameraMonitoring.ended_at = new Date().toISOString();
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
      }}, PROCESSING_NUDGE_MS);
    }}

    async function closeInterviewScreen() {{
      stopCameraMonitoring();
      let recordingError = '';
      try {{
        await stopAndUploadInterviewRecording();
      }} catch (error) {{
        recordingError = error.message || 'Interview completed, but recording upload failed.';
      }}
      if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
      if (audioMonitorId) window.cancelAnimationFrame(audioMonitorId);
      audioMonitorId = null;
      if (interviewBox) interviewBox.classList.add('hidden');
      setCallStatus('Completed', false);
      setMessage(recordingError || 'Interview completed. Thank you for your time today. I will get back to you with feedback soon.', Boolean(recordingError));
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
        startCameraMonitoring();
      }}
      if (hasAudio) startAudioActivityMonitor();
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
        const now = Date.now();
        let interim = '';
        let sawFinal = false;
        for (let i = event.resultIndex; i < event.results.length; i++) {{
          const text = event.results[i][0].transcript;
          if (event.results[i].isFinal) {{
            finalTranscript += text + ' ';
            sawFinal = true;
          }}
          else interim += text;
        }}
        const nextTranscript = (finalTranscript + interim).trim();
        if (nextTranscript && nextTranscript !== answerEl.value.trim()) {{
          lastTranscriptChangeAt = now;
        }}
        lastSpeechResultAt = now;
        answerEl.value = nextTranscript;
        if (answerEl.value.trim()) {{
          speechStarted = true;
          nextBtn.disabled = false;
          setMessage('Listening. Please continue naturally.');
          scheduleAutoAdvance();
          if (sawFinal) {{
            lastFinalTranscriptAt = Date.now();
            scheduleFinalResultAutoAdvance();
          }}
        }}
      }};
      rec.onerror = (event) => {{
        const reason = event && event.error ? event.error : 'unknown error';
        if (isRecording && !isSubmitting && !interviewClosed && reason === 'no-speech') {{
          setMessage('Listening. Please continue naturally.');
          return;
        }}
        isRecording = false;
        listenBtn.textContent = 'Answer';
        nextBtn.disabled = false;
        setMessage(`Speech recognition issue: ${{reason}}. You can type the answer and continue.`, true);
      }};
      rec.onend = () => {{
        if (isRecording && !isSubmitting && !interviewClosed) {{
          window.setTimeout(() => {{
            if (isRecording && !isSubmitting && !interviewClosed) {{
              try {{
                recognition.start();
                setMessage('Listening. Please continue naturally.');
              }} catch (error) {{}}
            }}
          }}, 150);
          return;
        }}
        isRecording = false;
        listenBtn.textContent = 'Answer';
        aiStatus.textContent = 'Ready';
        nextBtn.disabled = false;
        clearAutoAdvanceTimer();
      }};
      return rec;
    }}

    async function startInterview() {{
      if (!enforceSupportedBrowser()) return;
      startBtn.disabled = true;
      setMessage('Checking camera and microphone permissions...');
      const mediaOk = await ensureMediaAccess();
      if (!mediaOk) {{
        startBtn.disabled = false;
        return;
      }}
      setMessage('Please share your screen so this interview can be recorded.');
      const recordingOk = await startInterviewRecording();
      if (!recordingOk) {{
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
      const now = Date.now();
      lastSpeechResultAt = now;
      lastMicActivityAt = now;
      lastTranscriptChangeAt = now;
      lastFinalTranscriptAt = 0;
      aiStatus.textContent = 'Listening';
      setMessage('Listening. Please answer now.');
      scheduleAutoAdvance();
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
          body: JSON.stringify({{answer, turn_id: turnId, camera_monitoring: cameraMonitoring}})
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
    endBtn.addEventListener('click', async () => {{
      if (recognition && isRecording) recognition.stop();
      stopCameraMonitoring();
      try {{
        await stopAndUploadInterviewRecording();
      }} catch (error) {{
        setMessage(error.message || 'Interview ended, but recording upload failed.', true);
      }}
      if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
      setCallStatus('Ended', false);
      setMessage('The interview has been ended in this browser window.');
      startBtn.disabled = true;
      listenBtn.disabled = true;
      replayBtn.disabled = true;
      nextBtn.disabled = true;
      endBtn.disabled = true;
    }});
    enforceSupportedBrowser();
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
        details_text = self.json_text(row["details"])
        edit_form = f"""
        <section class="panel">
            <div class="panel-head"><h2>Edit Email Event</h2></div>
            <form method="post" action="/events/update" class="requirement-form">
                <input type="hidden" name="id" value="{html_escape(event_id)}">
                <label>Event Type<input name="event_type" value="{html_escape(row["event_type"] or "")}"></label>
                <label>Source Email<input name="source_email" value="{html_escape(row["source_email"] or "")}"></label>
                <label class="wide">Email Subject<input name="email_subject" value="{html_escape(row["email_subject"] or "")}"></label>
                <label class="wide">Details JSON<textarea name="details" rows="8">{details_text}</textarea></label>
                <button type="submit">Save Changes</button>
            </form>
        </section>
        """
        return f"""
        {self.back_link("/events", "Back to events")}
        <section class="panel">
            <div class="panel-head">
                <h2>Email Event #{html_escape(event_id)}</h2>
                <div class="action-stack">
                    {status_badge(row["event_type"])}
                    {self.delete_form("/events/delete", event_id, "Delete Event", "Delete this email event log?")}
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        {edit_form}
        """

    def status_options(self, selected: str | None) -> str:
        return "".join(
            f'<option value="{html_escape(option)}" {"selected" if option == selected else ""}>{html_escape(option.replace("_", " "))}</option>'
            for option in self.application_status_values()
        )

    def status_filter_options(self, selected: str | None) -> str:
        options = ['<option value="">All statuses</option>']
        options.extend(
            f'<option value="{html_escape(option)}" {"selected" if option == selected else ""}>{html_escape(option.replace("_", " "))}</option>'
            for option in self.application_status_values()
        )
        return "".join(options)

    def application_status_values(self) -> list[str]:
        return [
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
            "rejected_jd_score",
            "manual_hr_review",
            "final_hr_round_completed_pending_decision",
            "selected_documents_requested",
            "rejected_after_hr_round",
            "hold_after_hr_round",
            "no_open_requirement",
            "reviewed",
            "shortlisted",
            "rejected",
            "withdrawn",
        ]

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

    def delete_form(self, action: str, record_id, label: str, message: str) -> str:
        return f"""
        <form method="post" action="{html_escape(action)}" class="inline-form" onsubmit="return confirm('{html_escape(message)}');">
            <input type="hidden" name="id" value="{html_escape(record_id)}">
            <button type="submit" class="danger">{html_escape(label)}</button>
        </form>
        """

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
        pending_count = self.pending_operator_count()
        overview_label = f"Overview ({pending_count})" if pending_count else "Overview"
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
                    {self.nav_link("/", overview_label, active == "overview")}
                    {self.nav_link("/requirements", "Requirements", active == "requirements")}
                    {self.nav_link("/applications", "Applications", active == "applications")}
                    {self.nav_link("/candidates", "Candidates", active == "candidates")}
                    {self.nav_link("/events", "Email Events", active == "events")}
                </nav>
                <a class="logout-link" href="/logout">Sign out</a>
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
            <script>
            function configureHrReschedule(form) {{
                const scheduleNow = window.confirm(
                    'Do you want to schedule the final HR round now?\\n\\nOK = choose date/time now\\nCancel = ask candidate for availability'
                );
                form.reschedule_mode.value = scheduleNow ? 'schedule_now' : 'ask_candidate';
                if (scheduleNow) {{
                    const timeValue = window.prompt(
                        'Enter interview date/time in IST. Example: 2026-07-31T18:30',
                        ''
                    );
                    if (!timeValue) return false;
                    form.scheduled_at.value = timeValue;
                }} else {{
                    form.scheduled_at.value = '';
                }}
                const interviewer = form.interviewer_email;
                const selected = interviewer && interviewer.options[interviewer.selectedIndex]
                    ? interviewer.options[interviewer.selectedIndex].text
                    : 'Auto assign';
                return window.confirm(
                    (scheduleNow ? 'Schedule final HR round now' : 'Ask candidate for availability') +
                    '\\nInterviewer: ' + selected +
                    '\\nAllowed slots: Monday-Friday, 6 PM to 1 AM IST\\n\\nContinue?'
                );
            }}
            </script>
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
        LOGGER.info("[dashboard] %s - %s", self.address_string(), format % args)


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
.logout-link {
    display: block;
    margin-top: 18px;
    color: #c8d7ea;
    text-decoration: none;
    font-size: 13px;
}
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
.metric-link {
    color: inherit;
    display: block;
    text-decoration: none;
}
.metric-link:hover {
    border-color: var(--accent);
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
.notice-panel {
    border-color: #f2c94c;
}
.notice-panel-ok {
    border-color: var(--line);
}
.notification-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    padding: 16px;
}
.notification-card {
    display: grid;
    gap: 6px;
    padding: 14px;
    border: 1px solid #f2c94c;
    border-radius: 8px;
    background: #fffbeb;
    text-decoration: none;
}
.notification-card strong {
    color: #7a4b00;
}
.notification-card span {
    font-weight: 800;
    color: #b42318;
}
.notification-card small {
    color: #627084;
}
.notification-card em {
    font-style: normal;
    font-weight: 700;
    color: var(--accent);
}
.filter-note {
    margin: 0 0 12px;
}
.filter-note a {
    margin-left: 8px;
    color: var(--accent);
}
.filter-bar {
    display: flex;
    align-items: end;
    flex-wrap: wrap;
    gap: 10px;
    margin: 0 0 12px;
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: white;
}
.filter-bar label {
    display: grid;
    gap: 5px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .04em;
}
.filter-bar select {
    min-width: 220px;
}
.filter-bar input[type="date"] {
    min-width: 160px;
}
.filter-bar .checkbox-label {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 38px;
    color: var(--ink);
    text-transform: none;
    letter-spacing: 0;
    font-size: 14px;
}
.filter-bar .checkbox-label input {
    width: auto;
}
.filter-bar a {
    color: var(--accent);
    min-height: 38px;
    display: inline-flex;
    align-items: center;
}
.status-filter-link {
    display: inline-flex;
    text-decoration: none;
}
.status-filter-link:hover .badge {
    box-shadow: 0 0 0 2px rgba(27, 99, 177, .18);
}
.table-scroll {
    max-height: calc(100vh - 260px);
    overflow: auto;
}
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
.sticky-table thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    box-shadow: 0 1px 0 var(--line);
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
.badge-closed, .badge-rejected, .badge-rejected_jd_score, .badge-withdrawn {
    background: #fde8e6;
    color: var(--danger);
}
.badge-no_open_requirement, .badge-review, .badge-reviewed, .badge-manual_hr_review {
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
.schedule-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
}
.schedule-card {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 14px;
    background: #fbfdff;
}
.schedule-card > div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
}
.schedule-card strong, .schedule-card small {
    display: block;
}
.schedule-card small {
    color: var(--muted);
    margin-top: 3px;
}
.schedule-card b {
    display: inline-block;
    margin: 12px 0;
    font-size: 28px;
}
.schedule-card ul {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
}
.schedule-card li {
    display: grid;
    gap: 3px;
    padding-top: 8px;
    border-top: 1px solid var(--line);
}
.schedule-card li span {
    color: var(--muted);
    font-size: 12px;
}
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
.login-page {
    display: block;
    min-height: 100vh;
    background: var(--page);
}
.login-wrap {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
}
.login-card {
    width: min(420px, 100%);
    background: white;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 24px;
    box-shadow: 0 18px 42px rgba(15, 23, 42, .12);
}
.login-brand {
    color: var(--ink);
    margin-bottom: 20px;
}
.login-form {
    display: grid;
    gap: 14px;
    margin-top: 18px;
}
.login-form label {
    display: grid;
    gap: 6px;
    color: var(--muted);
    font-size: 13px;
}
.login-form input {
    width: 100%;
    box-sizing: border-box;
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
    .metrics, .notification-grid, .schedule-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid-two, .requirement-form, .detail-grid { grid-template-columns: 1fr; }
    .field, .field:nth-child(3n) { border-right: 0; }
    main { padding: 16px; }
    table { min-width: 900px; }
    .panel { overflow-x: auto; }
}
@media (max-width: 640px) {
    nav { grid-template-columns: 1fr 1fr; }
    .topbar, .search { align-items: stretch; flex-direction: column; }
    .metrics, .notification-grid, .schedule-grid { grid-template-columns: 1fr; }
}
"""


def run(host: str, port: int):
    start_final_hr_round_monitor()
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
