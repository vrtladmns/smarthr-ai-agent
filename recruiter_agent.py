import argparse
import base64
import email
import hashlib
import imaplib
import json
import logging
import mimetypes
import os
import random
import re
import subprocess
import smtplib
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from html import escape as html_escape
from html import unescape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langsmith import trace, traceable

from config import (
    DATABASE_URL,
    CV_STORAGE_DIR,
    DB_PROVIDER,
    FINAL_HR_DEFAULT_DURATION_MINUTES,
    FINAL_HR_INTERVIEWERS,
    FINAL_HR_WINDOW_END_HOUR,
    FINAL_HR_WINDOW_START_HOUR,
    FINAL_HR_WORKDAYS,
    GMAIL_CREDENTIALS_FILE,
    GMAIL_PUBSUB_TOPIC,
    GMAIL_TOKEN_FILE,
    GRAPH_CLIENT_STATE,
    GRAPH_NOTIFICATION_URL,
    GRAPH_PROCESSING_RETRY_SECONDS,
    GRAPH_SUBSCRIPTION_HOURS,
    GRAPH_WEBHOOK_HOST,
    GRAPH_WEBHOOK_PATH,
    GRAPH_WEBHOOK_PORT,
    GMAIL_WEBHOOK_HOST,
    GMAIL_WEBHOOK_PATH,
    GMAIL_WEBHOOK_PORT,
    MAIL_PROVIDER,
    MICROSOFT_CLIENT_ID,
    MICROSOFT_CLIENT_SECRET,
    MICROSOFT_GRAPH_BASE_URL,
    MICROSOFT_MAILBOX,
    MICROSOFT_TENANT_ID,
    NGROK_API_URL,
    OLLAMA_NUM_PREDICT,
    ONEDRIVE_RECORDINGS_FOLDER,
    ONEDRIVE_RECORDINGS_USER,
    RECRUITER_EMAIL,
    RECRUITER_EMAIL_PASSWORD,
    RECRUITER_FROM_EMAIL,
    RECRUITER_CAB_FACILITY,
    RECRUITER_DASHBOARD_BASE_URL,
    RECRUITER_HR_ESCALATION_EMAIL,
    RECRUITER_IMAP_HOST,
    RECRUITER_INTERVIEW_BASE_URL,
    RECRUITER_INTERVIEW_HOLD_MIN_SCORE,
    RECRUITER_INTERVIEW_PASS_SCORE,
    RECRUITER_IMAP_PORT,
    RECRUITER_LOG_FILE,
    RECRUITER_LOG_LEVEL,
    RECRUITER_MAILBOX,
    RECRUITER_OFFICE_LOCATION,
    RECRUITER_APPEND_SIGNATURE,
    RECRUITER_POLL_LIMIT,
    RECRUITER_POLL_SECONDS,
    RECRUITER_REPLY_ENABLED,
    RECRUITER_SCREENING_ATS_MIN,
    RECRUITER_SCREENING_JD_MIN,
    RECRUITER_SIGNATURE_COMPANY,
    RECRUITER_SIGNATURE_COMPANY_URL,
    RECRUITER_SIGNATURE_EMAIL,
    RECRUITER_SIGNATURE_LOGO_URL,
    RECRUITER_SIGNATURE_NAME,
    RECRUITER_SIGNATURE_SIGNOFF,
    RECRUITER_SMTP_HOST,
    RECRUITER_SMTP_PORT,
    RECRUITER_TIMEZONE,
    RECRUITER_WORKING_DAYS,
    RECRUITER_WORK_MODE,
    RECRUITER_WORK_SHIFT,
)
from llm_factory import make_chat_model


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


LOGGER = logging.getLogger("recruiter_agent")


def setup_recruiter_logging():
    if LOGGER.handlers:
        return
    log_level = getattr(logging, str(RECRUITER_LOG_LEVEL).upper(), logging.INFO)
    LOGGER.setLevel(log_level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    LOGGER.addHandler(console_handler)

    if RECRUITER_LOG_FILE:
        log_path = Path(RECRUITER_LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        LOGGER.addHandler(file_handler)

    LOGGER.propagate = False


def log_json(level: int, event: str, **details: Any):
    safe_details = {
        key: value
        for key, value in details.items()
        if key.lower() not in {"password", "secret", "token", "api_key", "client_secret"}
    }
    LOGGER.log(
        level,
        "%s %s",
        event,
        json.dumps(safe_details, default=str, ensure_ascii=False),
    )


def safe_trace_payload(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)[:300]
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in {
                "password",
                "secret",
                "token",
                "api_key",
                "client_secret",
                "payload",
                "attachment_payload",
                "cv_text",
                "body",
                "thread_context",
            }:
                output[key_text] = "[redacted]"
            else:
                output[key_text] = safe_trace_payload(item, depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [safe_trace_payload(item, depth + 1) for item in items[:30]]
    if isinstance(value, str):
        return value if len(value) <= 1200 else f"{value[:1200]}...[truncated]"
    return value


def trace_recruiter_event(
    name: str,
    *,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
):
    try:
        with trace(
            name,
            run_type="chain",
            inputs=safe_trace_payload(inputs or {}),
            metadata=safe_trace_payload(metadata or {}),
            tags=["recruiter-agent", *(tags or [])],
        ) as run:
            if outputs is not None:
                run.add_outputs(safe_trace_payload(outputs))
    except Exception as exc:
        LOGGER.debug("LangSmith trace event failed for %s: %s", name, exc)


def inbox_email_summary(inbox_email) -> dict[str, Any]:
    if not inbox_email:
        return {}
    return {
        "message_id": inbox_email.message_id,
        "uid": inbox_email.uid,
        "sender": inbox_email.sender,
        "subject": inbox_email.subject,
        "attachment_count": len(inbox_email.attachments or []),
        "cv_attachment_names": [
            attachment_display_name(filename)
            for filename, _ in inbox_email.attachments or []
            if is_cv_attachment(filename, _)
        ],
    }


setup_recruiter_logging()


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


CV_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}


def recruiter_tz():
    try:
        return ZoneInfo(RECRUITER_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def recruiter_now() -> datetime:
    return datetime.now(recruiter_tz())


def as_recruiter_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=recruiter_tz())
    return value.astimezone(recruiter_tz())


def recruiter_time_text(value: datetime) -> str:
    return f"{as_recruiter_time(value).strftime('%d %b %Y, %I:%M %p')} IST"


def final_hr_workdays() -> set[int]:
    days = set()
    for item in FINAL_HR_WORKDAYS.split(","):
        item = item.strip()
        if item.isdigit():
            days.add(int(item))
    return days or {0, 1, 2, 3, 4}


def final_hr_interviewers() -> list[dict[str, str]]:
    interviewers = []
    for item in FINAL_HR_INTERVIEWERS.split(","):
        value = item.strip()
        if not value:
            continue
        name, email_address = parseaddr(value)
        if not email_address and "@" in value:
            email_address = value
            name = value.split("@", 1)[0].replace(".", " ").title()
        if email_address:
            interviewers.append({"name": name or email_address, "email": email_address.lower()})
    return interviewers or [
        {"name": "Pragati Pradhan", "email": "pragati.pradhan@virtualadmins.org"},
        {"name": "Akash", "email": "akash@virtualadmins.org"},
    ]


def final_hr_interviewer_by_email(email_address: str | None) -> dict[str, str] | None:
    target = clean_email(email_address or "")
    if not target:
        return None
    for interviewer in final_hr_interviewers():
        if clean_email(interviewer["email"]) == target:
            return interviewer
    return None


def hr_notification_recipients(include_escalation_email: bool = True) -> list[str]:
    recipients = []
    seen = set()
    for interviewer in final_hr_interviewers():
        email_address = clean_email(interviewer.get("email"))
        if email_address and email_address not in seen:
            recipients.append(interviewer["email"])
            seen.add(email_address)
    escalation_email = clean_email(RECRUITER_HR_ESCALATION_EMAIL)
    if include_escalation_email and escalation_email and escalation_email not in seen:
        recipients.append(RECRUITER_HR_ESCALATION_EMAIL)
    return recipients


def send_hr_notification(mailer, subject: str, body: str, include_escalation_email: bool = True):
    for recipient in hr_notification_recipients(include_escalation_email=include_escalation_email):
        mailer.send_direct_email(recipient, subject, body)


# Thread rendering budget. build_thread_context() spends this newest-message-first.
THREAD_CONTEXT_MAX_CHARS = int(os.getenv("RECRUITER_THREAD_CONTEXT_MAX_CHARS", "8000"))
THREAD_CONTEXT_PER_MESSAGE_CHARS = int(os.getenv("RECRUITER_THREAD_CONTEXT_PER_MESSAGE_CHARS", "1200"))
# How many messages of a conversation to pull from the mail provider. Real threads
# run past 20 messages, and the newest ones are the ones that matter.
THREAD_FETCH_LIMIT = int(os.getenv("RECRUITER_THREAD_FETCH_LIMIT", "25"))

# How sure the LLM must be to attach a requirement. This is deliberately not a
# suitability bar: it only decides which opening to evaluate the CV against.
# Suitability is then judged by the JD score, which yields either screening
# questions or an explained rejection HR can revoke - both better outcomes than
# asking a human to assign the requirement by hand.
LLM_MATCH_MIN_CONFIDENCE = float(os.getenv("RECRUITER_LLM_MATCH_MIN_CONFIDENCE", "0.5"))
# Above this, the model's match is kept even when the titles share no words.
# "Sr. Accountant" and "US Bookkeeper" have no token in common, so the overlap
# check vetoed a correct match and the candidate went to a human. The JD score
# is the real gate and runs immediately after.
LLM_MATCH_TRUST_CONFIDENCE = float(os.getenv("RECRUITER_LLM_MATCH_TRUST_CONFIDENCE", "0.7"))
# How many open roles a CV may be scored against when nothing matched by title.
REQUIREMENT_SCORING_MAX = int(os.getenv("RECRUITER_REQUIREMENT_SCORING_MAX", "4"))
# A JD score this far below the screening minimum is still close enough that a
# person should look, rather than the candidate being told there is no opening.
NEAR_MISS_AMBIGUOUS_BAND = float(os.getenv("RECRUITER_NEAR_MISS_AMBIGUOUS_BAND", "10"))
# Expecting more than this multiple of the approved maximum is not a gap that a
# conversation closes. Disclosing the range to someone asking for twice it just
# buys one more round of email and then a human. Set to 0 to always disclose.
BUDGET_GAP_MAX_RATIO = float(os.getenv("RECRUITER_BUDGET_GAP_MAX_RATIO", "1.4"))
# Salary figures reach this code in two different units. "Business Development
# Executive" was entered as 40000-60000, which is per month, while every other
# role is annual, and candidates answer in whichever unit they think in - one
# countered at 42000 for that role and another at 660000. Anything below this
# floor is read as a monthly figure and annualised for the comparison only; the
# range quoted to a candidate is always what HR typed.
BUDGET_ANNUAL_FLOOR = float(os.getenv("RECRUITER_BUDGET_ANNUAL_FLOOR", "120000"))
# A gap this large is a unit or data-entry problem, not a candidate asking too
# much, and must never close an application on its own.
BUDGET_IMPLAUSIBLE_GAP_RATIO = float(os.getenv("RECRUITER_BUDGET_IMPLAUSIBLE_GAP_RATIO", "3"))

# Evidence of doing the work counts slightly less than carrying the job title,
# so an exact title match still wins when both requirements look plausible.
EVIDENCE_MATCH_WEIGHT = float(os.getenv("RECRUITER_EVIDENCE_MATCH_WEIGHT", "0.9"))

# Title words too common in ordinary CV prose to prove anything on their own.
# "Business Development Executive" reduces to {business, development}, both of
# which appear in most technology CVs, so matching a QA engineer to a sales role
# on those two words alone is a false positive waiting to happen. Titles made up
# entirely of these are matched on the title only, never on CV evidence.
EVIDENCE_WEAK_TOKENS = {
    "business",
    "development",
    "operations",
    "management",
    "service",
    "services",
    "process",
    "support",
    "technology",
    "technical",
    "digital",
    "global",
    "client",
    "customer",
    "project",
    "product",
    "quality",
    "general",
    "senior",
    "junior",
}

# A claim older than this is treated as abandoned and may be re-acquired. If the
# process is killed between claiming a message and releasing it, the claim would
# otherwise be permanent: every later notification skips the message, it stays
# unread, and the candidate is never answered. Re-processing is safe because the
# reply ledger still blocks duplicate emails.
MESSAGE_CLAIM_STALE_MINUTES = int(os.getenv("RECRUITER_MESSAGE_CLAIM_STALE_MINUTES", "30"))
# How often to pick up work an external dashboard has queued.
ACTION_QUEUE_POLL_SECONDS = int(os.getenv("RECRUITER_ACTION_QUEUE_POLL_SECONDS", "15"))

# Never send the same scenario to the same application twice inside this window.
# This is the backstop that caps the blast radius of any single logic bug.
REPLY_SCENARIO_COOLDOWN_HOURS = int(os.getenv("RECRUITER_REPLY_SCENARIO_COOLDOWN_HOURS", "24"))
# How many times one scenario may legitimately be sent inside that window.
# One was too strict: a candidate who replies with something new gets an answer
# that happens to share a scenario with an earlier message, and suppressing it
# left them with total silence. Two still caps the runaway loops this guard
# exists for - the original fault was a dozen near-identical emails.
REPLY_SCENARIO_MAX_PER_WINDOW = int(os.getenv("RECRUITER_REPLY_SCENARIO_MAX_PER_WINDOW", "2"))
# Scenarios that must never be repeated for an application, at any interval.
ONCE_PER_APPLICATION_SCENARIOS = {"budget_disclosure", "holding_reply", "budget_out_of_range", "not_selected"}

# The AI interview has already happened by these points. "Approve For Interview"
# is a pre-interview control and must never appear here - clicking it re-sends
# the interview link to someone who has already sat the interview.
POST_INTERVIEW_STATUSES = {
    "interview_completed",
    "interview_on_hold_hr_review",
    "hr_round_time_requested",
    "interview_availability_received",
    "interview_scheduled",
    "final_hr_round_pending",
    "final_hr_round_completed_pending_decision",
    "selected_documents_requested",
    "rejected_after_hr_round",
    "hold_after_hr_round",
    "interview_rejected",
}

# Where HR still owes a decision on a completed interview. The availability
# request can be re-sent from any of these, which matters when a status was
# changed by hand and no email ever went out.
POST_INTERVIEW_DECISION_STATUSES = {
    "interview_completed",
    "interview_on_hold_hr_review",
    "hr_round_time_requested",
}

# Statuses where a human owns the conversation. The agent stores replies and stops.
HUMAN_HOLD_STATUSES = {
    "hr_escalated",
    "manual_hr_review",
    "interview_on_hold_hr_review",
    "human_handled",
}

FINAL_AGENT_STATUSES = {
    "selected_documents_requested",
    "rejected_after_hr_round",
    "interview_rejected",
    "rejected",
    "withdrawn",
    "no_open_requirement",
    "manual_hr_review",
}


def is_final_agent_status(status: str | None) -> bool:
    return (status or "").strip().lower() in FINAL_AGENT_STATUSES


def is_final_hr_slot_allowed(value: datetime) -> bool:
    local = as_recruiter_time(value)
    hour_value = local.hour + local.minute / 60
    start = FINAL_HR_WINDOW_START_HOUR
    end = FINAL_HR_WINDOW_END_HOUR
    if start <= end:
        in_window = start <= hour_value < end
        operational_day = local.date()
    else:
        in_window = hour_value >= start or hour_value < end
        operational_day = local.date() if hour_value >= start else (local - timedelta(days=1)).date()
    return in_window and operational_day.weekday() in final_hr_workdays()


def final_hr_operational_day(value: datetime) -> datetime.date:
    local = as_recruiter_time(value)
    hour_value = local.hour + local.minute / 60
    if FINAL_HR_WINDOW_START_HOUR > FINAL_HR_WINDOW_END_HOUR and hour_value < FINAL_HR_WINDOW_END_HOUR:
        return (local - timedelta(days=1)).date()
    return local.date()


def next_final_hr_working_day(after: datetime | None = None) -> datetime.date:
    local = as_recruiter_time(after or recruiter_now())
    day = local.date() + timedelta(days=1)
    workdays = final_hr_workdays()
    for _ in range(14):
        if day.weekday() in workdays:
            return day
        day += timedelta(days=1)
    return day


def final_hr_datetime_for_operational_day(day, hour: int, minute: int = 0) -> datetime:
    calendar_day = day
    if FINAL_HR_WINDOW_START_HOUR > FINAL_HR_WINDOW_END_HOUR and hour < FINAL_HR_WINDOW_END_HOUR:
        calendar_day = day + timedelta(days=1)
    return datetime.combine(calendar_day, datetime.min.time(), tzinfo=recruiter_tz()).replace(
        hour=hour,
        minute=minute,
    )


def next_final_hr_slot(after: datetime | None = None) -> datetime:
    current = as_recruiter_time(after or recruiter_now()) + timedelta(minutes=15)
    current = current.replace(second=0, microsecond=0)
    if current.minute % 15:
        current += timedelta(minutes=15 - (current.minute % 15))
    for day_offset in range(14):
        day = (current + timedelta(days=day_offset)).date()
        for hour in range(0, 24):
            for minute in (0, 15, 30, 45):
                candidate = datetime.combine(day, datetime.min.time(), tzinfo=recruiter_tz()).replace(
                    hour=hour,
                    minute=minute,
                )
                if candidate <= current:
                    continue
                if is_final_hr_slot_allowed(candidate):
                    return candidate
    return current + timedelta(days=1)


def random_final_hr_slot(after: datetime | None = None) -> datetime:
    now = as_recruiter_time(after or recruiter_now())
    candidates = []
    for day_offset in range(1, 8):
        day = (now + timedelta(days=day_offset)).date()
        for hour in list(range(FINAL_HR_WINDOW_START_HOUR, 24)) + list(range(0, FINAL_HR_WINDOW_END_HOUR)):
            for minute in (0, 15, 30, 45):
                candidate = datetime.combine(day, datetime.min.time(), tzinfo=recruiter_tz()).replace(
                    hour=hour,
                    minute=minute,
                )
                if candidate > now and is_final_hr_slot_allowed(candidate):
                    candidates.append(candidate)
    return random.choice(candidates) if candidates else next_final_hr_slot(now)


def coerce_final_hr_slot(
    value: datetime | None,
    flexible: bool = False,
    require_next_working_day: bool = False,
) -> datetime | None:
    if flexible:
        return random_final_hr_slot()
    if not value:
        return None
    local = as_recruiter_time(value)
    if require_next_working_day:
        minimum_working_day = next_final_hr_working_day()
        if final_hr_operational_day(local) < minimum_working_day:
            local = final_hr_datetime_for_operational_day(minimum_working_day, local.hour, local.minute)
    if is_final_hr_slot_allowed(local) and (
        not require_next_working_day or final_hr_operational_day(local) >= next_final_hr_working_day()
    ):
        return local
    search_from = local
    if require_next_working_day and final_hr_operational_day(local) < next_final_hr_working_day():
        search_from = final_hr_datetime_for_operational_day(
            next_final_hr_working_day(),
            FINAL_HR_WINDOW_START_HOUR,
            0,
        ) - timedelta(minutes=15)
    return next_final_hr_slot(search_from)


def choose_final_hr_interviewer(
    db: "RecruiterDatabase",
    scheduled_at: datetime,
    preferred_email: str | None = None,
) -> dict[str, str]:
    preferred = final_hr_interviewer_by_email(preferred_email)
    if preferred:
        return preferred
    local = as_recruiter_time(scheduled_at)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    counts = {
        clean_email(row.get("hr_interviewer_email")): int(row.get("meeting_count") or 0)
        for row in db.final_hr_schedule_counts(day_start, day_end)
    }
    interviewers = final_hr_interviewers()
    random.shuffle(interviewers)
    return min(interviewers, key=lambda item: counts.get(clean_email(item["email"]), 0))


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS recruitment_requirements (
    id BIGSERIAL PRIMARY KEY,
    position_title TEXT NOT NULL,
    experience_min_years NUMERIC(5, 2),
    experience_max_years NUMERIC(5, 2),
    budget_min NUMERIC(12, 2),
    budget_max NUMERIC(12, 2),
    currency TEXT DEFAULT 'INR',
    job_description TEXT NOT NULL,
    recommended_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    urgently_required BOOLEAN DEFAULT FALSE,
    needed_within_days INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recruiter_candidates (
    id BIGSERIAL PRIMARY KEY,
    candidate_uid UUID NOT NULL UNIQUE,
    source_email TEXT,
    candidate_email TEXT,
    referrer_email TEXT,
    submission_type TEXT NOT NULL DEFAULT 'self_application',
    full_name TEXT,
    phone TEXT,
    location TEXT,
    linkedin_url TEXT,
    portfolio_url TEXT,
    current_title TEXT,
    current_company TEXT,
    total_experience_years NUMERIC(5, 2),
    skills JSONB NOT NULL DEFAULT '[]'::jsonb,
    education JSONB NOT NULL DEFAULT '[]'::jsonb,
    work_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    certifications JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_cv_text TEXT,
    cv_summary TEXT,
    ats_score NUMERIC(5, 2),
    ai_evaluation JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recruiter_applications (
    id BIGSERIAL PRIMARY KEY,
    application_uid UUID NOT NULL UNIQUE,
    candidate_id BIGINT NOT NULL REFERENCES recruiter_candidates(id),
    requirement_id BIGINT REFERENCES recruitment_requirements(id),
    email_message_id TEXT,
    email_thread_id TEXT,
    source_email TEXT,
    candidate_email TEXT,
    referrer_email TEXT,
    submission_type TEXT NOT NULL DEFAULT 'self_application',
    email_subject TEXT,
    detected_position TEXT,
    matched_position TEXT,
    application_status TEXT NOT NULL,
    ats_score NUMERIC(5, 2),
    jd_match_score NUMERIC(5, 2),
    strengths JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_requirements JSONB NOT NULL DEFAULT '[]'::jsonb,
    ai_short_description TEXT,
    ai_evaluation JSONB NOT NULL DEFAULT '{}'::jsonb,
    attachment_filename TEXT,
    attachment_sha256 TEXT,
    attachment_payload BYTEA,
    screening_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    screening_current_salary NUMERIC(12, 2),
    screening_expected_salary NUMERIC(12, 2),
    screening_current_location TEXT,
    screening_joining_days INTEGER,
    hr_escalation_reason TEXT,
    hr_escalated_at TIMESTAMPTZ,
    hr_approved_at TIMESTAMPTZ,
    interview_availability TEXT,
    interview_scheduled_at TIMESTAMPTZ,
    hr_interviewer_email TEXT,
    hr_interviewer_name TEXT,
    teams_event_id TEXT,
    teams_join_url TEXT,
    interview_link_token TEXT,
    interview_link_created_at TIMESTAMPTZ,
    interview_started_at TIMESTAMPTZ,
    interview_completed_at TIMESTAMPTZ,
    interview_reminder_sent_at TIMESTAMPTZ,
    interview_reminder_count INTEGER NOT NULL DEFAULT 0,
    interview_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS recruiter_applications_message_attachment_idx
ON recruiter_applications (email_message_id, attachment_sha256)
WHERE email_message_id IS NOT NULL AND attachment_sha256 IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS recruitment_requirements_position_title_idx
ON recruitment_requirements (LOWER(position_title));

ALTER TABLE recruiter_candidates ADD COLUMN IF NOT EXISTS candidate_email TEXT;
ALTER TABLE recruiter_candidates ADD COLUMN IF NOT EXISTS referrer_email TEXT;
ALTER TABLE recruiter_candidates ADD COLUMN IF NOT EXISTS submission_type TEXT NOT NULL DEFAULT 'self_application';
ALTER TABLE recruitment_requirements ADD COLUMN IF NOT EXISTS recommended_questions JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS email_thread_id TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS candidate_email TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS referrer_email TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS submission_type TEXT NOT NULL DEFAULT 'self_application';
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS attachment_payload BYTEA;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS screening_details JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS screening_current_salary NUMERIC(12, 2);
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS screening_expected_salary NUMERIC(12, 2);
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS screening_current_location TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS screening_joining_days INTEGER;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS hr_escalation_reason TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS hr_escalated_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS hr_approved_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_availability TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_scheduled_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS hr_interviewer_email TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS hr_interviewer_name TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS teams_event_id TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS teams_join_url TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_link_token TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_link_created_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_started_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_completed_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_reminder_sent_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_reminder_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_report JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS recruiter_email_events (
    id BIGSERIAL PRIMARY KEY,
    email_message_id TEXT,
    source_email TEXT,
    email_subject TEXT,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotency ledger for inbound provider messages. Graph webhooks are
-- at-least-once and the unread flag is only cleared ~50s after processing
-- starts, so the flag alone let a single email be answered twice.
CREATE TABLE IF NOT EXISTS recruiter_processed_messages (
    provider_message_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Outbound reply ledger. Every candidate-facing send is recorded here first,
-- which is what makes "never send the same scenario twice" enforceable
-- independently of whatever the surrounding state machine believes.
CREATE TABLE IF NOT EXISTS recruiter_sent_replies (
    id BIGSERIAL PRIMARY KEY,
    application_id BIGINT,
    recipient TEXT NOT NULL,
    scenario TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    provider_message_id TEXT,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS recruiter_sent_replies_app_scenario_idx
ON recruiter_sent_replies (application_id, scenario, sent_at DESC);
CREATE INDEX IF NOT EXISTS recruiter_sent_replies_recipient_scenario_idx
ON recruiter_sent_replies (recipient, scenario, sent_at DESC);

ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS human_handled_at TIMESTAMPTZ;
-- Interview session state, so a dashboard restart mid-interview does not throw
-- the candidate back to question one with a fresh set of questions.
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_session JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Attempt counter lives here rather than in process memory, which reset on every
-- restart and made the cap unenforceable.
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS interview_attempts INTEGER NOT NULL DEFAULT 0;
-- Set by an external dashboard to ask the agent to actually DO something.
-- Writing a status in SQL changes a row; it does not send the email, create the
-- Teams meeting or re-score the CV. Setting this column does.
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS requested_action TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS requested_action_by TEXT;

-- Work queued for the agent by anyone with database access.
CREATE TABLE IF NOT EXISTS agent_action_queue (
    id BIGSERIAL PRIMARY KEY,
    application_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_action_queue_pending_idx
ON agent_action_queue (status, created_at)
WHERE status = 'pending';

-- Setting requested_action enqueues the work and clears the column, so the
-- dashboard can simply write a value and watch the queue row for the outcome.
CREATE OR REPLACE FUNCTION enqueue_requested_action() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.requested_action IS NOT NULL AND NEW.requested_action <> '' THEN
        INSERT INTO agent_action_queue (application_id, action, requested_by)
        VALUES (NEW.id, NEW.requested_action, NEW.requested_action_by);
        NEW.requested_action := NULL;
        NEW.requested_action_by := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS recruiter_applications_requested_action ON recruiter_applications;
CREATE TRIGGER recruiter_applications_requested_action
BEFORE UPDATE OF requested_action ON recruiter_applications
FOR EACH ROW EXECUTE FUNCTION enqueue_requested_action();
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS budget_disclosed_at TIMESTAMPTZ;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS budget_response TEXT;
"""





@dataclass
class ThreadMessage:
    uid: bytes
    message_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime | None


@dataclass
class InboxEmail:
    uid: bytes
    gmail_thread_id: str
    message_id: str
    references: str
    in_reply_to: str
    sender: str
    subject: str
    body: str
    received_at: datetime | None
    attachments: list[tuple[str, bytes]]
    thread_messages: list[ThreadMessage]


def require_package(module_name: str, install_hint: str):
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency `{module_name}`. Install it with: {install_hint}. Original error: {exc}") from exc


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def parse_email_datetime(value: str | None):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def extract_body(message: email.message.EmailMessage) -> str:
    text_parts = []
    html_parts = []

    if message.is_multipart():
        for part in message.walk():
            content_disposition = part.get_content_disposition()
            content_type = part.get_content_type()
            if content_disposition == "attachment":
                continue
            if content_type == "text/plain":
                text_parts.append(part.get_content())
            elif content_type == "text/html":
                html_parts.append(strip_html(part.get_content()))
    else:
        if message.get_content_type() == "text/plain":
            text_parts.append(message.get_content())
        elif message.get_content_type() == "text/html":
            html_parts.append(strip_html(message.get_content()))

    body = "\n".join(part.strip() for part in text_parts if part and part.strip())
    if body:
        return body
    return "\n".join(part.strip() for part in html_parts if part and part.strip())


def parse_gmail_thread_id(fetch_metadata: Any) -> str:
    if isinstance(fetch_metadata, bytes):
        text = fetch_metadata.decode("utf-8", errors="ignore")
    else:
        text = str(fetch_metadata)
    match = re.search(r"X-GM-THRID\s+(\d+)", text)
    return match.group(1) if match else ""


def parse_thread_message(uid: bytes, parsed: email.message.EmailMessage) -> ThreadMessage:
    return ThreadMessage(
        uid=uid,
        message_id=parsed.get("Message-ID", ""),
        sender=parseaddr(parsed.get("From", ""))[1],
        subject=decode_mime(parsed.get("Subject")),
        body=extract_body(parsed),
        received_at=parse_email_datetime(parsed.get("Date")),
    )


def build_thread_context(
    inbox_email: InboxEmail,
    max_chars: int = THREAD_CONTEXT_MAX_CHARS,
    per_message_chars: int = THREAD_CONTEXT_PER_MESSAGE_CHARS,
) -> str:
    """Render the thread for an LLM prompt, newest-first priority.

    Two properties matter and both were broken before:
      * quoted history is stripped per message, so the budget is spent on new text
      * when the budget runs out the OLDEST messages are dropped, never the newest
    """
    messages = inbox_email.thread_messages or [
        ThreadMessage(
            uid=inbox_email.uid,
            message_id=inbox_email.message_id,
            sender=inbox_email.sender,
            subject=inbox_email.subject,
            body=inbox_email.body,
            received_at=inbox_email.received_at,
        )
    ]
    blocks = []
    for index, message in enumerate(messages, start=1):
        date_text = message.received_at.isoformat() if message.received_at else "unknown date"
        body = latest_reply_text(message.body or "")[:per_message_chars]
        blocks.append(
            f"Message {index}\n"
            f"From: {message.sender}\n"
            f"Date: {date_text}\n"
            f"Subject: {message.subject}\n"
            f"Body:\n{body}"
        )

    kept: list[str] = []
    total = 0
    for block in reversed(blocks):
        if kept and total + len(block) > max_chars:
            break
        kept.append(block)
        total += len(block)
    return "\n\n---\n\n".join(reversed(kept))


def thread_message_ids(inbox_email: InboxEmail) -> list[str]:
    seen = set()
    values = [
        inbox_email.message_id,
        inbox_email.in_reply_to,
        inbox_email.references,
    ]
    values.extend(message.message_id for message in inbox_email.thread_messages or [] if message.message_id)

    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        matches = re.findall(r"<[^>\s]+>", text)
        if matches:
            seen.update(matches)
        elif " " not in text:
            seen.add(text)
    return list(seen)


def latest_reply_text(body: str) -> str:
    """Strip quoted history, leaving only what the sender newly wrote.

    The `On ... wrote:` attribution is matched across line breaks because Gmail
    and Outlook both wrap it, which the previous single-line pattern missed.
    """
    if not body:
        return ""
    markers = [
        r"\n\s*On\b[\s\S]{0,300}?\bwrote:",
        r"\n\s*On\b[\s\S]{0,300}?\b(?:a|é)crit\s*:",
        r"\n\s*_{5,}\s*\n",
        r"\n\s*-{5,}\s*\n",
        r"\n\s*From:\s+",
        r"\n\s*Sent:\s+",
        r"\n\s*-{2,}\s*Original Message\s*-{2,}",
        r"\n\s*Get Outlook for\b",
        r"\n\s*Sent from my\b",
    ]
    latest = body
    for marker in markers:
        parts = re.split(marker, latest, maxsplit=1, flags=re.I | re.M)
        latest = parts[0]
    # Drop any residual quote lines that survived the header split.
    lines = [line for line in latest.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def extract_attachments(message: email.message.EmailMessage) -> list[tuple[str, bytes]]:
    attachments = []
    for part in message.walk():
        if part.get_content_disposition() not in {"attachment", "inline"} and not part.get_filename():
            continue

        filename = decode_mime(part.get_filename()) or f"attachment-{len(attachments) + 1}"
        content_type = part.get_content_type() or ""
        attachment_name = f"{filename}|{content_type}" if content_type else filename
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append((attachment_name, payload))

    return attachments


def attachment_display_name(filename: str) -> str:
    return (filename or "").split("|", 1)[0]


def is_cv_filename(filename: str) -> bool:
    return is_cv_attachment(filename, b"")


def is_cv_attachment(filename: str, payload: bytes | None = None) -> bool:
    raw = filename or ""
    display_name = attachment_display_name(raw)
    lowered = raw.lower()
    display_lower = display_name.lower()
    suffix = Path(display_lower).suffix
    if suffix in CV_EXTENSIONS:
        return True
    if any(mime in lowered for mime in ["application/pdf", "application/msword", "officedocument.wordprocessingml.document"]):
        return True
    if "text/plain" in lowered and any(word in display_lower for word in ["cv", "resume", "resumé", "biodata", "bio-data", "profile"]):
        return True
    if payload:
        if payload.startswith(b"%PDF") and any(word in display_lower for word in ["cv", "resume", "profile", "attachment"]):
            return True
        if payload[:2] == b"PK" and any(word in display_lower for word in ["cv", "resume", "profile", "attachment"]):
            return True
    return False


def safe_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_referral_thread(thread_context: str) -> bool:
    text = thread_context.lower()
    referral_patterns = [
        r"\bfriend'?s cv\b",
        r"\bfriend'?s resume\b",
        r"\bmy friend\b",
        r"\bfor my friend\b",
        r"\breferr?ing\b",
        r"\breferral\b",
        r"\bcandidate'?s cv\b",
        r"\bcandidate'?s resume\b",
    ]
    return any(re.search(pattern, text) for pattern in referral_patterns)


def clean_email(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
    return match.group(0).lower() if match else None


def short_fingerprint(value: str) -> str:
    if not value:
        return "empty"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def recruiter_email_body(*paragraphs: str) -> str:
    clean_paragraphs = [re.sub(r"\s+", " ", paragraph).strip() for paragraph in paragraphs if paragraph and paragraph.strip()]
    return "\r\n\r\n".join(["Hi,", *clean_paragraphs])


def linkify_html_text(value: str) -> str:
    escaped = html_escape(value)

    def link_url(match: re.Match) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,);]":
            trailing = url[-1] + trailing
            url = url[:-1]
        href = url
        return (
            f'<a href="{href}" style="color:#0563c1;text-decoration:underline;">{url}</a>'
            f"{trailing}"
        )

    escaped = re.sub(r"https?://[^\s<]+", link_url, escaped)

    def link_email(match: re.Match) -> str:
        email_address = match.group(0)
        return (
            f'<a href="mailto:{email_address}" style="color:#0563c1;text-decoration:underline;">'
            f"{email_address}</a>"
        )

    return re.sub(
        r"(?<![:/>])\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        link_email,
        escaped,
    )


def email_body_to_html(body: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\r?\n\r?\n", body) if part.strip()]
    html_parts = []
    for paragraph in paragraphs:
        normalized = paragraph.replace("\r\n", "\n").replace("\r", "\n")
        html_parts.append(f"<p>{linkify_html_text(normalized).replace(chr(10), '<br>')}</p>")
    return "".join(html_parts)


def recruiter_plain_signature() -> str:
    lines = [RECRUITER_SIGNATURE_SIGNOFF.strip(), "", RECRUITER_SIGNATURE_NAME.strip()]
    if RECRUITER_SIGNATURE_COMPANY:
        lines.append(RECRUITER_SIGNATURE_COMPANY.strip())
    if RECRUITER_SIGNATURE_EMAIL:
        lines.append(RECRUITER_SIGNATURE_EMAIL.strip())
    return "\r\n".join(line for line in lines if line is not None)


def recruiter_html_signature() -> str:
    if not RECRUITER_APPEND_SIGNATURE:
        return ""
    signoff = html_escape(RECRUITER_SIGNATURE_SIGNOFF.strip())
    name = html_escape(RECRUITER_SIGNATURE_NAME.strip())
    parts = [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">',
        f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;line-height:20px;color:#111827;padding:0 0 12px 0;">{signoff}</td></tr>',
        f'<tr><td style="font-family:Arial,sans-serif;font-size:14px;line-height:20px;color:#111827;padding:0;">{name}',
    ]
    if RECRUITER_SIGNATURE_COMPANY:
        company = html_escape(RECRUITER_SIGNATURE_COMPANY.strip())
        if RECRUITER_SIGNATURE_COMPANY_URL:
            company_url = html_escape(RECRUITER_SIGNATURE_COMPANY_URL.strip(), quote=True)
            parts.append(
                '<br><span style="font-size:15px;">&#127760;</span> '
                f'<a href="{company_url}" style="color:#6b46c1;font-weight:700;text-decoration:underline;">{company}</a>'
            )
        else:
            parts.append(f'<br><span style="font-size:15px;">&#127760;</span> {company}')
    if RECRUITER_SIGNATURE_EMAIL:
        email_address = html_escape(RECRUITER_SIGNATURE_EMAIL.strip())
        parts.append(
            '<br><span style="font-size:15px;">&#9993;&#65039;</span> '
            f'<a href="mailto:{email_address}" style="color:#111827;text-decoration:none;">{email_address}</a>'
        )
    parts.append("</td></tr>")
    if RECRUITER_SIGNATURE_LOGO_URL:
        logo_url = html_escape(RECRUITER_SIGNATURE_LOGO_URL.strip(), quote=True)
        alt = html_escape(RECRUITER_SIGNATURE_COMPANY.strip() or "Company logo", quote=True)
        parts.append(
            '<tr><td style="padding:8px 0 0 0;">'
            f'<a href="{html_escape(RECRUITER_SIGNATURE_COMPANY_URL.strip() or "#", quote=True)}" style="text-decoration:none;border:0;">'
            f'<img src="{logo_url}" alt="{alt}" width="180" border="0" '
            'style="display:block;width:180px;max-width:180px;height:auto;border:0;outline:none;text-decoration:none;-ms-interpolation-mode:bicubic;">'
            "</a></td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def draft_has_recruiter_signature(html: str) -> bool:
    if not html:
        return False
    top_html = re.split(
        r"(<hr\b|<blockquote\b|<div[^>]+class=[\"'][^\"']*(gmail_quote|WordSection|x_gmail_quote)[^\"']*[\"']|<p[^>]*>\s*from:)",
        html,
        maxsplit=1,
        flags=re.I,
    )[0]
    text = strip_html(top_html).lower()
    if not text.strip():
        return False
    markers = [
        RECRUITER_SIGNATURE_EMAIL.lower(),
        RECRUITER_SIGNATURE_COMPANY.lower(),
        RECRUITER_SIGNATURE_NAME.lower(),
    ]
    return any(marker and marker in text for marker in markers)


def append_signature_if_needed(body_html: str, existing_draft_html: str = "") -> str:
    if not RECRUITER_APPEND_SIGNATURE or draft_has_recruiter_signature(existing_draft_html):
        return body_html
    return f"{body_html}{recruiter_html_signature()}"


def candidate_email_from_cv(extracted: dict[str, Any], cv_text: str) -> str | None:
    return clean_email(extracted.get("email")) or clean_email(cv_text)


def non_indian_phone_reason(extracted: dict[str, Any], cv_text: str) -> str | None:
    values = [str(extracted.get("phone") or ""), cv_text[:4000]]
    combined = "\n".join(value for value in values if value)

    for match in re.finditer(r"(?<!\w)(\+|00)[\d\s().-]{8,22}\d\b", combined):
        prefix = match.group(1)
        raw_number = match.group(0)
        digits = re.sub(r"\D", "", raw_number)
        if prefix == "+" and not digits.startswith("91"):
            return f"Detected non-India phone number: {raw_number.strip()}"
        if prefix == "00" and not digits.startswith("0091"):
            return f"Detected non-India phone number: {raw_number.strip()}"

    extracted_phone = str(extracted.get("phone") or "").strip()
    if extracted_phone:
        phone_digits = re.sub(r"\D", "", extracted_phone)
        if extracted_phone.startswith("+") and not extracted_phone.startswith("+91"):
            return f"Extracted phone number is not an India +91 number: {extracted_phone}"
        if extracted_phone.startswith("00") and not phone_digits.startswith("0091"):
            return f"Extracted phone number is not an India +91 number: {extracted_phone}"

    return None


def score_number(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


NON_NUMERIC_TOKENS = {"null", "none", "na", "n/a", "nil", "-", "immediate", "fresher", "unknown"}


def numeric_value(value: Any) -> float | None:
    """Coerce whatever the LLM produced into something a numeric column accepts.

    Models routinely return "10+", "85%", "10-12 years" or "6 LPA" for fields
    typed NUMERIC in Postgres, and the raw string aborts the transaction. The
    leading number is the useful part, so take it rather than dropping the value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in NON_NUMERIC_TOKENS:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


# A shortfall smaller than this is not worth rejecting over; CVs round their
# dates and "1.8 years" against a 2 year minimum is noise, not a gap.
EXPERIENCE_TOLERANCE_YEARS = float(os.getenv("RECRUITER_EXPERIENCE_TOLERANCE_YEARS", "0.5"))


def experience_shortfall(
    requirement: dict[str, Any] | None,
    extracted: dict[str, Any] | None,
) -> str | None:
    """Reason the candidate is short of the role's stated minimum, if they are.

    experience_min_years was stored on every requirement and never once
    consulted, so a fresher applying to a role that asks for two years relied
    entirely on the LLM choosing to score them low. That is a stated, checkable
    requirement and it should be checked.

    Returns None when the candidate meets it, or when the CV does not state a
    figure - an unknown is not a shortfall, and rejecting on a missing field
    would throw away candidates whose CV simply lists dates instead of a total.
    """
    if not requirement:
        return None
    required = numeric_value(requirement.get("experience_min_years"))
    if not required:
        return None
    candidate_years = numeric_value((extracted or {}).get("total_experience_years"))
    if candidate_years is None:
        return None
    if candidate_years + EXPERIENCE_TOLERANCE_YEARS >= required:
        return None
    return (
        f"{candidate_years:g} year(s) of experience against a stated minimum of "
        f"{required:g} for {requirement.get('position_title') or 'this role'}"
    )


def passes_screening_threshold(
    evaluation: dict[str, Any],
    requirement: dict[str, Any] | None,
    extracted: dict[str, Any] | None = None,
) -> bool:
    if not requirement:
        return False
    if experience_shortfall(requirement, extracted):
        return False
    ats_score = score_number(evaluation.get("ats_score"))
    jd_match_score = score_number(evaluation.get("jd_match_score"))
    return (
        ats_score is not None
        and jd_match_score is not None
        and ats_score >= RECRUITER_SCREENING_ATS_MIN
        and jd_match_score >= RECRUITER_SCREENING_JD_MIN
    )


def dashboard_application_url(application_id: int | str) -> str:
    return f"{RECRUITER_DASHBOARD_BASE_URL.rstrip('/')}/applications/{application_id}"


def candidate_interview_url(token: str) -> str:
    return f"{RECRUITER_INTERVIEW_BASE_URL.rstrip('/')}/interview/{token}"


def application_candidate_recipient(application: dict[str, Any]) -> str | None:
    if (application.get("submission_type") or "").lower() == "referral":
        return application.get("candidate_email") or application.get("source_email")
    return application.get("source_email") or application.get("candidate_email")


def screening_work_terms() -> dict[str, Any]:
    return {
        "shift": RECRUITER_WORK_SHIFT,
        "work_mode": RECRUITER_WORK_MODE,
        "office_location": RECRUITER_OFFICE_LOCATION,
        "cab_facility": RECRUITER_CAB_FACILITY,
        "working_days": RECRUITER_WORKING_DAYS,
    }


SCREENING_FIELD_LABELS = (
    ("comfortable_with_terms", "confirmation of the shift/office work terms"),
    ("current_salary", "current salary"),
    ("expected_salary", "expected salary"),
    ("current_location", "current location"),
)


def missing_screening_fields(application: dict[str, Any] | None) -> list[str]:
    """Which screening answers are still unknown for this application."""
    answers = json_dict((application or {}).get("screening_details"))
    missing = [label for key, label in SCREENING_FIELD_LABELS if answers.get(key) in (None, "", [])]
    # Joining is answered by either a notice period or a date.
    if joining_days_from_answers(answers) is None:
        missing.append("joining time / notice period")
    return missing


MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_joining_date(value: Any):
    """Read a joining date the candidate stated, in whatever shape they wrote it.

    Candidates answer "how soon can you join?" with a date at least as often as
    with a number of days - "4 Sep", "after 31 August", "2026-09-04". Only
    joining_days was ever accepted, so those answers never satisfied the
    screening check and the agent asked the same question again.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    now = recruiter_now()

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=now.tzinfo).date()
        except ValueError:
            return None

    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        text,
        flags=re.I,
    ) or re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        text,
        flags=re.I,
    )
    if not match:
        return None
    groups = match.groups()
    day, month_name = (groups[0], groups[1]) if groups[0].isdigit() else (groups[1], groups[0])
    month = MONTH_NUMBERS.get(month_name.lower())
    if not month:
        return None
    try:
        candidate = datetime(now.year, month, int(day), tzinfo=now.tzinfo).date()
    except ValueError:
        return None
    if candidate < now.date():
        days_past = (now.date() - candidate).days
        if days_past <= JOINING_DATE_RECENT_PAST_DAYS:
            # "after 31 August", read on 1 September. They are free now. Saying
            # the answer is unreadable makes the agent ask again for something
            # the candidate has already told it.
            return now.date()
        # A joining date that has passed means next year only across a year end.
        try:
            rolled = datetime(now.year + 1, month, int(day), tzinfo=now.tzinfo).date()
        except ValueError:
            return None
        if (rolled - now.date()).days > YEAR_ROLLOVER_MAX_DAYS:
            return None
        candidate = rolled
    return candidate


def joining_days_from_answers(answers: dict[str, Any]) -> int | None:
    """Notice period in days, however the candidate expressed it."""
    days = screening_int(answers.get("joining_days"))
    if days is not None:
        return days
    joining_date = parse_joining_date(answers.get("joining_date"))
    if joining_date:
        return max((joining_date - recruiter_now().date()).days, 0)
    return None


def screening_answers_complete(answers: dict[str, Any]) -> bool:
    if joining_days_from_answers(answers) is None:
        return False
    return all(
        answers.get(key) not in (None, "", [])
        for key in ["comfortable_with_terms", "current_salary", "expected_salary", "current_location"]
    )


def candidate_declined_required_location(text: str) -> bool:
    latest = normalize_position_text(latest_reply_text(text))
    if not latest:
        return False
    if any(
        phrase in latest
        for phrase in [
            "not ready to relocate",
            "not willing to relocate",
            "cannot relocate",
            "cant relocate",
            "can't relocate",
            "not relocate",
            "not comfortable to relocate",
            "not comfortable with mohali",
            "not open to mohali",
            "not ready for mohali",
            "cannot move to mohali",
            "cant move to mohali",
            "can't move to mohali",
        ]
    ):
        return True
    preferred_city_only = any(city in latest for city in ["hyderabad", "bangalore", "bengaluru", "remote", "work from home"])
    mohali_missing = "mohali" not in latest
    if preferred_city_only and mohali_missing and any(
        phrase in latest
        for phrase in [
            "only",
            "looking for",
            "preferred location",
            "prefer",
            "if had any vacancies",
            "if you have any vacancies",
        ]
    ):
        return True
    return False


def json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def merge_screening_answers(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    merged = json_dict(existing).copy()
    for key, value in (incoming or {}).items():
        if value not in (None, "", []):
            merged[key] = value
    return merged


def screening_int(value: Any) -> int | None:
    number = score_number(value)
    return int(number) if number is not None else None


def requirement_budget_text(requirement: dict[str, Any] | None) -> str:
    if not requirement:
        return "the approved budget"
    currency = requirement.get("currency") or "INR"
    budget_min = requirement.get("budget_min")
    budget_max = requirement.get("budget_max")
    if budget_min and budget_max:
        return f"{currency} {budget_min} to {budget_max}"
    if budget_max:
        return f"up to {currency} {budget_max}"
    return "the approved budget"


def annualised_amount(value: Any) -> float | None:
    """Put a salary figure on an annual footing so two of them can be compared.

    A number below the floor is a monthly figure: nobody is paid 60000 a year,
    and both requirements and candidates supply monthly numbers in practice.
    """
    amount = score_number(value)
    if amount is None or amount <= 0:
        return None
    return amount * 12 if amount < BUDGET_ANNUAL_FLOOR else amount


def screening_fit(answers: dict[str, Any], requirement: dict[str, Any] | None) -> tuple[bool, list[str]]:
    issues = []
    if answers.get("comfortable_with_terms") is False:
        issues.append("candidate is not comfortable with the shift/office terms")
    expected_salary = annualised_amount(answers.get("expected_salary"))
    budget_max = annualised_amount(requirement.get("budget_max") if requirement else None)
    if expected_salary is not None and budget_max is not None and expected_salary > budget_max:
        raw_expected = score_number(answers.get("expected_salary"))
        raw_budget = score_number((requirement or {}).get("budget_max"))
        issues.append(
            f"expected salary is above budget ({raw_expected} > {raw_budget})"
            if (raw_expected, raw_budget) == (expected_salary, budget_max)
            else f"expected salary is above budget ({raw_expected} > {raw_budget}, "
                 f"compared annually as {expected_salary:.0f} > {budget_max:.0f})"
        )
    return not issues, issues


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_time_from_text(text: str) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.I)
    if not match:
        match = re.search(r"\b(?:at|around|by)\s+(\d{1,2})(?::(\d{2}))?\b", text, flags=re.I)
    if not match:
        match = re.search(r"\b(\d{1,2}):(\d{2})\b", text, flags=re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) if len(match.groups()) >= 3 and match.group(3) else "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not meridiem and 1 <= hour <= 8:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return hour, minute


WEEKDAY_NAMES = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# A bare "19 Aug" that has already passed almost never means next year. Rolling
# the year forward is only sensible across a December/January boundary, so it is
# accepted only when it lands inside this window.
# A stated joining date this recently past means "available now", not next year
# and not unreadable.
JOINING_DATE_RECENT_PAST_DAYS = int(os.getenv("RECRUITER_JOINING_DATE_RECENT_PAST_DAYS", "60"))
YEAR_ROLLOVER_MAX_DAYS = int(os.getenv("RECRUITER_YEAR_ROLLOVER_MAX_DAYS", "60"))
# Nothing in a recruiting pipeline is scheduled further out than this. Anything
# beyond it is a parsing error, not an intention.
MAX_SCHEDULE_DAYS_AHEAD = int(os.getenv("RECRUITER_MAX_SCHEDULE_DAYS_AHEAD", "90"))


def next_weekday_date(weekday: int, now: datetime):
    """The next occurrence of this weekday, never today."""
    ahead = (weekday - now.weekday()) % 7
    return (now + timedelta(days=ahead or 7)).date()


def parse_interview_datetime_fallback(text: str) -> datetime | None:
    """Read a slot out of what the candidate just wrote.

    Only the newest part of the message is considered. Reading the whole body
    meant quoted headers like "On Wed, 19 Aug 2026 at 11:28 PM, Career wrote:"
    were parsed as the candidate's availability - which is how a final round was
    booked for 19 August 2027.
    """
    latest = latest_reply_text(text or "")
    normalized = normalize_position_text(latest)
    now = recruiter_now()
    target_date = None
    weekday_only = False

    if "tomorrow" in normalized or "tommorow" in normalized or "next day" in normalized:
        target_date = (now + timedelta(days=1)).date()
    elif "today" in normalized:
        target_date = now.date()

    if not target_date:
        month_names = {
            "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
            "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
            "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
        }
        date_match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
            latest,
            flags=re.I,
        )
        if date_match:
            day = int(date_match.group(1))
            month = month_names[date_match.group(2).lower()]
            try:
                candidate = datetime(now.year, month, day, tzinfo=now.tzinfo).date()
            except ValueError:
                candidate = None
            if candidate and candidate < now.date():
                try:
                    rolled = datetime(now.year + 1, month, day, tzinfo=now.tzinfo).date()
                except ValueError:
                    rolled = None
                # Only a genuine year boundary, never "yesterday" meaning next August.
                candidate = (
                    rolled
                    if rolled and (rolled - now.date()).days <= YEAR_ROLLOVER_MAX_DAYS
                    else None
                )
            target_date = candidate

    if not target_date:
        # "I am available on friday, monday and tuesday" - take the soonest.
        weekday_hits = {
            WEEKDAY_NAMES[word]
            for word in re.findall(r"[a-z]+", normalized)
            if word in WEEKDAY_NAMES
        }
        if weekday_hits:
            target_date = min(next_weekday_date(day, now) for day in weekday_hits)
            # Naming a day without a time is an offer of that whole day; the
            # HR window coercion picks a real slot inside it.
            weekday_only = True

    flexible = any(
        phrase in normalized
        for phrase in [
            "anytime",
            "any time",
            "as per your convenience",
            "your convenience",
            "you can schedule",
            "schedule it",
        ]
    )
    time_parts = parse_time_from_text(latest)
    if not target_date and flexible:
        target_date = (now + timedelta(days=1)).date()
    if not target_date:
        return None
    if time_parts:
        hour, minute = time_parts
    elif flexible or weekday_only:
        hour, minute = 11, 0
    else:
        return None

    scheduled_at = datetime.combine(target_date, datetime.min.time(), tzinfo=recruiter_tz()).replace(hour=hour, minute=minute)
    if scheduled_at <= now:
        scheduled_at += timedelta(days=1)
    if (scheduled_at - now).days > MAX_SCHEDULE_DAYS_AHEAD:
        # Whatever produced this was not a date the candidate offered.
        log_json(
            logging.WARNING,
            "interview_slot_rejected_too_far_ahead",
            scheduled_at=scheduled_at.isoformat(),
            days_ahead=(scheduled_at - now).days,
        )
        return None
    return scheduled_at


def is_cv_role_override_request(latest_body: str, thread_context: str) -> bool:
    latest_text = normalize_position_text(latest_body)
    thread_text = normalize_position_text(thread_context)
    correction_patterns = [
        r"\b(previous|above|earlier|last) (message|messages|mail|email|emails) (was|were)? ?(wrong|worng|incorrect|mistake)\b",
        r"\b(ignore|disregard) (the )?(previous|above|earlier|last) (message|messages|mail|email|emails)\b",
        r"\b(apply|applying|application) for (the )?(role|position|job) (in|inside|mentioned in|as per|from) (the )?(cv|resume)\b",
        r"\b(position|role|job) (is|should be) (the )?(one )?(in|inside|mentioned in|as per|from) (the )?(cv|resume)\b",
        r"\bwant to apply for (the )?(position|role|job) (in|inside|mentioned in|as per|from) (the )?(cv|resume)\b",
    ]
    has_correction = any(re.search(pattern, latest_text) for pattern in correction_patterns)
    mentions_cv_role = any(
        phrase in thread_text
        for phrase in [
            "position in cv",
            "role in cv",
            "job in cv",
            "position from cv",
            "role from cv",
            "as per cv",
            "mentioned in cv",
        ]
    )
    return has_correction or mentions_cv_role


def is_withdrawal_request(latest_body: str) -> bool:
    text = normalize_position_text(latest_body)
    patterns = [
        r"\bwithdraw (my )?(application|cv|resume)\b",
        r"\bremove (my )?(application|cv|resume|profile)\b",
        r"\bnot interested\b",
        r"\bno longer interested\b",
        r"\bdo not want to continue\b",
        r"\bplease cancel\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def is_status_followup(latest_body: str) -> bool:
    """True only for a genuine enquiry about progress.

    The bare word "interview" used to match, so a candidate writing "thank you
    for inviting me to interview" was treated as chasing an update and had the
    interview link re-sent to them.
    """
    raw = latest_reply_text(latest_body or "")
    text = normalize_position_text(raw)
    if not text:
        return False
    if is_pure_acknowledgement(raw):
        return False
    enquiry_patterns = [
        r"\bany update\b",
        r"\bany news\b",
        r"\bstatus of (my|the) (application|profile|candidature)\b",
        r"\b(what|whats|what is) the status\b",
        r"\bcurrent status\b",
        r"\bplease (share|provide|update) .{0,20}status\b",
        r"\bwhen can i expect\b",
        r"\bwhen will i (hear|know|get)\b",
        r"\bhave(nt| not)? (you )?(heard|got) back\b",
        r"\bhaven t heard\b",
        r"\bawaiting (your )?(response|reply|update)\b",
        r"\bfollowing up\b",
        r"\bjust checking\b",
        r"\bchecking in\b",
        r"\bkindly update\b",
        r"\bwhat(s| is) the next step\b",
        r"\bam i shortlisted\b",
        r"\bwas i shortlisted\b",
    ]
    if any(re.search(pattern, text) for pattern in enquiry_patterns):
        return True
    # A question mark plus a progress word is also a genuine enquiry.
    if "?" in raw and re.search(r"\b(status|update|shortlisted|next step|next steps|progress)\b", text):
        return True
    return False


ACKNOWLEDGEMENT_PHRASES = (
    "thank you",
    "thanks",
    "thankyou",
    "noted",
    "sure",
    "okay",
    "ok",
    "will do",
    "got it",
    "received",
    "acknowledged",
    "great",
    "perfect",
    "looking forward",
    "appreciate it",
    "much appreciated",
)

ACKNOWLEDGEMENT_FILLER = {
    "hi",
    "hello",
    "dear",
    "sir",
    "madam",
    "team",
    "hr",
    "regards",
    "best",
    "kind",
    "warm",
    "thanks",
    "thank",
    "you",
    "your",
    "yours",
    "i",
    "me",
    "my",
    "we",
    "for",
    "the",
    "and",
    "so",
    "very",
    "much",
    "a",
    "lot",
    "it",
    "this",
    "that",
    "to",
    "of",
    "sincerely",
    "sir/madam",
}

# Anything here means the message carries real content and deserves a reply.
ACKNOWLEDGEMENT_CONTENT_PATTERN = re.compile(
    r"\b(salary|ctc|lpa|lakh|notice|period|join|joining|location|relocat|shift|"
    r"resume|cv|attach|experience|expected|current|budget|available|availability|"
    r"reschedul|cancel|withdraw|interview link|not able|unable|issue|problem|"
    r"when|where|what|why|how|which|who)\b"
)


HUMAN_ATTENTION_PATTERNS = (
    r"\?",
    r"\b(please|kindly|could you|can you|would you|request)\b",
    r"\b(reconsider|revert|update me|follow ?up|status|any other|other role|other position)\b",
    r"\b(when|why|how|what|which|where)\b",
    r"\b(call|contact|discuss|clarify|explain)\b",
)


def message_needs_human_attention(latest_body: str) -> bool:
    """Whether a reply on a closed thread is actually asking for something.

    A candidate answering a final update with "Noted, thank you for letting me
    know, I appreciate the update on my application" is past the courtesy-note
    test - it is too long and too varied - but it still asks for nothing. Eight
    of sixteen manual-review emails in the sampled fortnight were exactly this.
    """
    text = latest_reply_text(latest_body or "").strip()
    if not text:
        return False
    return any(re.search(pattern, text, flags=re.I) for pattern in HUMAN_ATTENTION_PATTERNS)


def is_pure_acknowledgement(latest_body: str) -> bool:
    """True when the message is a courtesy note that needs no reply.

    Replying to "Thanks, I will do that" is what made the agent feel relentless;
    the candidate had no way to end the exchange except by going silent.
    """
    raw = latest_reply_text(latest_body or "").strip()
    if not raw:
        return False
    if "?" in raw:
        return False
    text = normalize_position_text(raw)
    if not text:
        return False
    words = text.split()
    if len(words) > 25:
        return False
    if not any(phrase in text for phrase in ACKNOWLEDGEMENT_PHRASES):
        return False
    if ACKNOWLEDGEMENT_CONTENT_PATTERN.search(text):
        return False
    if re.search(r"\b\d{3,}\b", raw):
        return False
    substantive = [word for word in words if word not in ACKNOWLEDGEMENT_FILLER]
    return len(substantive) <= 4


def budget_gap_ratio(answers: dict[str, Any], requirement: dict[str, Any] | None) -> float | None:
    """How far above the approved maximum the candidate is, as a multiple.

    1.1 means they want ten percent more than the ceiling, which is worth a
    conversation. 2.0 means they are applying to the wrong salary band.
    """
    expected = annualised_amount((answers or {}).get("expected_salary"))
    budget_max = annualised_amount((requirement or {}).get("budget_max"))
    if expected is None or not budget_max or budget_max <= 0:
        return None
    return expected / budget_max


def screening_issue_kind(issues: list[str]) -> str | None:
    """Classify why screening_fit failed, so every failure has a route.

    Previously only a salary failure could reach HR; a work-terms failure
    re-entered the negotiation state forever with no exit.
    """
    if not issues:
        return None
    joined = " ".join(str(issue).lower() for issue in issues)
    if "budget" in joined or "salary" in joined:
        return "budget"
    if "shift" in joined or "office" in joined or "terms" in joined or "location" in joined:
        return "terms"
    return "other"


BUDGET_REJECTION_PATTERNS = (
    r"\bcan(no|')?t\b",
    r"\bcannot\b",
    r"\bnot (ok|okay|comfortable|possible|acceptable|feasible|workable)\b",
    r"\btoo low\b",
    r"\btoo less\b",
    r"\bvery low\b",
    r"\bnot interested\b",
    r"\bwon(')?t work\b",
    r"\bdoes ?n(o|')?t work\b",
    r"\bbelow my\b",
    r"\bless than my\b",
    r"\bnot able to\b",
)

BUDGET_ACCEPTANCE_PATTERNS = (
    r"\b(i )?agree\b",
    r"\bagreed\b",
    r"\bi accept\b",
    r"\bacceptable\b",
    r"\bthat works\b",
    r"\bworks for me\b",
    r"\bfine (with|by) me\b",
    r"\bi am fine\b",
    r"\bi'?m fine\b",
    r"\b(i am|i'?m) (ok|okay|comfortable)\b",
    r"\bno issue\b",
    r"\bno problem\b",
    r"\bplease proceed\b",
    r"\bproceed further\b",
    r"\bgo ahead\b",
    r"\bmove ahead\b",
    r"\bmove forward\b",
    r"\bready to (work|join|proceed)\b",
    r"\bcan adjust\b",
    r"\bwithin (my )?budget\b",
)


def deterministic_budget_signal(text: str, requirement: dict[str, Any] | None = None) -> str | None:
    """Cheap, high-precision read of a budget answer, or None if unclear.

    Deliberately does NOT scan for bare numbers. The previous implementation
    returned "accepted" for any figure below the ceiling, so "my notice period
    is 90 days" silently bypassed HR approval.
    """
    raw = latest_reply_text(text or "")
    lowered = normalize_position_text(raw)
    if not lowered:
        return None
    if any(re.search(pattern, lowered) for pattern in BUDGET_REJECTION_PATTERNS):
        return "rejects"
    if any(re.search(pattern, lowered) for pattern in BUDGET_ACCEPTANCE_PATTERNS):
        return "accepts"
    if re.fullmatch(r"(yes|yeah|yep|sure|ok|okay)\b.{0,20}", lowered.strip()):
        return "accepts"
    return None


# Phrases that reveal the sender is not who the signature claims to be. The
# mailbox signs as "HR Team", so telling a candidate their profile will be
# "shared with the team" announces a forward to itself.
PERSONA_LEAK_PATTERNS = (
    r"\b(our|the|your) (hr )?team (will|would|can|is|are|has|have)\b",
    r"\bwith (our|the) team\b",
    r"\bto (our|the) (hr )?team\b",
    r"\bhr team\b",
    r"\bpass(ing|ed)? (this|it|your|the) .{0,20}(on|to|along)\b",
    r"\bforward(ing|ed)? (this|it|your|the)\b",
    r"\bescalat(e|es|ed|ing|ion)\b",
    r"\binternal(ly)?\b",
    r"\bour system\b",
    r"\bin our records\b",
    r"\bthe recruiter\b",
    r"\bconcerned (department|team|person)\b",
    r"\bai\b",
    r"\bautomated\b",
    r"\bbot\b",
    r"\balgorithm\b",
    r"\bats score\b",
    r"\bdatabase\b",
)

# Legitimate references to a genuinely different, named human.
PERSONA_LEAK_ALLOWLIST = (
    r"\bhr manager\b",
    r"\bhiring manager\b",
)


def persona_leak(body: str) -> str | None:
    """Return the offending phrase if the reply breaks the HR persona."""
    text = " ".join((body or "").split()).lower()
    if not text:
        return None
    for allowed in PERSONA_LEAK_ALLOWLIST:
        text = re.sub(allowed, " ", text)
    for pattern in PERSONA_LEAK_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def strip_persona_leak_lines(body: str) -> str:
    """Drop the sentences that break persona, keeping the rest of the reply."""
    kept_paragraphs = []
    for paragraph in (body or "").split("\n\n"):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        clean = [sentence for sentence in sentences if not persona_leak(sentence)]
        rebuilt = " ".join(part.strip() for part in clean if part.strip()).strip()
        if rebuilt:
            kept_paragraphs.append(rebuilt)
    result = "\n\n".join(kept_paragraphs).strip()
    if not result or len(result.split()) < 6:
        return recruiter_email_body(
            "Thanks for getting back to me.",
            "I have everything I need for now and will come back to you shortly with the next step.",
        )
    if not result.lower().startswith("hi"):
        result = f"Hi,\n\n{result}"
    return result


# Fields safe to hand an LLM. Everything else on the application row - notably
# attachment_payload (the raw CV bytes) and raw_cv_text - used to be serialized
# straight into prompts by json.dumps(application, default=str).
APPLICATION_PROMPT_FIELDS = (
    "id",
    "detected_position",
    "matched_position",
    "requirement_position",
    "application_status",
    "ats_score",
    "jd_match_score",
    "budget_min",
    "budget_max",
    "currency",
    "needed_within_days",
    "screening_details",
    "screening_current_salary",
    "screening_expected_salary",
    "screening_current_location",
    "screening_joining_days",
    "interview_availability",
    "interview_scheduled_at",
    "budget_disclosed_at",
    "budget_response",
    "full_name",
)


def application_prompt_facts(application: dict[str, Any] | None) -> dict[str, Any]:
    if not application:
        return {}
    facts = {
        key: application.get(key)
        for key in APPLICATION_PROMPT_FIELDS
        if application.get(key) is not None
    }
    summary = application.get("cv_summary") or application.get("ai_short_description")
    if summary:
        facts["cv_summary"] = str(summary)[:1200]
    return facts


# "tomorrow" is misspelled more often than not, and candidates say they will
# "give", "attend" or "sit" the interview as readily as "complete" it.
LATER_WORDS = r"(later|soon|tomorrow|tommorow|tommorrow|tomorow|tmrw|tmr|next week|monday|tuesday|wednesday|thursday|friday|weekend|in a few days|after a few days)"
INTERVIEW_VERBS = r"(do|complete|finish|take|attempt|give|attend|sit|join|start)"


def wants_a_fresh_interview_link(latest_body: str) -> bool:
    """Candidate is asking for the link again, not reporting a problem with it."""
    text = normalize_position_text(latest_reply_text(latest_body or ""))
    if not text:
        return False
    return any(
        re.search(pattern, text)
        for pattern in [
            r"\b(send|share|resend|re send|forward|give)\b[^.]{0,24}\b(new|another|fresh|again|link)\b",
            r"\bnew link\b",
            r"\banother link\b",
            r"\blink again\b",
            r"\bresend\b",
        ]
    )


def is_interview_delay_reply(latest_body: str) -> bool:
    """Candidate intends to sit the interview, just not right now."""
    text = normalize_position_text(latest_reply_text(latest_body or ""))
    if not text:
        return False
    patterns = [
        r"\b(i am|i'm|im)\s+(busy|occupied|tied up|travelling|traveling|unwell|sick)\b",
        r"\bnot\s+(available|free)\s+(today|right now|currently|at the moment|now)\b",
        rf"\b(i will|i'll|will|can i|i can|i would)\s+{INTERVIEW_VERBS}\s+(it|the interview|this)?\s*{LATER_WORDS}\b",
        rf"\b(i will|i'll|will)\s+{INTERVIEW_VERBS}\b[^.]{{0,30}}\b{LATER_WORDS}\b",
        rf"\b{LATER_WORDS}\s+(i will|i'll|i can|i would)\b",
        r"\b(in|after)\s+(a\s+)?few\s+days\b",
        r"\bneed\s+(some|a little|more)?\s*time\b",
        r"\bcan\s+i\s+(do|complete|take|give|attend)\s+it\s+later\b",
        rf"\bwill\s+(complete|do|give|attend)\s+{LATER_WORDS}\b",
        rf"\b{LATER_WORDS}\b[^.]{{0,20}}\b(i will|i'll|i ll|i can)\b",
        # Apostrophes are stripped by normalisation, so "I'll" arrives as "i ll".
        rf"\b(i ll|ill)\s+{INTERVIEW_VERBS}\b[^.]{{0,30}}\b{LATER_WORDS}\b",
        # Catch-all: an interview verb and a later-word in the same clause.
        rf"\b{INTERVIEW_VERBS}\b[^.]{{0,30}}\b{LATER_WORDS}\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_cv_details(extracted: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "full_name": None,
        "email": None,
        "phone": None,
        "location": None,
        "linkedin_url": None,
        "portfolio_url": None,
        "target_position": None,
        "current_title": None,
        "current_company": None,
        "total_experience_years": None,
        "skills": [],
        "education": [],
        "work_history": [],
        "certifications": [],
        "projects": [],
        "achievements": [],
    }
    normalized = {**defaults, **extracted}
    for key in ["skills", "education", "work_history", "certifications", "projects", "achievements"]:
        normalized[key] = ensure_list(normalized.get(key))
    # "10+" and friends are typed NUMERIC in the database and abort the insert.
    normalized["total_experience_years"] = numeric_value(normalized.get("total_experience_years"))
    return normalized


def normalize_evaluation(evaluation: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "ats_score": None,
        "jd_match_score": None,
        "short_description": "",
        "strengths": [],
        "risks": [],
        "missing_requirements": [],
        "recommendation": "review",
        "reasoning": "",
    }
    normalized = {**defaults, **evaluation}
    for key in ["strengths", "risks", "missing_requirements"]:
        normalized[key] = ensure_list(normalized.get(key))
    for key in ["ats_score", "jd_match_score"]:
        normalized[key] = numeric_value(normalized.get(key))
    return normalized


def normalize_position_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\bdev\b", "developer", value)
    value = re.sub(r"\bacct\b", "accountant", value)
    value = re.sub(r"\baccounting\b", "accountant", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [
        word
        for word in value.split()
        if word not in {"job", "role", "position", "opening", "vacancy", "profile"}
    ]
    return " ".join(words)


def position_tokens(value: str | None) -> set[str]:
    return set(normalize_position_text(value).split())


GENERIC_ROLE_WORDS = {
    "job",
    "role",
    "position",
    "opening",
    "vacancy",
    "profile",
    "candidate",
    "resume",
    "cv",
    "senior",
    "sr",
    "junior",
    "jr",
    "lead",
    "trainee",
    "intern",
    "associate",
    "assistant",
    "executive",
    "specialist",
    "manager",
    "officer",
    "staff",
    "team",
    "full",
    "cycle",
    "us",
    "uk",
    "india",
}


def meaningful_role_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in position_tokens(value)
        if token not in GENERIC_ROLE_WORDS and len(token) > 1
    }


def deterministic_requirement_match(
    extracted: dict[str, Any],
    classification: dict[str, Any],
    requirements: list[dict[str, Any]],
    source_text: str = "",
) -> dict[str, Any]:
    candidate_positions = [
        extracted.get("target_position"),
        extracted.get("current_title"),
        classification.get("detected_position"),
    ]
    candidate_role_text = normalize_position_text(" ".join(value for value in candidate_positions if value))
    candidate_skill_text = normalize_position_text(
        " ".join(str(skill) for skill in ensure_list(extracted.get("skills")))
    )
    candidate_text = normalize_position_text(
        " ".join(value for value in [candidate_role_text, candidate_skill_text] if value)
    )
    if not candidate_text and source_text:
        candidate_text = normalize_position_text(source_text[:1200])
    candidate_words = position_tokens(candidate_text)
    if not candidate_words:
        return {"requirement_id": None, "confidence": 0, "reason": "No candidate role detected"}
    candidate_role_words = meaningful_role_tokens(candidate_role_text) or meaningful_role_tokens(candidate_text)
    if not candidate_role_words:
        return {"requirement_id": None, "confidence": 0, "reason": "No meaningful candidate role detected"}

    # Everything we know about the candidate, not just their job title. A CV
    # headed "Fractional CFO & US Bookkeeping" was previously matched on the
    # title alone, so the word "bookkeeping" - the reason they are a fit - never
    # reached the comparison.
    evidence_stems = stem_role_tokens(
        " ".join(
            value
            for value in [candidate_role_text, candidate_skill_text, normalize_position_text(source_text[:6000])]
            if value
        )
    )

    best_requirement = None
    best_score = 0.0
    best_reason = "No deterministic position-title match"

    for requirement in requirements:
        title = requirement.get("position_title")
        title_text = normalize_position_text(title)
        title_words = meaningful_role_tokens(title)
        if not title_words:
            continue

        if title_text and candidate_role_text and title_text in candidate_role_text:
            score = 1.0
            reason = "Requirement title appears in candidate role/title"
        elif candidate_role_text and candidate_role_text in title_text:
            score = 0.95
            reason = "Candidate role/title appears in requirement title"
        else:
            overlap = candidate_role_words & title_words
            score = len(overlap) / max(len(title_words), 1)
            reason = f"Shared meaningful role tokens: {sorted(overlap)}" if overlap else "No shared meaningful role tokens"

        # Fall back to evidence: does the candidate demonstrably do this work,
        # whatever they call themselves? Stemmed so "bookkeeper" finds
        # "bookkeeping". Weighted just under a real title match so an exact
        # title still wins when both are present.
        distinctive_words = title_words - EVIDENCE_WEAK_TOKENS
        title_stems = {stem_role_token(word) for word in distinctive_words}
        evidence_hits = title_stems & evidence_stems
        evidence_score = (
            (len(evidence_hits) / max(len(title_stems), 1)) * EVIDENCE_MATCH_WEIGHT
            if title_stems
            else 0.0
        )
        if evidence_score > score:
            score = evidence_score
            reason = (
                f"Requirement terms {sorted(evidence_hits)} evidenced in the candidate's CV/skills"
            )

        if score > best_score:
            best_score = score
            best_requirement = requirement
            best_reason = reason

    if best_requirement and best_score >= 0.65:
        return {
            "requirement_id": best_requirement["id"],
            "confidence": round(best_score, 2),
            "reason": best_reason,
        }

    return {
        "requirement_id": None,
        "confidence": round(best_score, 2),
        "reason": "No deterministic position-title match",
    }


def requirement_is_compatible_with_candidate_role(
    extracted: dict[str, Any],
    classification: dict[str, Any],
    requirement: dict[str, Any] | None,
) -> bool:
    if not requirement:
        return True
    candidate_role_text = normalize_position_text(
        " ".join(
            value
            for value in [
                extracted.get("target_position"),
                extracted.get("current_title"),
                classification.get("detected_position"),
            ]
            if value
        )
    )
    if not candidate_role_text:
        return True
    title_text = normalize_position_text(requirement.get("position_title"))
    if not title_text:
        return True
    if title_text in candidate_role_text or candidate_role_text in title_text:
        return True

    role_words = meaningful_role_tokens(candidate_role_text)
    requirement_text = normalize_position_text(
        " ".join(
            str(value)
            for value in [
                requirement.get("position_title"),
                requirement.get("job_description"),
            ]
            if value
        )
    )
    requirement_words = meaningful_role_tokens(requirement_text)
    overlap = role_words & requirement_words
    if overlap:
        return True
    return False


def classification_has_specific_role(classification: dict[str, Any]) -> bool:
    role = normalize_position_text(classification.get("detected_position"))
    if not role:
        return False
    vague_roles = {
        "job",
        "any",
        "any suitable",
        "suitable",
        "opening",
        "vacancy",
        "resume",
        "cv",
        "profile",
    }
    if role in vague_roles:
        return False
    return len(position_tokens(role)) >= 1


def application_has_saved_cv(application: dict[str, Any] | None) -> bool:
    if not application:
        return False
    for field in [
        "attachment_filename",
        "attachment_sha256",
        "raw_cv_text",
        "cv_summary",
        "ai_short_description",
    ]:
        if str(application.get(field) or "").strip():
            return True
    return False


def application_role_is_compatible(
    application: dict[str, Any] | None,
    classification: dict[str, Any],
    thread_context: str,
) -> bool:
    if not application or not classification_has_specific_role(classification):
        return True
    application_role = (
        application.get("requirement_position")
        or application.get("matched_position")
        or application.get("detected_position")
    )
    requested_role = classification.get("detected_position")
    if not application_role or not requested_role:
        return True
    return roles_are_compatible(application_role, requested_role, thread_context)


ROLE_FAMILY_KEYWORDS = {
    "hr": {
        "hr",
        "human",
        "resource",
        "resources",
        "recruiter",
        "recruitment",
        "talent",
        "onboarding",
        "employee",
        "payroll",
    },
    "design": {
        "ui",
        "ux",
        "designer",
        "design",
        "figma",
        "wireframe",
        "prototype",
        "usability",
        "interface",
    },
    "software": {
        "python",
        "java",
        "javascript",
        "developer",
        "engineer",
        "backend",
        "frontend",
        "fullstack",
        "django",
        "fastapi",
        "flask",
    },
    "accounting": {
        "accountant",
        "accounting",
        "bookkeeping",
        "bookkeeper",
        "gst",
        "tds",
        "tally",
        "ledger",
        "reconciliation",
        "payable",
        "payables",
        "receivable",
        "receivables",
        "invoice",
        "invoicing",
        "quickbooks",
        "xero",
        "journal",
        "audit",
    },
    "tax": {
        "tax",
        "taxation",
        "irs",
        "1040",
        "1065",
        "1120",
        "k1",
        "preparer",
    },
    "sales": {
        "sales",
        "selling",
        # "business" on its own is not a sales signal: HR Business Partner,
        # Business Analyst and Business Operations are all something else, and
        # it also made the requirement "Business Development Executive" look
        # adjacent to any candidate whose CV used the word. The phrase is
        # matched instead, in ROLE_FAMILY_PHRASES.
        "bd",
        "revenue",
        "prospecting",
        "pipeline",
        "crm",
        "quota",
        "outreach",
        "telesales",
        "telecalling",
    },
    "it_support": {
        "desktop",
        "helpdesk",
        "gpo",
        "troubleshooting",
        "hardware",
    },
}

# Suffixes stripped before family lookup so that bookkeeper/bookkeeping,
# payable/payables and designer/design collapse onto the same key. The previous
# exact-match lookup meant "US Bookkeeper" matched no family at all, which
# silently skipped the family check and rejected every bookkeeping CV.
ROLE_TOKEN_SUFFIXES = ("ers", "er", "ing", "ors", "or", "ists", "ist", "s")


def stem_role_token(token: str) -> str:
    lowered = (token or "").lower()
    for suffix in ROLE_TOKEN_SUFFIXES:
        if len(lowered) > len(suffix) + 3 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def stem_role_tokens(value: str | None) -> set[str]:
    return {stem_role_token(token) for token in position_tokens(value)}


STEMMED_ROLE_FAMILIES = {
    family: {stem_role_token(word) for word in words}
    for family, words in ROLE_FAMILY_KEYWORDS.items()
}


# Phrases carry a field where the separate words do not.
ROLE_FAMILY_PHRASES = {
    "sales": {"business development", "inside sales", "field sales", "lead generation"},
    "hr": {"human resource", "talent acquisition", "people operations"},
    "accounting": {"accounts payable", "accounts receivable", "book keeping", "general ledger"},
    "it_support": {"help desk", "service desk", "desktop support"},
}


def role_families_from_text(value: str | None) -> set[str]:
    tokens = stem_role_tokens(value)
    families = {family for family, keywords in STEMMED_ROLE_FAMILIES.items() if tokens & keywords}
    text = normalize_position_text(value) or ""
    if text:
        for family, phrases in ROLE_FAMILY_PHRASES.items():
            if any(phrase in text for phrase in phrases):
                families.add(family)
    return families


def candidate_role_families(
    cv_role_summary: dict[str, Any] | None,
    extracted: dict[str, Any] | None = None,
    cv_text: str = "",
) -> set[str]:
    """Everything we can infer about which field this candidate works in."""
    summary = cv_role_summary or {}
    families = set()
    declared = str(summary.get("role_family") or "").strip().lower()
    if declared:
        families.add(declared)
    # Titles only, for the same reason requirement_role_families takes the title
    # and not the JD. Scanning CV prose read "payroll", "employees" and "client"
    # out of an ordinary accounting CV and returned {accounting, hr, sales}, so
    # every accountant looked adjacent to Business Development Executive. That
    # inflation both fired the near-miss handoff and stopped
    # single_family_requirement finding its one unambiguous match.
    families |= role_families_from_text(
        " ".join(
            str(value)
            for value in [
                summary.get("primary_role"),
                (extracted or {}).get("target_position"),
                (extracted or {}).get("current_title"),
            ]
            if value
        )
    )
    return families


def single_family_requirement(
    requirements: list[dict[str, Any]],
    cv_role_summary: dict[str, Any] | None,
    extracted: dict[str, Any] | None = None,
    cv_text: str = "",
) -> dict[str, Any] | None:
    """The one open role in this candidate's field, when there is exactly one.

    Job titles in accounting barely overlap as strings - "Senior Accountant",
    "Accounts Executive" and "Sr. Accounts Associate" share no token with
    "US Bookkeeper" - so title matching alone sent every one of them to a human.
    When the field is unambiguous, evaluate the CV against that JD and let the
    JD score decide. That produces either screening questions or an explained
    rejection, instead of asking HR to assign the requirement by hand.
    """
    families = candidate_role_families(cv_role_summary, extracted, cv_text)
    if not families:
        return None

    # The field the CV summary actually declared outranks anything inferred from
    # loose keywords: "Sr. US Accountant & Payroll Executive" reads as both
    # accounting and HR, and only the first of those is what they do.
    declared = str((cv_role_summary or {}).get("role_family") or "").strip().lower()
    for candidate_families in ({declared} if declared else set(), families):
        if not candidate_families:
            continue
        matches = [
            requirement
            for requirement in requirements or []
            if requirement_role_families(requirement) & candidate_families
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def best_requirement_by_score(
    ai: Any,
    requirements: list[dict[str, Any]],
    cv_role_summary: dict[str, Any] | None,
    extracted: dict[str, Any],
    cv_text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """Score the CV against the plausible open roles and pick the best one.

    Titles are a poor way to decide whether a CV fits a job: "Sr. Accountant"
    and "US Bookkeeper" share no word, and "Assistant Manager" yields no role
    family at all, so both went to a human. The JD score already answers the
    question and the thresholds already route the answer, so ask it rather than
    asking HR.

    Returns (requirement, its evaluation, every evaluation tried). The caller
    decides; a role is only returned when it clears the screening bar.
    """
    shortlist = near_miss_requirements(requirements, cv_role_summary, extracted, cv_text)
    if not shortlist:
        # No family agreed, which with a seven-word vocabulary means "unknown"
        # far more often than "unrelated". Score against what is open instead.
        shortlist = list(requirements or [])
    shortlist = shortlist[:REQUIREMENT_SCORING_MAX]

    scored: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_evaluation: dict[str, Any] | None = None
    best_score = -1.0
    for requirement in shortlist:
        try:
            evaluation = ai.evaluate_cv(cv_text, extracted, requirement)
        except Exception as exc:  # one bad call must not lose the others
            log_json(
                logging.WARNING,
                "requirement_scoring_failed",
                requirement=requirement.get("position_title"),
                error=str(exc),
            )
            continue
        score = score_number(evaluation.get("jd_match_score"))
        scored.append(
            {
                "requirement_id": requirement.get("id"),
                "position_title": requirement.get("position_title"),
                "ats_score": evaluation.get("ats_score"),
                "jd_match_score": evaluation.get("jd_match_score"),
                "passes": passes_screening_threshold(evaluation, requirement, extracted),
            }
        )
        if score is not None and score > best_score:
            best, best_evaluation, best_score = requirement, evaluation, score

    if best and passes_screening_threshold(best_evaluation or {}, best, extracted):
        return best, best_evaluation, scored
    return None, best_evaluation, scored


def near_miss_requirements(
    requirements: list[dict[str, Any]],
    cv_role_summary: dict[str, Any] | None,
    extracted: dict[str, Any] | None = None,
    cv_text: str = "",
) -> list[dict[str, Any]]:
    """Open requirements in the same domain as the candidate, when nothing matched.

    A candidate whose role family lines up with an open role but whose title does
    not is a near miss, not a rejection. Sending "we have no openings" to a
    ten-year accounting professional because "accountant" and "bookkeeper" share
    no tokens is the expensive kind of mistake, so these go to a human instead.
    """
    if not requirements:
        return []

    candidate_families = candidate_role_families(cv_role_summary, extracted, cv_text)
    if not candidate_families:
        return []

    return [
        requirement
        for requirement in requirements
        if requirement_role_families(requirement) & candidate_families
    ]


def requirement_role_families(requirement: dict[str, Any] | None) -> set[str]:
    """Role families implied by the requirement row itself.

    Role knowledge belongs in the requirement HR created, not in this module.
    Title and job description are both consulted so a newly added opening works
    without touching the code.
    """
    if not requirement:
        return set()
    # Title only. A job description is prose that mentions payroll, clients and
    # talent in passing, and deriving the field from it made unrelated roles
    # look adjacent. No family simply means "unknown", which is handled safely
    # by every caller.
    return role_families_from_text(requirement.get("position_title"))


def roles_are_compatible(requested_role: str | None, cv_role: str | None, cv_text: str = "") -> bool:
    requested_text = normalize_position_text(requested_role)
    if not requested_text:
        return True

    cv_role_text = normalize_position_text(cv_role)
    cv_search_text = normalize_position_text(f"{cv_role or ''} {cv_text[:4000]}")
    # Generic words like "us", "senior", "full" and "cycle" must not count toward
    # the overlap ratio; "US Bookkeeper" would otherwise be half-satisfied by the
    # letter pair "us" appearing anywhere in the CV.
    requested_words = meaningful_role_tokens(requested_text)
    if not requested_words:
        return True

    if requested_text in cv_search_text:
        return True
    if cv_role_text and cv_role_text in requested_text:
        return True

    requested_families = role_families_from_text(requested_text)
    cv_families = role_families_from_text(cv_search_text)
    if requested_families and requested_families & cv_families:
        return True

    cv_stems = stem_role_tokens(cv_search_text)
    overlap = {word for word in requested_words if stem_role_token(word) in cv_stems}
    return len(overlap) / len(requested_words) >= 0.5


def extract_docx_text(path: Path) -> str:
    try:
        return _extract_docx_text(path)
    except Exception as exc:
        log_json(logging.WARNING, "docx_extract_failed", error=str(exc)[:300])
        return ""


def _extract_docx_text(path: Path) -> str:
    paragraphs = []
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for node in root.findall(".//w:t", namespace):
        if node.text:
            paragraphs.append(node.text)
    return " ".join(paragraphs)


# Scanned CVs are common - people photograph or scan a printout - and they carry
# no text layer at all. OCR is attempted only after the cheap paths fail, is
# capped, and degrades to "unreadable" if the tooling is not installed.
OCR_ENABLED = os.getenv("RECRUITER_OCR_ENABLED", "true").strip().lower() not in {"false", "0", "no"}
# Ceiling for the adaptive render: past this the page is slower to OCR without
# reading any better.
OCR_MAX_DPI = int(os.getenv("RECRUITER_OCR_MAX_DPI", "400"))
OCR_MAX_PAGES = int(os.getenv("RECRUITER_OCR_MAX_PAGES", "5"))
OCR_DPI = int(os.getenv("RECRUITER_OCR_DPI", "200"))
_OCR_UNAVAILABLE_LOGGED = False


def extract_pdf_text_pypdf(path: Path) -> str:
    try:
        pypdf = require_package("pypdf", "./venv/bin/python -m pip install pypdf")
    except RuntimeError:
        pypdf = require_package("PyPDF2", "./venv/bin/python -m pip install pypdf")
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        # Corrupt or truncated files used to raise straight out of process_email,
        # which aborted the whole message and left it unread and unanswered.
        # An unreadable CV is a normal outcome, not a crash.
        log_json(logging.WARNING, "pdf_open_failed", error=str(exc)[:300])
        return ""
    pages = []
    for index, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:
            log_json(logging.WARNING, "pdf_page_extract_failed", page=index, error=str(exc)[:200])
    return "\n".join(pages).strip()


def extract_pdf_text_pymupdf(path: Path) -> str:
    """Second opinion on the text layer; reads some files pypdf cannot."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # older name
        except ImportError:
            return ""
    try:
        with pymupdf.open(str(path)) as document:
            return "\n".join(page.get_text() or "" for page in document).strip()
    except Exception as exc:
        log_json(logging.WARNING, "pymupdf_extract_failed", error=str(exc)[:200])
        return ""


def ocr_dpi_for_page(page: Any) -> int:
    """Render at least as finely as the scan already is.

    A scanned CV is one full-page JPEG. Rendering it at a fixed 200 dpi threw
    away detail the file already had - Kanika_Sr.BDM.pdf carries 1153x1612 per
    page and was being handed to Tesseract at 800x1200 - and OCR accuracy falls
    off quickly once characters get small.
    """
    try:
        width_pt = float(page.rect.width) or 0.0
        if width_pt <= 0:
            return OCR_DPI
        native_width = max(
            (int(image[2]) for image in page.get_images(full=True) if len(image) > 2),
            default=0,
        )
        if native_width <= 0:
            return OCR_DPI
        needed = int(native_width * 72.0 / width_pt)
        return max(OCR_DPI, min(needed, OCR_MAX_DPI))
    except Exception:
        return OCR_DPI


def ocr_pdf_text(path: Path) -> str:
    """Read a scanned CV by rendering its pages and running OCR.

    Optional: without PyMuPDF and Tesseract installed this returns nothing and
    the caller falls back to telling the candidate the file was unreadable.
    """
    global _OCR_UNAVAILABLE_LOGGED
    if not OCR_ENABLED:
        return ""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        if not _OCR_UNAVAILABLE_LOGGED:
            _OCR_UNAVAILABLE_LOGGED = True
            log_json(
                logging.WARNING,
                "ocr_unavailable",
                error=str(exc),
                hint="sudo apt-get install -y tesseract-ocr && "
                     "./venv/bin/python -m pip install pymupdf pytesseract pillow",
            )
        return ""

    started = time.monotonic()
    pages_text = []
    try:
        with pymupdf.open(str(path)) as document:
            for index, page in enumerate(document):
                if index >= OCR_MAX_PAGES:
                    break
                pixmap = page.get_pixmap(dpi=ocr_dpi_for_page(page))
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                pages_text.append(pytesseract.image_to_string(image) or "")
    except Exception as exc:
        log_json(logging.WARNING, "ocr_failed", error=str(exc)[:300])
        return ""

    text = "\n".join(pages_text).strip()
    log_json(
        logging.INFO,
        "ocr_completed",
        pages=len(pages_text),
        chars=len(text),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    return text


def extract_pdf_text(path: Path) -> str:
    text = extract_pdf_text_pypdf(path)
    if text:
        return text
    text = extract_pdf_text_pymupdf(path)
    if text:
        log_json(logging.INFO, "pdf_text_recovered_by_pymupdf", chars=len(text))
        return text
    return ocr_pdf_text(path)


def sanitize_db_text(value: Any) -> str:
    """Strip bytes PostgreSQL refuses in a text column.

    Some PDFs yield NUL and other C0 control characters. Inserting them raises
    DataError mid-flight, which aborts processing after replies have already been
    sent and leaves the message to be retried and re-answered.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def line_is_letter_spaced(line: str) -> bool:
    """Heuristic: has this line been split into individual glyphs?"""
    tokens = line.split()
    if len(tokens) < 4:
        return False
    single = sum(1 for token in tokens if len(token) == 1)
    return single / len(tokens) >= 0.6


def repair_letter_spaced_text(text: str) -> str:
    """Rejoin text that a PDF exported one character at a time.

    Some CVs are produced by tools that position every glyph individually, and
    pypdf then yields "S U M M A R Y" and "D e v c o n s  S o f t w a r e".
    Every word in such a CV is invisible to matching, scoring and the LLM: the
    candidate's own job title cannot be found because it is not there as a word.
    Word boundaries survive as runs of two or more spaces, which is what makes
    the repair possible.
    """
    if not text:
        return text
    repaired_lines = []
    for line in text.splitlines():
        if line_is_letter_spaced(line):
            words = ["".join(part.split()) for part in re.split(r"\s{2,}", line.strip())]
            repaired_lines.append(" ".join(word for word in words if word))
        else:
            repaired_lines.append(line)
    return "\n".join(repaired_lines)


def clean_cv_text(text: str) -> str:
    """Everything a freshly extracted CV must go through before it is used."""
    return sanitize_db_text(repair_letter_spaced_text(text))


def sniff_cv_format(payload: bytes) -> str | None:
    """What the file actually is, regardless of what it is called.

    Candidates send a PDF named .doc, a .docx named .doc, and Word's "save as"
    leaves RTF behind a .doc extension. Trusting the extension meant those were
    read with the wrong parser or, for .doc, with no parser at all.
    """
    head = (payload or b"")[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "docx"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    return None


def extract_rtf_text(payload: bytes) -> str:
    """Enough RTF handling to recover the words of a CV."""
    text = payload.decode("latin-1", errors="ignore")
    # Font and colour tables are metadata, not the CV.
    text = re.sub(r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|pict)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
                  " ", text)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    return re.sub(r"[{}]", " ", text)


def extract_legacy_doc_text(path: Path) -> str:
    """Legacy binary .doc, via whichever converter the host happens to have.

    .doc was accepted as a CV but had no branch in extract_cv_text at all, so
    every one of them read as empty and the candidate was told their CV could
    not be read.
    """
    for command in (
        ["antiword", str(path)],
        ["catdoc", str(path)],
        ["libreoffice", "--headless", "--cat", str(path)],
    ):
        try:
            result = subprocess.run(command, capture_output=True, timeout=60)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        text = (result.stdout or b"").decode("utf-8", errors="ignore").strip()
        if text:
            return text
    log_json(
        logging.WARNING,
        "legacy_doc_converter_unavailable",
        hint="sudo apt-get install -y antiword",
    )
    return ""


def extract_cv_text(filename: str, payload: bytes) -> str:
    suffix = Path(filename.lower()).suffix
    by_extension = {".txt": "txt", ".docx": "docx", ".pdf": "pdf", ".doc": "doc"}.get(suffix)
    # Content wins. PDF, DOCX, RTF and legacy DOC all carry a signature; only
    # plain text has none, and that is the fallback at the end anyway. A .doc
    # with no OLE2 header is not a Word document whatever it is called, so it
    # must not be sent to the Word converter.
    kind = sniff_cv_format(payload)
    if kind and by_extension and kind != by_extension:
        log_json(logging.INFO, "cv_format_differs_from_extension",
                 filename=filename, named=by_extension, detected=kind)

    with TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(payload)

        if kind == "txt":
            return clean_cv_text(payload.decode("utf-8", errors="ignore"))
        if kind == "docx":
            return clean_cv_text(extract_docx_text(path))
        if kind == "pdf":
            return clean_cv_text(extract_pdf_text(path))
        if kind == "rtf":
            return clean_cv_text(extract_rtf_text(payload))
        if kind == "doc":
            return clean_cv_text(extract_legacy_doc_text(path))
        # Unknown container: a CV is mostly text, so try that before giving up.
        return clean_cv_text(payload.decode("utf-8", errors="ignore"))


def safe_cv_storage_filename(filename: str) -> str:
    display_name = attachment_display_name(filename) or "cv"
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", display_name).strip(" ._")
    if not safe_name:
        safe_name = "cv"
    return safe_name[:180]


def safe_onedrive_path_part(value: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return (safe_name or "recording")[:120]


def save_cv_attachment_file(application_id: int, filename: str, payload: bytes) -> str:
    storage_root = Path(CV_STORAGE_DIR)
    if not storage_root.is_absolute():
        storage_root = Path.cwd() / storage_root

    storage_dir = storage_root / f"application-{application_id}"
    storage_dir.mkdir(parents=True, exist_ok=True)

    safe_name = safe_cv_storage_filename(filename)
    content_hash = hashlib.sha256(payload).hexdigest()[:16]
    target = storage_dir / f"{content_hash}-{safe_name}"
    target.write_bytes(payload)

    try:
        return str(target.relative_to(Path.cwd()))
    except ValueError:
        return str(target)


def split_sql_statements(script: str) -> list[str]:
    """Split a DDL script on statement boundaries.

    Splitting on every ";" breaks two things: a semicolon inside a `--` comment,
    and the body of a dollar-quoted function, which is full of them.
    """
    statements = []
    buffer = []
    dollar_tag = None
    for raw_line in script.splitlines():
        line = raw_line
        if dollar_tag is None:
            without_comment = re.sub(r"--.*$", "", line)
        else:
            without_comment = line

        search_from = 0
        while True:
            match = re.search(r"\$[A-Za-z_]*\$", without_comment[search_from:])
            if not match:
                break
            tag = match.group(0)
            search_from += match.end()
            if dollar_tag is None:
                dollar_tag = tag
            elif tag == dollar_tag:
                dollar_tag = None

        buffer.append(without_comment)
        if dollar_tag is None and without_comment.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            buffer = []

    tail = "\n".join(buffer).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


class RecruiterDatabase:
    """PostgreSQL-backed store for the recruiter agent.

    The SQL Server path was removed: it required rewriting every query by string
    substitution (%s -> ?, NOW() -> SYSDATETIMEOFFSET(), LIMIT -> TOP), which
    forced all SQL to the lowest common denominator and had already drifted out
    of sync with the migrations.
    """

    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is required.")
        psycopg = require_package("psycopg", "./venv/bin/python -m pip install psycopg[binary]")
        self.psycopg = psycopg
        self.conn = psycopg.connect(DATABASE_URL)
        self.provider = "postgres"

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def rollback(self):
        """Return the connection to a usable state after a failed statement.

        PostgreSQL aborts the whole transaction on any error, and every later
        statement then fails with InFailedSqlTransaction. Without this, a single
        bad insert takes out the error logging and the cleanup that runs after
        it, turning one recoverable failure into a silent cascade.
        """
        try:
            self.conn.rollback()
        except Exception as exc:
            LOGGER.warning("Database rollback failed: %s", exc)

    def rows(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        try:
            with self.conn.cursor(row_factory=self.psycopg.rows.dict_row) as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
        except Exception:
            self.rollback()
            raise

    def one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.rows(query, params)
        return rows[0] if rows else None

    def execute(self, query: str, params: tuple = ()):
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(query, params)
            self.conn.commit()
        except Exception:
            self.rollback()
            raise

    def init_schema(self):
        with self.conn.cursor() as cursor:
            for statement in split_sql_statements(CREATE_TABLES_SQL):
                cursor.execute(statement)
        self.conn.commit()

    def log_email_event(self, inbox_email: InboxEmail, event_type: str, details: dict[str, Any]):
        self.execute(
            """
            INSERT INTO recruiter_email_events
            (email_message_id, source_email, email_subject, event_type, details)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (
                inbox_email.message_id,
                inbox_email.sender,
                inbox_email.subject,
                event_type,
                json.dumps(details, default=str),
            ),
        )

    def recent_event_count(self, source_email: str, event_types: list[str], hours: int = 48) -> int:
        source = clean_email(source_email)
        if not source or not event_types:
            return 0
        placeholders = ", ".join(["%s"] * len(event_types))
        row = self.one(
            f"""
            SELECT COUNT(*) AS count
            FROM recruiter_email_events
            WHERE LOWER(COALESCE(source_email, '')) = %s
              AND event_type IN ({placeholders})
              AND created_at >= %s
            """,
            (source, *event_types, datetime.now(timezone.utc) - timedelta(hours=hours)),
        )
        return int(row["count"] or 0) if row else 0

    def claim_provider_message(self, provider_message_id: str) -> bool:
        """Atomically claim an inbound message. False means it was already handled.

        This is the guard against duplicate Graph notifications; it must not be
        replaced by an in-process lock, which does not survive a restart.
        """
        message_id = (provider_message_id or "").strip()
        if not message_id:
            return True
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO recruiter_processed_messages (provider_message_id)
                    VALUES (%s)
                    ON CONFLICT (provider_message_id) DO UPDATE
                        SET processed_at = NOW()
                        WHERE recruiter_processed_messages.processed_at
                              < NOW() - make_interval(mins => %s)
                    RETURNING provider_message_id
                    """,
                    (message_id, MESSAGE_CLAIM_STALE_MINUTES),
                )
                claimed = cursor.fetchone() is not None
            # Committed immediately so the claim is durable and independent of
            # whatever the rest of processing does. Failures release it again.
            self.conn.commit()
            return claimed
        except Exception:
            self.rollback()
            raise

    def release_provider_message(self, provider_message_id: str):
        """Undo a claim so a genuinely failed message can be retried."""
        message_id = (provider_message_id or "").strip()
        if not message_id:
            return
        self.execute(
            "DELETE FROM recruiter_processed_messages WHERE provider_message_id = %s",
            (message_id,),
        )

    def record_sent_reply(
        self,
        application_id: int | None,
        recipient: str,
        scenario: str,
        body: str,
        provider_message_id: str | None = None,
    ):
        self.execute(
            """
            INSERT INTO recruiter_sent_replies
            (application_id, recipient, scenario, body_hash, provider_message_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                application_id,
                clean_email(recipient) or (recipient or ""),
                scenario,
                hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
                provider_message_id,
            ),
        )

    def sent_reply_count(
        self,
        application_id: int | None,
        recipient: str,
        scenario: str,
        hours: int = REPLY_SCENARIO_COOLDOWN_HOURS,
    ) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        if application_id:
            row = self.one(
                """
                SELECT COUNT(*) AS count FROM recruiter_sent_replies
                WHERE application_id = %s AND scenario = %s AND sent_at >= %s
                """,
                (application_id, scenario, since),
            )
        else:
            row = self.one(
                """
                SELECT COUNT(*) AS count FROM recruiter_sent_replies
                WHERE recipient = %s AND scenario = %s AND sent_at >= %s
                """,
                (clean_email(recipient) or (recipient or ""), scenario, since),
            )
        return int(row["count"] or 0) if row else 0

    def scenario_ever_sent(self, application_id: int | None, recipient: str, scenario: str) -> bool:
        """Has this scenario ever been sent for this application?

        Used for genuinely once-per-application messages such as the budget
        disclosure, where a 24h cooldown is not strong enough.
        """
        if application_id:
            row = self.one(
                "SELECT 1 AS hit FROM recruiter_sent_replies WHERE application_id = %s AND scenario = %s LIMIT 1",
                (application_id, scenario),
            )
        else:
            row = self.one(
                "SELECT 1 AS hit FROM recruiter_sent_replies WHERE recipient = %s AND scenario = %s LIMIT 1",
                (clean_email(recipient) or (recipient or ""), scenario),
            )
        return row is not None

    def agent_sent_message_ids(self, limit: int = 500) -> set[str]:
        rows = self.rows(
            """
            SELECT provider_message_id FROM recruiter_sent_replies
            WHERE provider_message_id IS NOT NULL
            ORDER BY sent_at DESC LIMIT %s
            """,
            (limit,),
        )
        return {str(row["provider_message_id"]) for row in rows if row.get("provider_message_id")}

    def mark_human_handled(self, application_id: int, reason: str):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = 'human_handled',
                human_handled_at = NOW(),
                hr_escalation_reason = COALESCE(hr_escalation_reason, %s)
            WHERE id = %s
            """,
            (reason, application_id),
        )

    def save_interview_session(self, application_id: int, session: dict[str, Any]):
        """Persist enough of the live session to resume after a restart."""
        snapshot = {
            key: session.get(key)
            for key in (
                "questions",
                "current_index",
                "current_question",
                "transcript",
                "followup_for_current",
                "last_client_turn_id",
                "role_context",
                "started_at",
            )
        }
        self.execute(
            "UPDATE recruiter_applications SET interview_session = %s::jsonb WHERE id = %s",
            (json.dumps(snapshot, default=str), application_id),
        )

    def load_interview_session(self, application_id: int) -> dict[str, Any]:
        row = self.one(
            "SELECT interview_session FROM recruiter_applications WHERE id = %s",
            (application_id,),
        )
        return json_dict(row.get("interview_session")) if row else {}

    def clear_interview_session(self, application_id: int):
        self.execute(
            "UPDATE recruiter_applications SET interview_session = '{}'::jsonb WHERE id = %s",
            (application_id,),
        )

    def bump_interview_attempts(self, application_id: int) -> int:
        """Increment and return the attempt count, committed immediately.

        rows()/one() deliberately do not commit, so an UPDATE ... RETURNING run
        through them is rolled back when the connection closes. Every request
        opens its own connection, so the counter always read back as 1 and the
        cap could never fire.
        """
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE recruiter_applications
                    SET interview_attempts = COALESCE(interview_attempts, 0) + 1
                    WHERE id = %s
                    RETURNING interview_attempts
                    """,
                    (application_id,),
                )
                row = cursor.fetchone()
            self.conn.commit()
            return int(row[0]) if row else 1
        except Exception:
            self.rollback()
            raise

    def mark_interview_link_sent(self, application_id: int):
        """Explicit, idempotent transition for 'the candidate has the link'.

        Previously this only happened as a side effect of minting a new token
        inside ensure_interview_link(), so re-sending an existing link left the
        status untouched and the candidate stuck in a resend loop.
        """
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = 'interview_link_sent',
                interview_link_created_at = COALESCE(interview_link_created_at, NOW())
            WHERE id = %s
              AND LOWER(COALESCE(application_status, '')) NOT IN
                  ('interview_started', 'interview_completed', 'interview_on_hold_hr_review',
                   'interview_rejected', 'interview_scheduled', 'human_handled')
            """,
            (application_id,),
        )

    def mark_budget_disclosed(self, application_id: int, screening_details: dict[str, Any]):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = 'budget_disclosed',
                screening_details = %s::jsonb,
                budget_disclosed_at = COALESCE(budget_disclosed_at, NOW())
            WHERE id = %s
            """,
            (json.dumps(screening_details, default=str), application_id),
        )

    def record_budget_response(self, application_id: int, response: str, screening_details: dict[str, Any]):
        self.execute(
            """
            UPDATE recruiter_applications
            SET budget_response = %s,
                screening_details = %s::jsonb
            WHERE id = %s
            """,
            (response, json.dumps(screening_details, default=str), application_id),
        )

    def open_application_for_candidate(self, candidate_email: str, requirement_id: int | None) -> dict[str, Any] | None:
        """Find an existing live application for this person and role.

        Without this, a candidate who starts a second email thread for the same
        opening gets a brand new application with no memory of the first, and is
        asked for a CV they already sent.
        """
        email_address = clean_email(candidate_email)
        if not email_address:
            return None
        closed = tuple(FINAL_AGENT_STATUSES | {"human_handled"})
        placeholders = ", ".join(["%s"] * len(closed))
        params: list[Any] = [email_address, email_address]
        requirement_clause = ""
        if requirement_id:
            requirement_clause = "AND ra.requirement_id = %s"
            params.append(requirement_id)
        params.extend(closed)
        return self.one(
            f"""
            SELECT ra.*, rr.position_title AS requirement_position
            FROM recruiter_applications ra
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE (LOWER(COALESCE(ra.candidate_email, '')) = %s
                   OR LOWER(COALESCE(ra.source_email, '')) = %s)
              {requirement_clause}
              AND LOWER(COALESCE(ra.application_status, '')) NOT IN ({placeholders})
            ORDER BY ra.created_at DESC
            LIMIT 1
            """,
            tuple(params),
        )

    def mark_manual_hr_review(self, application_id: int, reason: str):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                hr_escalation_reason = %s,
                hr_escalated_at = NOW()
            WHERE id = %s
            """,
            ("manual_hr_review", reason, application_id),
        )

    def open_requirements(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT *
            FROM recruitment_requirements
            WHERE LOWER(TRIM(status)) = 'open'
            ORDER BY urgently_required DESC, created_at DESC
            LIMIT 50
            """
        )

    def latest_application_for_email(self, email_address: str) -> dict[str, Any] | None:
        sender = (email_address or "").lower()
        if not sender:
            return None
        return self.one(
            """
            SELECT
                ra.*,
                rr.position_title AS requirement_position
            FROM recruiter_applications ra
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE
                LOWER(COALESCE(ra.source_email, '')) = %s
                OR LOWER(COALESCE(ra.candidate_email, '')) = %s
                OR LOWER(COALESCE(ra.referrer_email, '')) = %s
            ORDER BY ra.created_at DESC
            LIMIT 1
            """,
            (sender, sender, sender),
        )

    def latest_application_for_thread(self, inbox_email: InboxEmail) -> dict[str, Any] | None:
        conditions = []
        params = []

        thread_id = str(inbox_email.gmail_thread_id or "").strip()
        if thread_id:
            conditions.append("COALESCE(ra.email_thread_id, '') = %s")
            params.append(thread_id)

        message_ids = thread_message_ids(inbox_email)
        if message_ids:
            placeholders = ", ".join(["%s"] * len(message_ids))
            conditions.append(f"ra.email_message_id IN ({placeholders})")
            params.extend(message_ids)

        if not conditions:
            return None

        return self.one(
            f"""
            SELECT
                ra.*,
                rr.position_title AS requirement_position
            FROM recruiter_applications ra
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE {' OR '.join(conditions)}
            ORDER BY ra.created_at DESC
            LIMIT 1
            """,
            tuple(params),
        )

    def latest_application_for_inbox_email(self, inbox_email: InboxEmail) -> dict[str, Any] | None:
        return self.latest_application_for_thread(inbox_email) or self.latest_application_for_email(inbox_email.sender)

    def application_with_requirement(self, application_id: int) -> dict[str, Any] | None:
        return self.one(
            """
            SELECT
                ra.*,
                rc.full_name,
                rc.phone,
                rc.location AS candidate_profile_location,
                rc.raw_cv_text,
                rc.cv_summary,
                rc.ai_evaluation AS candidate_ai_evaluation,
                rr.position_title AS requirement_position,
                rr.budget_min,
                rr.budget_max,
                rr.currency,
                rr.needed_within_days,
                rr.job_description,
                rr.recommended_questions
            FROM recruiter_applications ra
            JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE ra.id = %s
            """,
            (application_id,),
        )

    def update_interview_report(self, application_id: int, report: dict[str, Any]):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                interview_report = %s::jsonb,
                interview_completed_at = NOW()
            WHERE id = %s
            """,
            ("interview_completed", json.dumps(report, default=str), application_id),
        )

    def mark_post_interview_outcome(self, application_id: int, status: str, reason: str | None = None):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                hr_escalation_reason = %s,
                hr_escalated_at = CASE WHEN %s = 'interview_on_hold_hr_review' THEN NOW() ELSE hr_escalated_at END
            WHERE id = %s
            """,
            (status, reason, status, application_id),
        )

    def scheduled_hr_rounds_for_decision_check(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT id, interview_scheduled_at
            FROM recruiter_applications
            WHERE application_status = %s
              AND interview_scheduled_at IS NOT NULL
            """,
            ("interview_scheduled",),
        )

    def ensure_interview_link(self, application_id: int, reset: bool = False) -> str:
        application = self.one(
            "SELECT interview_link_token FROM recruiter_applications WHERE id = %s",
            (application_id,),
        )
        token = application.get("interview_link_token") if application else None
        if token and not reset:
            return token
        token = uuid4().hex
        self.execute(
            """
            UPDATE recruiter_applications
            SET interview_link_token = %s,
                interview_link_created_at = NOW(),
                interview_started_at = NULL,
                interview_completed_at = NULL,
                interview_reminder_sent_at = NULL,
                interview_reminder_count = 0,
                interview_report = '{}'::jsonb,
                application_status = %s
            WHERE id = %s
            """,
            (token, "interview_link_sent", application_id),
        )
        return token

    def application_by_interview_token(self, token: str) -> dict[str, Any] | None:
        return self.one(
            """
            SELECT
                ra.*,
                rc.full_name,
                rc.phone,
                rc.location AS candidate_profile_location,
                rc.raw_cv_text,
                rc.cv_summary,
                rc.ai_evaluation AS candidate_ai_evaluation,
                rr.position_title AS requirement_position,
                rr.budget_min,
                rr.budget_max,
                rr.currency,
                rr.needed_within_days,
                rr.job_description,
                rr.recommended_questions
            FROM recruiter_applications ra
            JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE ra.interview_link_token = %s
            """,
            (token,),
        )

    def mark_interview_started(self, application_id: int):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                interview_started_at = COALESCE(interview_started_at, NOW())
            WHERE id = %s
            """,
            ("interview_started", application_id),
        )

    def pending_interview_reminders(
        self,
        link_cutoff: datetime,
        reminder_cutoff: datetime,
        max_reminders: int = 1,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT
                ra.*,
                rc.full_name,
                rr.position_title AS requirement_position
            FROM recruiter_applications ra
            JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE ra.interview_link_token IS NOT NULL
              AND ra.interview_link_created_at IS NOT NULL
              AND ra.interview_link_created_at <= %s
              AND ra.interview_completed_at IS NULL
              AND COALESCE(ra.interview_reminder_count, 0) < %s
              AND (
                    ra.interview_reminder_sent_at IS NULL
                    OR ra.interview_reminder_sent_at <= %s
                  )
              AND LOWER(COALESCE(ra.application_status, '')) IN ('interview_link_sent', 'interview_started')
            ORDER BY ra.interview_link_created_at ASC
            LIMIT %s
            """,
            (link_cutoff, max_reminders, reminder_cutoff, limit),
        )

    def mark_interview_reminder_sent(self, application_id: int):
        self.execute(
            """
            UPDATE recruiter_applications
            SET interview_reminder_sent_at = NOW(),
                interview_reminder_count = COALESCE(interview_reminder_count, 0) + 1
            WHERE id = %s
            """,
            (application_id,),
        )

    def mark_interview_reminder_handled(self, application_id: int):
        self.execute(
            """
            UPDATE recruiter_applications
            SET interview_reminder_sent_at = NOW(),
                interview_reminder_count = CASE
                    WHEN COALESCE(interview_reminder_count, 0) < 1 THEN 1
                    ELSE interview_reminder_count
                END
            WHERE id = %s
            """,
            (application_id,),
        )

    def update_application_screening(
        self,
        application_id: int,
        status: str,
        screening_details: dict[str, Any] | None = None,
        escalation_reason: str | None = None,
    ):
        details = screening_details or {}
        values = (
            status,
            json.dumps(details, default=str),
            score_number(details.get("current_salary")),
            score_number(details.get("expected_salary")),
            details.get("current_location"),
            joining_days_from_answers(details),
        )
        if escalation_reason:
            self.execute(
                """
                UPDATE recruiter_applications
                SET application_status = %s,
                    screening_details = %s::jsonb,
                    screening_current_salary = %s,
                    screening_expected_salary = %s,
                    screening_current_location = %s,
                    screening_joining_days = %s,
                    hr_escalation_reason = %s,
                    hr_escalated_at = NOW()
                WHERE id = %s
                """,
                (*values, escalation_reason, application_id),
            )
            return
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                screening_details = %s::jsonb,
                screening_current_salary = %s,
                screening_expected_salary = %s,
                screening_current_location = %s,
                screening_joining_days = %s
            WHERE id = %s
            """,
            (*values, application_id),
        )

    def mark_hr_approved_for_interview(self, application_id: int):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = 'interview_time_requested',
                hr_approved_at = NOW()
            WHERE id = %s
            """,
            (application_id,),
        )

    def update_interview_schedule(
        self,
        application_id: int,
        status: str,
        availability: str | None = None,
        scheduled_at: datetime | None = None,
        teams_event_id: str | None = None,
        teams_join_url: str | None = None,
        interviewer_email: str | None = None,
        interviewer_name: str | None = None,
    ):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                interview_availability = %s,
                interview_scheduled_at = %s,
                hr_interviewer_email = %s,
                hr_interviewer_name = %s,
                teams_event_id = %s,
                teams_join_url = %s
            WHERE id = %s
            """,
            (
                status,
                availability,
                scheduled_at,
                interviewer_email,
                interviewer_name,
                teams_event_id,
                teams_join_url,
                application_id,
            ),
        )

    def final_hr_schedule_counts(self, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT
                COALESCE(hr_interviewer_email, '') AS hr_interviewer_email,
                COALESCE(hr_interviewer_name, '') AS hr_interviewer_name,
                COUNT(*) AS meeting_count
            FROM recruiter_applications
            WHERE interview_scheduled_at >= %s
              AND interview_scheduled_at < %s
              AND application_status IN (
                  'interview_scheduled',
                  'final_hr_round_completed_pending_decision',
                  'selected_documents_requested',
                  'rejected_after_hr_round',
                  'hold_after_hr_round'
              )
            GROUP BY COALESCE(hr_interviewer_email, ''), COALESCE(hr_interviewer_name, '')
            """,
            (start_at, end_at),
        )

    def final_hr_scheduled_applications(self, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT
                ra.id,
                ra.application_status,
                ra.interview_scheduled_at,
                ra.hr_interviewer_email,
                ra.hr_interviewer_name,
                ra.teams_join_url,
                rc.full_name,
                COALESCE(rr.position_title, ra.matched_position, ra.detected_position) AS role
            FROM recruiter_applications ra
            JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
            LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
            WHERE ra.interview_scheduled_at >= %s
              AND ra.interview_scheduled_at < %s
              AND ra.application_status IN (
                  'interview_scheduled',
                  'final_hr_round_completed_pending_decision',
                  'selected_documents_requested',
                  'rejected_after_hr_round',
                  'hold_after_hr_round'
              )
            ORDER BY ra.interview_scheduled_at ASC
            """,
            (start_at, end_at),
        )

    def mark_latest_application_withdrawn(self, email_address: str) -> dict[str, Any] | None:
        application = self.latest_application_for_email(email_address)
        if not application:
            return None
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = 'withdrawn'
            WHERE id = %s
            """,
            (application["id"],),
        )
        application["application_status"] = "withdrawn"
        return application

    def insert_candidate(
        self,
        inbox_email: InboxEmail,
        cv_text: str,
        extracted: dict[str, Any],
        evaluation: dict[str, Any],
        submission_type: str,
        candidate_email: str | None,
        referrer_email: str | None,
    ) -> int:
        values = (
            str(uuid4()),
            inbox_email.sender,
            candidate_email,
            referrer_email,
            submission_type,
            extracted.get("full_name"),
            extracted.get("phone"),
            extracted.get("location"),
            extracted.get("linkedin_url"),
            extracted.get("portfolio_url"),
            extracted.get("current_title"),
            extracted.get("current_company"),
            numeric_value(extracted.get("total_experience_years")),
            json.dumps(extracted.get("skills", [])),
            json.dumps(extracted.get("education", [])),
            json.dumps(extracted.get("work_history", [])),
            json.dumps(extracted.get("certifications", [])),
            sanitize_db_text(cv_text),
            evaluation.get("short_description"),
            numeric_value(evaluation.get("ats_score")),
            json.dumps(evaluation, default=str),
        )
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    """
                INSERT INTO recruiter_candidates
                (
                    candidate_uid, source_email, candidate_email, referrer_email,
                    submission_type, full_name, phone, location,
                    linkedin_url, portfolio_url, current_title, current_company,
                    total_experience_years, skills, education, work_history,
                    certifications, raw_cv_text, cv_summary, ats_score, ai_evaluation
                )
                VALUES
                (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s::jsonb
                )
                RETURNING id
                """,
                    values,
                )
                candidate_id = cursor.fetchone()[0]
            self.conn.commit()
            return candidate_id
        except Exception:
            self.rollback()
            raise

    def insert_application(
        self,
        inbox_email: InboxEmail,
        candidate_id: int,
        requirement: dict[str, Any] | None,
        extracted: dict[str, Any],
        evaluation: dict[str, Any],
        attachment_filename: str,
        attachment_sha256: str,
        attachment_payload: bytes | None,
        status: str,
        submission_type: str,
        candidate_email: str | None,
        referrer_email: str | None,
    ) -> int | None:
        values = (
            str(uuid4()),
            candidate_id,
            requirement["id"] if requirement else None,
            inbox_email.message_id,
            inbox_email.gmail_thread_id,
            inbox_email.sender,
            candidate_email,
            referrer_email,
            submission_type,
            inbox_email.subject,
            extracted.get("target_position"),
            requirement["position_title"] if requirement else None,
            status,
            numeric_value(evaluation.get("ats_score")),
            numeric_value(evaluation.get("jd_match_score")),
            json.dumps(evaluation.get("strengths", [])),
            json.dumps(evaluation.get("risks", [])),
            json.dumps(evaluation.get("missing_requirements", [])),
            evaluation.get("short_description"),
            json.dumps(evaluation, default=str),
            attachment_filename,
            attachment_sha256,
            attachment_payload,
            inbox_email.received_at,
        )
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO recruiter_applications
                (
                    application_uid, candidate_id, requirement_id, email_message_id,
                    email_thread_id, source_email, candidate_email, referrer_email, submission_type,
                    email_subject, detected_position, matched_position,
                    application_status, ats_score, jd_match_score, strengths, risks,
                    missing_requirements, ai_short_description, ai_evaluation,
                    attachment_filename, attachment_sha256, attachment_payload, received_at
                )
                VALUES
                (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s::jsonb,
                    %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                values,
            )
            row = cursor.fetchone()
        self.conn.commit()
        if row:
            return row[0]
        existing = self.one(
            """
            SELECT id
            FROM recruiter_applications
            WHERE email_message_id = %s AND attachment_sha256 = %s
            LIMIT 1
            """,
            (inbox_email.message_id, attachment_sha256),
        )
        return existing["id"] if existing else None

    def update_application_attachment_filename(self, application_id: int, attachment_filename: str):
        self.execute(
            """
            UPDATE recruiter_applications
            SET attachment_filename = %s
            WHERE id = %s
            """,
            (attachment_filename, application_id),
        )

    def mark_jd_score_rejected(self, application_id: int, reason: str):
        self.execute(
            """
            UPDATE recruiter_applications
            SET application_status = %s,
                hr_escalation_reason = %s,
                hr_escalated_at = NOW()
            WHERE id = %s
            """,
            ("rejected_jd_score", reason, application_id),
        )


class RecruiterInbox:
    provider_name = "imap"

    def __init__(self):
        missing = [
            name
            for name, value in {
                "RECRUITER_IMAP_HOST": RECRUITER_IMAP_HOST,
                "RECRUITER_EMAIL": RECRUITER_EMAIL,
                "RECRUITER_EMAIL_PASSWORD": RECRUITER_EMAIL_PASSWORD,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing inbox settings: {', '.join(missing)}")

    def fetch_unseen(self, limit: int) -> list[InboxEmail]:
        inbox = imaplib.IMAP4_SSL(RECRUITER_IMAP_HOST, RECRUITER_IMAP_PORT)
        try:
            inbox.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            inbox.select(RECRUITER_MAILBOX)
            _, search_data = inbox.uid("search", None, "UNSEEN")
            uids = search_data[0].split()[:limit]
            messages = []

            for uid in uids:
                fetch_data = self.fetch_message(inbox, uid)
                gmail_thread_id = self.parse_provider_thread_id(fetch_data)
                raw_message = fetch_data[0][1]
                parsed = email.message_from_bytes(raw_message, policy=default)
                thread_messages = self.fetch_thread_messages(inbox, gmail_thread_id, parsed)
                if not thread_messages:
                    thread_messages = [parse_thread_message(uid, parsed)]
                messages.append(
                    InboxEmail(
                        uid=uid,
                        gmail_thread_id=gmail_thread_id,
                        message_id=parsed.get("Message-ID", ""),
                        references=parsed.get("References", ""),
                        in_reply_to=parsed.get("In-Reply-To", ""),
                        sender=parseaddr(parsed.get("From", ""))[1],
                        subject=decode_mime(parsed.get("Subject")),
                        body=extract_body(parsed),
                        received_at=parse_email_datetime(parsed.get("Date")),
                        attachments=extract_attachments(parsed),
                        thread_messages=thread_messages,
                    )
                )

            return messages
        finally:
            try:
                inbox.logout()
            except imaplib.IMAP4.error:
                pass

    def fetch_message(self, inbox: imaplib.IMAP4_SSL, uid: bytes):
        _, fetch_data = inbox.uid("fetch", uid, "(BODY.PEEK[])")
        return fetch_data

    def parse_provider_thread_id(self, fetch_data: Any) -> str:
        return ""

    def fetch_thread_messages(
        self,
        inbox: imaplib.IMAP4_SSL,
        provider_thread_id: str,
        parsed: email.message.EmailMessage,
        limit: int = THREAD_FETCH_LIMIT,
    ) -> list[ThreadMessage]:
        return [parse_thread_message(b"", parsed)]

    def mark_seen(self, uid: bytes):
        inbox = imaplib.IMAP4_SSL(RECRUITER_IMAP_HOST, RECRUITER_IMAP_PORT)
        try:
            inbox.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            inbox.select(RECRUITER_MAILBOX)
            inbox.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                inbox.logout()
            except imaplib.IMAP4.error:
                pass

    def mark_unseen(self, uid: bytes):
        inbox = imaplib.IMAP4_SSL(RECRUITER_IMAP_HOST, RECRUITER_IMAP_PORT)
        try:
            inbox.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            inbox.select(RECRUITER_MAILBOX)
            inbox.uid("store", uid, "-FLAGS", "(\\Seen)")
        finally:
            try:
                inbox.logout()
            except imaplib.IMAP4.error:
                pass


class GmailIMAPProvider(RecruiterInbox):
    provider_name = "gmail_imap"

    def fetch_message(self, inbox: imaplib.IMAP4_SSL, uid: bytes):
        try:
            _, fetch_data = inbox.uid("fetch", uid, "(X-GM-THRID BODY.PEEK[])")
            return fetch_data
        except imaplib.IMAP4.error:
            print("Gmail thread extension unavailable; falling back to standard IMAP fetch.")
            return super().fetch_message(inbox, uid)

    def parse_provider_thread_id(self, fetch_data: Any) -> str:
        return parse_gmail_thread_id(fetch_data[0][0] if fetch_data and fetch_data[0] else b"")

    def fetch_thread_messages(
        self,
        inbox: imaplib.IMAP4_SSL,
        provider_thread_id: str,
        parsed: email.message.EmailMessage,
        limit: int = THREAD_FETCH_LIMIT,
    ) -> list[ThreadMessage]:
        gmail_thread_id = provider_thread_id
        if not gmail_thread_id:
            return [parse_thread_message(b"", parsed)]
        try:
            _, search_data = inbox.uid("search", None, "X-GM-THRID", gmail_thread_id)
            uids = search_data[0].split()[-limit:]
            messages = []
            for uid in uids:
                _, fetch_data = inbox.uid("fetch", uid, "(BODY.PEEK[])")
                if not fetch_data or not fetch_data[0]:
                    continue
                parsed = email.message_from_bytes(fetch_data[0][1], policy=default)
                messages.append(parse_thread_message(uid, parsed))
            return messages
        except Exception as exc:
            print(f"Could not fetch Gmail thread {gmail_thread_id}: {exc}")
            return [parse_thread_message(b"", parsed)]


class OutlookIMAPProvider(RecruiterInbox):
    provider_name = "outlook_imap"


def parse_graph_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def graph_email_address(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return value.get("emailAddress", {}).get("address", "") or ""


def graph_header(headers: list[dict[str, str]] | None, name: str) -> str:
    if not headers:
        return ""
    target = name.lower()
    for header in headers:
        if header.get("name", "").lower() == target:
            return header.get("value", "")
    return ""


def graph_body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = body.get("content") or message.get("bodyPreview") or ""
    if body.get("contentType", "").lower() == "html":
        return strip_html(content)
    return content


def parse_graph_thread_message(message: dict[str, Any]) -> ThreadMessage:
    return ThreadMessage(
        uid=(message.get("id") or "").encode(),
        message_id=message.get("internetMessageId") or message.get("id") or "",
        sender=graph_email_address(message.get("from")),
        subject=message.get("subject") or "",
        body=graph_body_text(message),
        received_at=parse_graph_datetime(message.get("receivedDateTime")),
    )


class MicrosoftGraphProvider:
    provider_name = "microsoft_graph"
    graph_scopes = ["https://graph.microsoft.com/.default"]

    def __init__(self, mailbox: str | None = None):
        configured_mailbox = mailbox or MICROSOFT_MAILBOX
        missing = [
            name
            for name, value in {
                "MICROSOFT_TENANT_ID": MICROSOFT_TENANT_ID,
                "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
                "MICROSOFT_CLIENT_SECRET": MICROSOFT_CLIENT_SECRET,
                "MICROSOFT_MAILBOX": configured_mailbox,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Microsoft Graph settings: {', '.join(missing)}")

        self.requests = require_package("requests", "./venv/bin/python -m pip install -r requirements-recruiter.txt")
        self.msal = require_package("msal", "./venv/bin/python -m pip install -r requirements-recruiter.txt")
        self.authority = f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}"
        self.app = None
        self.base_url = MICROSOFT_GRAPH_BASE_URL.rstrip("/")
        self.mailbox_address = configured_mailbox
        self.mailbox = quote(configured_mailbox, safe="")

    def client(self):
        if self.app is None:
            self.app = self.msal.ConfidentialClientApplication(
                MICROSOFT_CLIENT_ID,
                authority=self.authority,
                client_credential=MICROSOFT_CLIENT_SECRET,
                validate_authority=False,
                instance_discovery=False,
            )
        return self.app

    def token(self) -> str:
        app = self.client()
        result = app.acquire_token_silent(self.graph_scopes, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=self.graph_scopes)
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error") or "unknown auth error"
            raise RuntimeError(f"Microsoft Graph authentication failed: {error}")
        return result["access_token"]

    def request(self, method: str, path: str, **kwargs):
        headers = kwargs.pop("headers", {})
        timeout = kwargs.pop("timeout", 30)
        headers["Authorization"] = f"Bearer {self.token()}"
        headers.setdefault("Accept", "application/json")
        # @odata.nextLink values are absolute URLs and must not be prefixed again.
        url = path if path.lower().startswith("http") else f"{self.base_url}{path}"
        response = self.requests.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Microsoft Graph {method} {path} failed: {response.status_code} {response.text}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def upload_onedrive_file(
        self,
        file_bytes: bytes,
        filename: str,
        folder: str | None = None,
        content_type: str = "application/octet-stream",
        user_email: str | None = None,
    ) -> dict[str, Any]:
        if not file_bytes:
            raise RuntimeError("Cannot upload empty OneDrive file.")
        drive_user = quote(user_email or ONEDRIVE_RECORDINGS_USER or self.mailbox_address, safe="")
        folder_parts = [
            safe_onedrive_path_part(part)
            for part in str(folder or ONEDRIVE_RECORDINGS_FOLDER or "AI Recruiter Interview Recordings").split("/")
            if safe_onedrive_path_part(part)
        ]
        safe_filename = safe_onedrive_path_part(filename)
        drive_path = "/".join([*folder_parts, safe_filename])
        return self.request(
            "PUT",
            f"/users/{drive_user}/drive/root:/{quote(drive_path, safe='/')}:/content",
            headers={"Content-Type": content_type or "application/octet-stream"},
            data=file_bytes,
            timeout=180,
        )

    def message_to_inbox_email(self, message: dict[str, Any]) -> InboxEmail:
        attachments = self.fetch_attachments(message["id"]) if message.get("hasAttachments") else []
        thread_messages = self.fetch_thread_messages(message.get("conversationId") or "")
        if not thread_messages:
            thread_messages = [parse_graph_thread_message(message)]
        return InboxEmail(
            uid=message["id"].encode(),
            gmail_thread_id=message.get("conversationId") or "",
            message_id=message.get("internetMessageId") or message.get("id") or "",
            references=graph_header(message.get("internetMessageHeaders"), "References"),
            in_reply_to=graph_header(message.get("internetMessageHeaders"), "In-Reply-To"),
            sender=graph_email_address(message.get("from")),
            subject=message.get("subject") or "",
            body=graph_body_text(message),
            received_at=parse_graph_datetime(message.get("receivedDateTime")),
            attachments=attachments,
            thread_messages=thread_messages,
        )

    def fetch_message_by_id(self, message_id: str, resource_path: str | None = None) -> InboxEmail | None:
        if not message_id:
            return None
        try:
            message = self.request(
                "GET",
                f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}",
                params={
                    "$select": (
                        "id,internetMessageId,conversationId,subject,body,bodyPreview,from,"
                        "receivedDateTime,hasAttachments,internetMessageHeaders,isRead"
                    ),
                },
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
        except RuntimeError as exc:
            print(f"Could not fetch Microsoft Graph message {message_id}: {exc}")
            if not resource_path:
                return None
            message = self.fetch_message_by_resource(resource_path)
            if not message:
                return None
        return self.message_to_inbox_email(message)

    def fetch_message_by_resource(self, resource_path: str) -> dict[str, Any] | None:
        resource_path = (resource_path or "").strip().lstrip("/")
        if not resource_path.lower().startswith("users/"):
            print(f"Skipping unsupported Microsoft Graph notification resource: {resource_path}")
            return None
        try:
            return self.request(
                "GET",
                f"/{resource_path}",
                params={
                    "$select": (
                        "id,internetMessageId,conversationId,subject,body,bodyPreview,from,"
                        "receivedDateTime,hasAttachments,internetMessageHeaders,isRead"
                    ),
                },
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
        except RuntimeError as exc:
            print(f"Could not fetch Microsoft Graph message from resource {resource_path}: {exc}")
            return None

    def fetch_unseen(self, limit: int) -> list[InboxEmail]:
        params = {
            "$filter": "isRead eq false",
            "$top": str(limit),
            "$select": (
                "id,internetMessageId,conversationId,subject,body,bodyPreview,from,"
                "receivedDateTime,hasAttachments,internetMessageHeaders"
            ),
        }
        data = self.request(
            "GET",
            f"/users/{self.mailbox}/mailFolders/inbox/messages",
            params=params,
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        messages = []
        for message in data.get("value", []):
            messages.append(self.message_to_inbox_email(message))
        return messages

    def excluded_thread_folder_ids(self) -> set[str]:
        """Folders whose contents must not count as part of a conversation.

        /users/{id}/messages spans every folder, so a reply that was deleted, or
        a draft that was never sent, still came back as thread history and the
        agent reasoned from messages the mailbox owner had thrown away.
        """
        cached = getattr(self, "_excluded_folder_ids", None)
        if cached is not None:
            return cached
        excluded = set()
        for well_known in ("deleteditems", "junkemail", "drafts"):
            try:
                folder = self.request(
                    "GET",
                    f"/users/{self.mailbox}/mailFolders/{well_known}",
                    params={"$select": "id"},
                )
                if folder.get("id"):
                    excluded.add(folder["id"])
            except Exception as exc:
                log_json(
                    logging.WARNING,
                    "graph_folder_lookup_failed",
                    folder=well_known,
                    error=str(exc)[:200],
                )
        self._excluded_folder_ids = excluded
        return excluded

    def keep_thread_message(self, message: dict[str, Any], excluded: set[str]) -> bool:
        if message.get("isDraft"):
            return False
        return message.get("parentFolderId") not in excluded

    def fetch_thread_messages(self, conversation_id: str, limit: int = THREAD_FETCH_LIMIT) -> list[ThreadMessage]:
        """Return the NEWEST `limit` messages of a conversation, oldest-first.

        Microsoft Graph returns a filtered /messages collection in ascending
        receivedDateTime order, so `$top` alone yields the OLDEST messages and
        freezes the agent's view of any thread longer than `limit`. The explicit
        descending `$orderby` is what makes this correct.
        """
        if not conversation_id:
            return []
        safe_conversation_id = conversation_id.replace("'", "''")
        select = "id,internetMessageId,subject,body,bodyPreview,from,receivedDateTime,parentFolderId,isDraft"
        excluded = self.excluded_thread_folder_ids()
        params = {
            "$filter": f"conversationId eq '{safe_conversation_id}'",
            "$orderby": "receivedDateTime desc",
            "$top": str(limit),
            "$select": select,
        }
        try:
            data = self.request(
                "GET",
                f"/users/{self.mailbox}/messages",
                params=params,
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            messages = [
                parse_graph_thread_message(message)
                for message in data.get("value", [])
                if self.keep_thread_message(message, excluded)
            ]
        except RuntimeError as exc:
            # Some tenants reject $filter + $orderby on /messages with an
            # InefficientFilter error. Fall back to paging and keeping the tail.
            log_json(
                logging.WARNING,
                "graph_thread_orderby_unsupported_paging_instead",
                conversation_id=conversation_id,
                error=str(exc)[:300],
            )
            messages = self.fetch_thread_messages_by_paging(safe_conversation_id, select, limit, excluded)

        messages.sort(key=lambda item: item.received_at.isoformat() if item.received_at else "")
        return messages[-limit:]

    def fetch_thread_messages_by_paging(
        self,
        safe_conversation_id: str,
        select: str,
        limit: int,
        excluded: set[str] | None = None,
    ) -> list[ThreadMessage]:
        """Page an ascending conversation and keep only the newest `limit` messages."""
        collected: list[ThreadMessage] = []
        params = {
            "$filter": f"conversationId eq '{safe_conversation_id}'",
            "$top": "50",
            "$select": select,
        }
        path = f"/users/{self.mailbox}/messages"
        for _ in range(20):  # hard page cap; 1000 messages is far beyond any real thread
            data = self.request(
                "GET",
                path,
                params=params,
                headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            collected.extend(
                parse_graph_thread_message(message)
                for message in data.get("value", [])
                if self.keep_thread_message(message, excluded or set())
            )
            next_link = data.get("@odata.nextLink")
            if not next_link:
                break
            path = next_link
            params = None
        return collected[-limit:] if len(collected) > limit else collected

    def fetch_attachments(self, message_id: str) -> list[tuple[str, bytes]]:
        data = self.request(
            "GET",
            f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}/attachments",
        )
        attachments = []
        for attachment in data.get("value", []):
            if attachment.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            content = attachment.get("contentBytes")
            name = attachment.get("name") or "attachment"
            content_type = attachment.get("contentType") or ""
            attachment_name = f"{name}|{content_type}" if content_type else name
            if content:
                attachments.append((attachment_name, base64.b64decode(content)))
        return attachments

    def mark_seen(self, uid: bytes):
        message_id = uid.decode()
        self.request(
            "PATCH",
            f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}",
            json={"isRead": True},
        )

    def mark_unseen(self, uid: bytes):
        message_id = uid.decode()
        self.request(
            "PATCH",
            f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}",
            json={"isRead": False},
        )

    def send_reply(self, inbox_email: InboxEmail, subject: str, body: str, to_email: str | None = None) -> str | None:
        """Send a threaded reply. Returns the internetMessageId when known.

        The returned id is recorded in the reply ledger so the agent can later
        tell its own outbound messages apart from ones a human typed into the
        shared mailbox.
        """
        recipient = to_email or inbox_email.sender
        if not RECRUITER_REPLY_ENABLED:
            print(f"[reply disabled] To: {recipient} | Subject: {subject}\n{body}")
            return None

        body = body.replace("\n", "\r\n").replace("\r\r\n", "\r\n")
        message_id = inbox_email.uid.decode()
        if message_id:
            draft = self.request(
                "POST",
                f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}/createReply",
                json={},
            )
            draft_id = draft.get("id")
            if not draft_id:
                raise RuntimeError(f"Microsoft Graph createReply did not return a draft id: {draft}")
            sent_internet_id = draft.get("internetMessageId")
            draft_body = (draft.get("body") or {}).get("content") or ""
            reply_html = append_signature_if_needed(email_body_to_html(body), draft_body)
            content = f"{reply_html}<br>{draft_body}" if draft_body else reply_html
            patch_payload = {"body": {"contentType": "HTML", "content": content}}
            if to_email and clean_email(to_email) != clean_email(inbox_email.sender):
                patch_payload["toRecipients"] = [{"emailAddress": {"address": recipient}}]
            self.request(
                "PATCH",
                f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}",
                json=patch_payload,
            )
            self.request(
                "POST",
                f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}/send",
                json={},
            )
            if not sent_internet_id:
                try:
                    sent = self.request(
                        "GET",
                        f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}",
                        params={"$select": "internetMessageId"},
                    )
                    sent_internet_id = sent.get("internetMessageId")
                except Exception:
                    sent_internet_id = None
            return sent_internet_id

        base_subject = inbox_email.subject or subject
        reply_subject = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
        reply_html = append_signature_if_needed(email_body_to_html(body))
        self.request(
            "POST",
            f"/users/{self.mailbox}/sendMail",
            json={
                "message": {
                    "subject": reply_subject,
                    "body": {"contentType": "HTML", "content": reply_html},
                    "toRecipients": [{"emailAddress": {"address": recipient}}],
                },
                "saveToSentItems": True,
            },
        )
        return None

    def send_direct_email(self, to_email: str, subject: str, body: str) -> str | None:
        """Send a standalone email. Returns its internetMessageId when known.

        Created as a draft first so the id is available: sendMail returns
        nothing, which meant every direct email the agent sent was invisible to
        the reply ledger, and the next candidate reply in that thread looked
        like a human had taken the conversation over.
        """
        if not RECRUITER_REPLY_ENABLED:
            print(f"[direct reply disabled] To: {to_email} | Subject: {subject}\n{body}")
            return None
        reply_html = append_signature_if_needed(email_body_to_html(body))
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": reply_html},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }
        try:
            draft = self.request("POST", f"/users/{self.mailbox}/messages", json=message)
            draft_id = draft.get("id")
            if draft_id:
                self.request(
                    "POST",
                    f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}/send",
                    json={},
                )
                return draft.get("internetMessageId")
        except Exception as exc:
            log_json(
                logging.WARNING,
                "graph_draft_send_failed_falling_back",
                error=str(exc)[:300],
            )
        self.request(
            "POST",
            f"/users/{self.mailbox}/sendMail",
            json={"message": message, "saveToSentItems": True},
        )
        return None

    def create_online_meeting(self, subject: str, start_at: datetime, end_at: datetime) -> str:
        data = self.request(
            "POST",
            f"/users/{self.mailbox}/onlineMeetings",
            json={
                "subject": subject,
                "startDateTime": start_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "endDateTime": end_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        return data.get("joinWebUrl") or data.get("joinUrl") or ""

    def get_online_meeting_by_join_url(self, join_url: str) -> dict[str, Any] | None:
        if not join_url:
            return None
        data = self.request(
            "GET",
            f"/users/{self.mailbox}/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{join_url}'"},
        )
        meetings = data.get("value") or []
        return meetings[0] if meetings else None

    def cancel_calendar_event(self, event_id: str, comment: str = "This interview has been rescheduled.") -> bool:
        if not event_id:
            return False
        try:
            self.request(
                "POST",
                f"/users/{self.mailbox}/events/{quote(event_id, safe='')}/cancel",
                json={"Comment": comment},
            )
            return True
        except RuntimeError as exc:
            if "404" in str(exc) or "ErrorItemNotFound" in str(exc):
                return False
            raise

    def create_teams_calendar_event(
        self,
        recipient_email: str,
        subject: str,
        start_at: datetime,
        end_at: datetime,
        body: str,
    ) -> tuple[str, str]:
        start_local = as_recruiter_time(start_at)
        end_local = as_recruiter_time(end_at)
        event = self.request(
            "POST",
            f"/users/{self.mailbox}/events",
            json={
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": email_body_to_html(body),
                },
                "start": {
                    "dateTime": start_local.replace(tzinfo=None).isoformat(timespec="seconds"),
                    "timeZone": RECRUITER_TIMEZONE,
                },
                "end": {
                    "dateTime": end_local.replace(tzinfo=None).isoformat(timespec="seconds"),
                    "timeZone": RECRUITER_TIMEZONE,
                },
                "location": {"displayName": "Microsoft Teams"},
                "attendees": [
                    {
                        "emailAddress": {"address": recipient_email},
                        "type": "required",
                    }
                ],
                "isOnlineMeeting": True,
                "onlineMeetingProvider": "teamsForBusiness",
            },
        )
        event_id = event.get("id") or ""
        join_url = ((event.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
        if event_id and not join_url:
            time.sleep(1)
            refreshed = self.request(
                "GET",
                f"/users/{self.mailbox}/events/{quote(event_id, safe='')}",
                params={"$select": "id,onlineMeeting,webLink"},
            )
            join_url = ((refreshed.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
        return event_id, join_url

    def create_inbox_subscription(self, notification_url: str, client_state: str, hours: int) -> dict[str, Any]:
        if not notification_url:
            raise RuntimeError("GRAPH_NOTIFICATION_URL is required, for example https://your-domain.com/graph/outlook")
        if not client_state:
            raise RuntimeError("GRAPH_CLIENT_STATE is required. Use a random secret string.")

        expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, min(hours, 48)))
        resource = f"users/{MICROSOFT_MAILBOX}/mailFolders('Inbox')/messages"
        return self.request(
            "POST",
            "/subscriptions",
            json={
                "changeType": "created",
                "notificationUrl": notification_url,
                "resource": resource,
                "expirationDateTime": expires_at.isoformat().replace("+00:00", "Z"),
                "clientState": client_state,
            },
        )

    def renew_inbox_subscription(self, subscription_id: str, hours: int) -> dict[str, Any]:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, min(hours, 48)))
        return self.request(
            "PATCH",
            f"/subscriptions/{quote(subscription_id, safe='')}",
            json={"expirationDateTime": expires_at.isoformat().replace("+00:00", "Z")},
        )

    def renew_or_create_inbox_subscription(
        self,
        notification_url: str,
        client_state: str,
        hours: int,
    ) -> dict[str, Any]:
        """Keep exactly one live Inbox subscription for this mailbox.

        Graph subscriptions expire after at most 48 hours. Nothing renewed them,
        so mail processing stopped silently whenever one lapsed - no error, no
        log line, just candidates never receiving a reply.
        """
        target = f"users/{MICROSOFT_MAILBOX}/mailfolders('inbox')/messages"
        for subscription in self.list_subscriptions():
            resource = (subscription.get("resource") or "").lower().replace("%40", "@")
            if resource != target.lower():
                continue
            if (subscription.get("notificationUrl") or "") != notification_url:
                continue
            try:
                renewed = self.renew_inbox_subscription(subscription["id"], hours)
                log_json(
                    logging.INFO,
                    "graph_subscription_renewed",
                    subscription_id=subscription["id"],
                    expires=renewed.get("expirationDateTime"),
                )
                return renewed
            except Exception as exc:
                log_json(
                    logging.WARNING,
                    "graph_subscription_renew_failed_recreating",
                    subscription_id=subscription.get("id"),
                    error=str(exc)[:300],
                )
                break
        created = self.create_inbox_subscription(notification_url, client_state, hours)
        log_json(
            logging.INFO,
            "graph_subscription_created",
            subscription_id=created.get("id"),
            expires=created.get("expirationDateTime"),
        )
        return created

    def list_subscriptions(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/subscriptions")
        return data.get("value", [])

    def delete_subscription(self, subscription_id: str):
        if not subscription_id:
            raise RuntimeError("A Microsoft Graph subscription id is required.")
        self.request("DELETE", f"/subscriptions/{quote(subscription_id, safe='')}")

    def delete_matching_inbox_subscriptions(
        self,
        notification_url: str | None = None,
        exclude_subscription_id: str | None = None,
    ) -> list[dict[str, Any]]:
        deleted = []
        inbox_resource = f"users/{MICROSOFT_MAILBOX}/mailFolders('Inbox')/messages".lower()
        normalized_url = notification_url.lower() if notification_url else ""
        excluded = exclude_subscription_id or ""
        for subscription in self.list_subscriptions():
            if excluded and subscription.get("id") == excluded:
                continue
            resource = (subscription.get("resource") or "").lower()
            url = (subscription.get("notificationUrl") or "").lower()
            client_state = subscription.get("clientState") or ""
            is_current_inbox = resource == inbox_resource
            is_same_url = bool(normalized_url and url == normalized_url)
            is_same_client_state = bool(GRAPH_CLIENT_STATE and client_state == GRAPH_CLIENT_STATE)
            if not (is_current_inbox or is_same_url or is_same_client_state):
                continue
            self.delete_subscription(subscription.get("id", ""))
            deleted.append(subscription)
        return deleted


def build_inbox_provider() -> RecruiterInbox:
    provider = MAIL_PROVIDER.lower()
    if provider in {"gmail", "gmail_imap"}:
        return GmailIMAPProvider()
    if provider in {"outlook", "outlook_imap", "imap"}:
        return OutlookIMAPProvider() if provider in {"outlook", "outlook_imap"} else RecruiterInbox()
    if provider in {"graph", "microsoft_graph", "outlook_graph"}:
        return MicrosoftGraphProvider()
    raise RuntimeError(
        f"Unsupported MAIL_PROVIDER={MAIL_PROVIDER!r}. Use gmail_imap, outlook_imap, microsoft_graph, or imap."
    )



class RecruiterMailer:
    def send_direct_email(self, to_email: str, subject: str, body: str):
        if not RECRUITER_REPLY_ENABLED:
            print(f"[direct reply disabled] To: {to_email} | Subject: {subject}\n{body}")
            return

        missing = [
            name
            for name, value in {
                "RECRUITER_SMTP_HOST": RECRUITER_SMTP_HOST,
                "RECRUITER_EMAIL": RECRUITER_EMAIL,
                "RECRUITER_EMAIL_PASSWORD": RECRUITER_EMAIL_PASSWORD,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing SMTP settings: {', '.join(missing)}")

        plain_body = f"{body}\r\n\r\n{recruiter_plain_signature()}" if RECRUITER_APPEND_SIGNATURE else body
        html_body = append_signature_if_needed(email_body_to_html(body))
        message = EmailMessage()
        message["From"] = RECRUITER_FROM_EMAIL
        message["To"] = to_email
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(plain_body)
        message.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(RECRUITER_SMTP_HOST, RECRUITER_SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            smtp.send_message(message)
        return message["Message-ID"]

    def send_reply(self, inbox_email: InboxEmail, subject: str, body: str, to_email: str | None = None):
        recipient = to_email or inbox_email.sender
        if not RECRUITER_REPLY_ENABLED:
            print(f"[reply disabled] To: {recipient} | Subject: {subject}\n{body}")
            return

        missing = [
            name
            for name, value in {
                "RECRUITER_SMTP_HOST": RECRUITER_SMTP_HOST,
                "RECRUITER_EMAIL": RECRUITER_EMAIL,
                "RECRUITER_EMAIL_PASSWORD": RECRUITER_EMAIL_PASSWORD,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing SMTP settings: {', '.join(missing)}")

        message = EmailMessage()
        message["From"] = RECRUITER_FROM_EMAIL
        message["To"] = recipient
        base_subject = inbox_email.subject or subject
        message["Subject"] = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
        if inbox_email.message_id:
            message["In-Reply-To"] = inbox_email.message_id
            references = " ".join(
                value
                for value in [inbox_email.references, inbox_email.in_reply_to, inbox_email.message_id]
                if value
            )
            message["References"] = references
        plain_body = f"{body}\r\n\r\n{recruiter_plain_signature()}" if RECRUITER_APPEND_SIGNATURE else body
        html_body = append_signature_if_needed(email_body_to_html(body))
        message.set_content(plain_body)
        message.add_alternative(html_body, subtype="html")

        with smtplib.SMTP(RECRUITER_SMTP_HOST, RECRUITER_SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            smtp.send_message(message)


class RecruiterAI:
    def __init__(self):
        self.llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1400))
        self.repair_llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1400))
        self.reply_llm = make_chat_model(json_mode=False, max_tokens=350)

    def json_call(self, prompt: str) -> dict[str, Any]:
        started_at = time.monotonic()
        try:
            response = self.llm.invoke(
                f"""
Return one valid JSON object only.
Do not include markdown, comments, trailing commas, or explanatory text.

{prompt}
"""
            ).content
            json_text = self.extract_json_object(response)
            try:
                result = json.loads(json_text)
            except json.JSONDecodeError as exc:
                log_json(
                    logging.WARNING,
                    "llm_json_parse_failed_repairing",
                    error=str(exc),
                    response_preview=str(response)[:1000],
                    elapsed_ms=round((time.monotonic() - started_at) * 1000),
                )
                repaired = self.repair_json(json_text, exc)
                result = json.loads(repaired)
            log_json(
                logging.DEBUG,
                "llm_json_call_completed",
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                response_chars=len(str(response)),
            )
            return result
        except Exception as exc:
            LOGGER.exception(
                "llm_json_call_failed %s",
                json.dumps(
                    {
                        "error": str(exc),
                        "prompt_preview": prompt[:1000],
                        "elapsed_ms": round((time.monotonic() - started_at) * 1000),
                    },
                    default=str,
                ),
            )
            raise

    def extract_json_object(self, response: str) -> str:
        match = re.search(r"\{.*\}", response, flags=re.S)
        if not match:
            raise ValueError(f"LLM did not return JSON: {response}")
        return match.group(0)

    @traceable(name="draft_hr_reply")
    def draft_reply(
        self,
        inbox_email: InboxEmail,
        scenario: str,
        facts: dict[str, Any],
        fallback_body: str,
    ) -> str:
        thread_context = build_thread_context(inbox_email)
        response = self.reply_llm.invoke(
            f"""
You are an HR recruiter replying by email.
Read the full thread and write the reply naturally, like a real HR person.

Rules:
- Return only the email body text.
- Start with "Hi,".
- Use short paragraphs with blank lines between them.
- Be warm, concise, and professional.
- Do not sound automated.

Voice (important):
- You ARE the HR team for this company. Write in the first person ("I", "we").
- Never refer to "the team", "our team", "our HR team", "the recruiter", or "the
  concerned department" as if they were someone else. You are them.
- Never say a message will be forwarded, passed on, escalated, shared internally,
  reviewed internally, or sent to anyone else. Decisions happen with you.
- Never say "in our records", "in our system", or "our system shows".
- Never mention systems, tools, automation, AI, databases, scores, or matching.

Content:
- Do not invent interviews, shortlisting, salaries, deadlines, or openings that are not in the facts.
- Never ask for information that already appears in the facts as received or known.
  If the facts list what is still missing, ask only for those items and briefly
  acknowledge what you already have.
- If the facts say CV is required, clearly ask for the CV as PDF, DOCX, or TXT.
- If the facts say no active opening, say so politely and say you will keep the profile for future suitable roles.
- If the facts say application received, acknowledge receipt and say you will come back to them.
- Do not include a signature. The mailer adds the HR signature separately.

Scenario:
{scenario}

Facts:
{json.dumps(facts, default=str)}

Full email thread:
{thread_context[:8000]}
"""
        ).content.strip()
        response = self.clean_reply_body(response)
        if not self.reply_has_meaningful_content(response):
            log_json(
                logging.WARNING,
                "llm_reply_empty_using_fallback",
                scenario=scenario,
                response_preview=response[:300],
                fallback_preview=fallback_body[:300],
            )
            return fallback_body
        return response

    def reply_has_meaningful_content(self, body: str) -> bool:
        text = normalize_position_text(body)
        words = [
            word
            for word in text.split()
            if word not in {"hi", "hello", "regards", "best", "hr", "team", "thanks", "thank", "you"}
        ]
        return len(words) >= 4

    def clean_reply_body(self, body: str) -> str:
        body = re.sub(r"```(?:text|html|markdown)?", "", body, flags=re.I).replace("```", "")
        body = body.strip().strip('"').strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        signature_markers = [
            RECRUITER_SIGNATURE_EMAIL.lower(),
            RECRUITER_SIGNATURE_COMPANY.lower(),
            RECRUITER_SIGNATURE_NAME.lower(),
        ]
        lines = body.splitlines()
        kept_lines = []
        for line in lines:
            lowered = line.strip().lower()
            if lowered and any(marker and marker in lowered for marker in signature_markers):
                break
            kept_lines.append(line.rstrip())
        body = "\n".join(kept_lines).strip()
        if not body.lower().startswith("hi"):
            body = f"Hi,\n\n{body}"
        return body

    def repair_json(self, broken_json: str, error: json.JSONDecodeError) -> str:
        repaired = self.repair_llm.invoke(
            f"""
Repair this invalid JSON into one valid JSON object.
Return only the repaired JSON object. Preserve all available data.

Parser error:
{error}

Invalid JSON:
{broken_json}
"""
        ).content
        return self.extract_json_object(repaired)

    @traceable(name="classify_recruiting_email")
    def classify_email(self, inbox_email: InboxEmail) -> dict[str, Any]:
        thread_context = build_thread_context(inbox_email)
        return self.json_call(
            f"""
Return only valid JSON.
Act like a human HR recruiter reading the full email thread.
Classify whether this thread is about employment, hiring, job application, internship, or a candidate CV.
If there are multiple messages, infer the requested position from the conversation context.
For example, if HR asked for a CV for Accountant and the latest reply only says "attached", detected_position must remain Accountant.

JSON schema:
{{
  "is_employment_related": true,
  "detected_position": "string or null",
  "latest_intent": "asking_about_opening/submitting_cv/follow_up/other",
  "reason": "short reason"
}}

Email thread:
{thread_context[:8000]}
"""
        )

    @traceable(name="extract_cv_details")
    def extract_cv_details(self, cv_text: str, thread_context: str) -> dict[str, Any]:
        return normalize_cv_details(self.json_call(
            f"""
Return only valid JSON.
Extract all useful candidate details from this CV. Use null when unknown.
Only extract candidate facts from the CV text. The email thread is context for the application, not proof of the candidate's CV role.

JSON schema:
{{
  "full_name": null,
  "email": null,
  "phone": null,
  "location": null,
  "linkedin_url": null,
  "portfolio_url": null,
  "target_position": null,
  "current_title": null,
  "current_company": null,
  "total_experience_years": null,
  "skills": [],
  "education": [],
  "work_history": [],
  "certifications": [],
  "projects": [],
  "achievements": []
}}

Email thread context:
{thread_context[:2500]}

CV text:
{cv_text[:12000]}
"""
        ))

    @traceable(name="summarize_cv_role")
    def summarize_cv_role(self, cv_text: str, extracted: dict[str, Any]) -> dict[str, Any]:
        return self.json_call(
            f"""
Return only valid JSON.
Read the CV and summarize what type of job this candidate actually belongs to.
Focus on the candidate's main role, current title, work history, projects, and core skills.
Do not infer Accountant from phrases like "user accounts", "account unlocks", "Active Directory accounts", or support tickets.

JSON schema:
{{
  "cv_summary": "2-3 sentence recruiter summary",
  "primary_role": "specific role title or null",
  "role_family": "short free-form role family, for example tax, accounting, it_support, seo, python_backend, ui_ux, sales, hr, admin, or other",
  "seniority": "junior/mid/senior/unknown",
  "evidence": ["short evidence from CV"],
  "confidence": 0
}}

Already extracted CV facts:
{json.dumps(safe_trace_payload(extracted), default=str)}

CV text:
{cv_text[:12000]}
"""
        )

    @traceable(name="match_requirement")
    def match_requirement(
        self,
        extracted: dict[str, Any],
        requirements: list[dict[str, Any]],
        cv_text: str = "",
    ) -> dict[str, Any]:
        compact_requirements = [
            {
                "id": row["id"],
                "position_title": row["position_title"],
                "experience_min_years": str(row["experience_min_years"]),
                "experience_max_years": str(row["experience_max_years"]),
                "budget_min": str(row["budget_min"]),
                "budget_max": str(row["budget_max"]),
                "urgently_required": row["urgently_required"],
                "needed_within_days": row["needed_within_days"],
                "job_description": (row["job_description"] or "")[:2500],
            }
            for row in requirements
        ]
        return self.json_call(
            f"""
Return only valid JSON.
Choose the best open requirement for this candidate, or null if none fits.
Match by what the candidate actually DOES - their work history, responsibilities and
skills - against the requirement's job description. Job titles are a weak signal:
people describe the same work with different titles and seniorities.

- If the CV evidences the core work in the JD, that is a match even when the
  titles differ (a CV headed "Fractional CFO & US Bookkeeping" fits a Bookkeeper
  role, because the bookkeeping work is there).
- Being more senior than the role is NOT a reason to return null. Match it and
  let a human weigh seniority.
- Do not match on incidental shared words alone, and return null when the
  candidate works in a genuinely different function.
- Use confidence >= 0.75 when the work described in the CV covers the JD.

JSON schema:
{{
  "requirement_id": null,
  "confidence": 0,
  "reason": "short reason with evidence from CV and JD"
}}

Candidate:
{json.dumps(extracted)}

Candidate CV (primary evidence - weigh this above the job title):
{(cv_text or "")[:4000]}

Open requirements:
{json.dumps(compact_requirements)}
"""
        )

    @traceable(name="evaluate_cv_ats")
    def evaluate_cv(self, cv_text: str, extracted: dict[str, Any], requirement: dict[str, Any] | None) -> dict[str, Any]:
        jd = requirement["job_description"] if requirement else "No matching active job requirement."
        position = requirement["position_title"] if requirement else extracted.get("target_position")
        return normalize_evaluation(self.json_call(
            f"""
Return only valid JSON.
Evaluate this CV like an ATS and recruiter.
If a job description is provided, include a JD match score. If no job requirement exists, still provide a general ATS score.

JSON schema:
{{
  "ats_score": 0,
  "jd_match_score": null,
  "short_description": "2 sentence recruiter summary",
  "strengths": [],
  "risks": [],
  "missing_requirements": [],
  "recommendation": "shortlist/review/reject/hold",
  "reasoning": "brief reasoning"
}}

Target position: {position}
Candidate details:
{json.dumps(extracted)}

Job description:
{jd[:5000]}

CV text:
{cv_text[:12000]}
"""
        ))

    @traceable(name="extract_screening_answers")
    def extract_screening_answers(self, inbox_email: InboxEmail, application: dict[str, Any]) -> dict[str, Any]:
        """Extract only what is NEW in the latest reply.

        Values already stored on the application are supplied as known facts
        rather than re-derived from the thread, so a value cannot be "un-learned"
        when the message that carried it falls outside the context window. The
        caller merges the result over the stored answers.
        """
        thread_context = build_thread_context(inbox_email)
        known = json_dict(application.get("screening_details"))
        return self.json_call(
            f"""
Return only valid JSON.
Extract the candidate's screening answers.
Prefer the newest message. If the candidate corrected a value, use the corrected one.
Anything listed under "Already known" is confirmed; repeat it unless the candidate changed it.
For salary, return the numeric annual amount. If candidate says 6 LPA or 6 lakh, return 600000.
For joining, fill joining_days when they give a notice period in days or months,
and joining_date (YYYY-MM-DD) when they name a date such as "4 Sep" or
"after 31 August". Either one answers the question; fill whichever they gave.
If a value is genuinely unknown, use null.

JSON schema:
{{
  "comfortable_with_terms": true,
  "current_salary": null,
  "expected_salary": null,
  "current_location": null,
  "joining_days": null,
  "joining_date": null,
  "interview_availability": null,
  "notes": "short notes"
}}

Already known (do not lose these):
{json.dumps(known, default=str)}

Role and budget:
{json.dumps(application_prompt_facts(application), default=str)}

Work terms:
{json.dumps(screening_work_terms())}

Candidate's latest message:
{latest_reply_text(inbox_email.body)[:2000]}

Email thread:
{thread_context[:8000]}
"""
        )

    @traceable(name="classify_budget_response")
    def classify_budget_response(self, inbox_email: InboxEmail, application: dict[str, Any]) -> dict[str, Any]:
        """Read one thing only: did the candidate accept the stated range?

        Deliberately scoped to the latest reply. Handing this the whole thread is
        what let unrelated numbers and older messages contaminate the decision.
        """
        latest = latest_reply_text(inbox_email.body)[:1500]
        try:
            return self.json_call(
                f"""
Return only valid JSON.
The candidate was told the salary range for this role and asked whether it works for them.
Classify ONLY their answer to that question.

Ignore notice periods, dates, years of experience, phone numbers, and any other
number that is not a salary. A number alone is never an answer.

  "accepts"  - they agree to the stated range
  "rejects"  - they decline it
  "counter"  - they propose a different figure
  "unclear"  - anything else, including no direct answer

JSON schema:
{{
  "response": "accepts|rejects|counter|unclear",
  "counter_amount": null,
  "evidence": "the exact words that decided it"
}}

Stated range:
{requirement_budget_text(application)}

Candidate's reply:
{latest}
"""
            )
        except Exception as exc:
            log_json(logging.WARNING, "classify_budget_response_failed", error=str(exc)[:300])
            return {}

    @traceable(name="extract_interview_schedule")
    def extract_interview_schedule(self, inbox_email: InboxEmail, application: dict[str, Any]) -> dict[str, Any]:
        thread_context = build_thread_context(inbox_email)
        now_text = recruiter_now().isoformat()
        return self.json_call(
            f"""
Return only valid JSON.
The candidate is sharing interview availability. Extract one clear interview slot if possible.
Use the current local datetime to resolve words like today/tomorrow.
If the candidate gives multiple options, choose the earliest clear future slot.
If no clear date and time is available, set scheduled_at to null.

JSON schema:
{{
  "scheduled_at": null,
  "duration_minutes": 45,
  "availability_text": "short summary",
  "needs_clarification": false
}}

Current local datetime:
{now_text}

Application:
{json.dumps(application_prompt_facts(application), default=str)}

Email thread:
{thread_context[:8000]}
"""
        )


class AIRecruiterAgent:
    def __init__(self):
        self.db = RecruiterDatabase()
        self.inbox = build_inbox_provider()
        self.mailer = self.inbox if hasattr(self.inbox, "send_reply") else RecruiterMailer()
        self.ai = RecruiterAI()

    def close(self):
        self.db.close()

    def init_schema(self):
        self.db.init_schema()

    def send_candidate_reply(
        self,
        inbox_email: InboxEmail,
        subject: str,
        body: str,
        scenario: str,
        application: dict[str, Any] | None = None,
        to_email: str | None = None,
    ) -> bool:
        """The single gate every candidate-facing email passes through.

        Three guarantees are enforced here rather than in each caller, because
        the callers are exactly what kept getting them wrong:
          1. the same scenario is never repeated inside the cooldown window
          2. some scenarios are never repeated at all
          3. a reply that breaks the HR persona is downgraded, not sent

        Returns True if an email actually went out.
        """
        application_id = (application or {}).get("id")
        recipient = to_email or inbox_email.sender

        if scenario in ONCE_PER_APPLICATION_SCENARIOS:
            already_sent = self.db.scenario_ever_sent(application_id, recipient, scenario)
            sent_count = 1 if already_sent else 0
        else:
            sent_count = self.db.sent_reply_count(application_id, recipient, scenario)
            already_sent = sent_count >= REPLY_SCENARIO_MAX_PER_WINDOW

        if already_sent:
            log_json(
                logging.WARNING,
                "duplicate_reply_suppressed",
                **inbox_email_summary(inbox_email),
                scenario=scenario,
                application_id=application_id,
                already_sent_count=sent_count,
                suppressed_body=body[:600],
            )
            trace_recruiter_event(
                "duplicate_reply_suppressed",
                inputs=inbox_email_summary(inbox_email),
                outputs={"scenario": scenario, "application_id": application_id},
                tags=["guardrail"],
            )
            self.db.log_email_event(
                inbox_email,
                "duplicate_reply_suppressed",
                {
                    "scenario": scenario,
                    "application_id": application_id,
                    "already_sent_count": sent_count,
                    "suppressed_body": body[:2000],
                    "no_candidate_reply_sent": True,
                },
            )
            # Tell a human, but do NOT touch the application status. The guard's
            # job is to stop the email, not to rewrite pipeline state: a run that
            # had just booked a Teams meeting and set interview_scheduled found
            # itself dragged back to manual_hr_review by this branch, so the
            # meeting existed and nobody was told about it.
            self.notify_hr_rate_limited(
                inbox_email,
                application,
                f"Repeat message suppressed: {inbox_email.sender}",
                recruiter_email_body(
                    f"The agent tried to send the '{scenario}' message again and it was suppressed.",
                    f"Candidate: {inbox_email.sender}",
                    f"Application: {dashboard_application_url(application_id)}"
                    if application_id
                    else RECRUITER_DASHBOARD_BASE_URL,
                    "If the candidate still needs this message, send it from the dashboard. "
                    "If the thread is going in circles, take it over.",
                ),
                "repeat_reply_notice",
            )
            self.db.log_email_event(
                inbox_email,
                "repeat_reply_handoff_to_hr",
                {"scenario": scenario, "application_id": application_id, "no_candidate_reply_sent": True},
            )
            return False

        leak = persona_leak(body)
        if leak:
            log_json(
                logging.WARNING,
                "persona_leak_detected",
                **inbox_email_summary(inbox_email),
                scenario=scenario,
                phrase=leak,
            )
            trace_recruiter_event(
                "persona_leak_detected",
                inputs=inbox_email_summary(inbox_email),
                outputs={"scenario": scenario, "phrase": leak},
                tags=["guardrail"],
            )
            body = strip_persona_leak_lines(body)

        provider_message_id = self.mailer.send_reply(inbox_email, subject, body, to_email=to_email)
        self.db.record_sent_reply(application_id, recipient, scenario, body, provider_message_id)
        log_json(
            logging.INFO,
            "candidate_reply_sent",
            **inbox_email_summary(inbox_email),
            scenario=scenario,
            application_id=application_id,
        )
        return True

    def thread_taken_over_by_human(self, inbox_email: InboxEmail) -> bool:
        """True when the newest outbound message in this thread was not ours.

        A colleague replying from the shared mailbox leaves no status behind, so
        without this the agent talks straight over them.

        This fails SAFE. Deciding "a human wrote that" silences the agent on the
        thread permanently, so it only concludes so when it can positively
        recognise its own messages: if nothing the agent sent to this recipient
        was ever recorded, it cannot tell the difference and must not guess.
        That is exactly what went wrong when direct emails were not being
        recorded - the agent read its own "Final HR round availability" message
        as a stranger's and abandoned the candidate.
        """
        messages = inbox_email.thread_messages or []
        if not messages:
            return False
        mailbox = clean_email(MICROSOFT_MAILBOX) or clean_email(RECRUITER_FROM_EMAIL)
        outbound = [
            message
            for message in messages
            if mailbox and clean_email(message.sender) == mailbox
        ]
        if not outbound:
            return False
        latest_outbound = outbound[-1]
        if not latest_outbound.message_id:
            return False

        known_ids = self.db.agent_sent_message_ids()
        if latest_outbound.message_id in known_ids:
            return False

        # Do we have any record of our own outbound mail in this thread at all?
        # If not, the ledger simply has no coverage here and an unknown id proves
        # nothing.
        thread_ids = {message.message_id for message in outbound if message.message_id}
        if not (thread_ids & known_ids):
            log_json(
                logging.INFO,
                "human_takeover_check_skipped_no_ledger_coverage",
                **inbox_email_summary(inbox_email),
                outbound_in_thread=len(outbound),
            )
            return False
        return True

    def notify_manual_hr_review(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any] | None,
        reason: str,
        event_type: str = "manual_hr_review_requested",
        mark_application: bool = True,
    ):
        application_id = application.get("id") if application else None
        if application_id and mark_application:
            self.db.mark_manual_hr_review(application_id, reason)
        dashboard_url = dashboard_application_url(application_id) if application_id else RECRUITER_DASHBOARD_BASE_URL
        role = None
        if application:
            role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        body = recruiter_email_body(
            "Please review this candidate manually.",
            f"Reason: {reason}",
            f"Candidate email: {inbox_email.sender}",
            f"Subject: {inbox_email.subject}",
            f"Current status: {application.get('application_status') if application else '-'}",
            f"Role: {role or '-'}",
            f"Dashboard: {dashboard_url}",
            "Latest message:",
            inbox_email.body[:1800] or "-",
        )
        send_hr_notification(
            self.mailer if hasattr(self.mailer, "send_direct_email") else RecruiterMailer(),
            f"Manual HR review needed: {inbox_email.subject}",
            body,
        )
        self.db.log_email_event(
            inbox_email,
            event_type,
            {
                "application_id": application_id,
                "reason": reason,
                "dashboard_url": dashboard_url,
                "no_candidate_reply_sent": True,
            },
        )

    def reply_missing_cv(self, inbox_email: InboxEmail):
        fallback_body = recruiter_email_body(
            "Thanks for reaching out.",
            "Please send your updated CV as a PDF, DOCX, or TXT attachment. Once we have it, we can review your profile for the role discussed in this thread.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate asked about or wants to apply for an open role, but no CV was attached",
            {"cv_required": True, "supported_cv_formats": ["PDF", "DOCX", "TXT"]},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"CV required: {inbox_email.subject}",
            body,
            scenario="missing_cv",
        )

    def reply_followup_missing_cv(
        self,
        inbox_email: InboxEmail,
        requirement: dict[str, Any] | None,
        application: dict[str, Any] | None = None,
    ):
        role = requirement["position_title"] if requirement else None
        role_text = f" for {role}" if role else ""
        fallback_body = recruiter_email_body(
            "Thanks for following up.",
            f"I still need your updated CV{role_text} before I can take your application further.",
            "Please send it as a PDF, DOCX, or TXT attachment and I will pick it up from there.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate followed up, but no CV has been received yet for the role discussed in the thread",
            {"cv_required": True, "role": role, "supported_cv_formats": ["PDF", "DOCX", "TXT"]},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"CV required: {inbox_email.subject}",
            body,
            scenario="followup_missing_cv",
            application=application,
        )

    def reply_wrong_cv(
        self,
        inbox_email: InboxEmail,
        requested_position: str | None,
        cv_position: str | None,
        application: dict[str, Any] | None = None,
    ):
        requested_text = f" for {requested_position}" if requested_position else ""
        cv_text = f" It reads closer to a {cv_position} profile." if cv_position else ""
        fallback_body = recruiter_email_body(
            "Thanks for sharing the CV.",
            f"I was expecting a CV{requested_text}, and this attachment does not look like a match.{cv_text}",
            "Could you check and send the right one? I will review it as soon as it arrives.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate attached a CV, but it does not match the role requested in the email thread",
            {
                "requested_position": requested_position,
                "cv_position": cv_position,
                "ask_for_correct_cv": True,
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Correct CV required: {inbox_email.subject}",
            body,
            scenario="wrong_cv",
            application=application,
        )

    def reply_no_opening(
        self,
        inbox_email: InboxEmail,
        position: str | None,
        application: dict[str, Any] | None = None,
    ):
        role_text = f" for {position}" if position else ""
        fallback_body = recruiter_email_body(
            "Thanks for sharing your profile.",
            f"I do not have an active opening{role_text} at the moment. I have kept your details on file and will reach out if a suitable role opens up.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate shared or asked about a role, but there is no active opening for that position",
            {"active_opening": False, "position": position, "profile_saved": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application update: {inbox_email.subject}",
            body,
            scenario="no_opening",
            application=application,
        )

    def reply_profile_under_review(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        """Sent when the candidate is a near miss and a person is deciding.

        Deliberately not a rejection and not a promise. The alternative was
        telling a relevant candidate we had nothing for them.
        """
        fallback_body = recruiter_email_body(
            "Thanks for sharing your profile.",
            "Your background looks relevant to the kind of work we hire for, so I am reviewing "
            "where it fits best against what we have open right now.",
            "I will come back to you shortly with an update.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate's background is relevant but does not map cleanly to a current opening; "
            "tell them warmly that you are reviewing where they fit and will come back shortly; "
            "do not reject them, do not promise a specific role, and do not ask for anything",
            {"profile_relevant": True, "under_review": True, "ask_for_nothing": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Your application: {inbox_email.subject}",
            body,
            scenario="profile_under_review",
            application=application,
        )

    def reply_india_location_only(self, inbox_email: InboxEmail):
        fallback_body = recruiter_email_body(
            "Thanks for sharing your CV.",
            "At the moment I am hiring only for candidates based in India. The contact details in the CV do not appear to be Indian, so I will not be able to take this application forward right now.",
            "Wishing you all the best in your job search.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate shared a CV, but the contact number appears to be outside India and this hiring is India-only",
            {"india_only_hiring": True, "do_not_proceed": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application update: {inbox_email.subject}",
            body,
            scenario="india_location_only",
        )

    def reply_received(self, inbox_email: InboxEmail, application: dict[str, Any] | None = None):
        fallback_body = recruiter_email_body(
            "Thanks for applying and sharing your CV.",
            "I have received your application and will come back to you shortly with the next step.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate applied for an active role and shared a CV",
            {"application_received": True, "next_step": "under review"},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application received: {inbox_email.subject}",
            body,
            scenario="application_received",
            application=application,
        )

    def reply_screening_questions(
        self,
        inbox_email: InboxEmail,
        requirement: dict[str, Any] | None,
        application: dict[str, Any] | None = None,
    ):
        role = requirement["position_title"] if requirement else None
        fallback_body = recruiter_email_body(
            "Thanks for applying and sharing your CV.",
            f"Your profile looks relevant for {role}. Before we move ahead, please confirm if you are comfortable with night shift, work from office in Mohali, 5 days working, and cab facility available.",
            "Also please share your current salary, expected salary, current location, and how soon you can join.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate CV passed the initial ATS/JD screen; ask pre-interview screening questions",
            {
                "application_received": True,
                "role": role,
                "work_terms": screening_work_terms(),
                "ask_for": ["current salary", "expected salary", "current location", "joining time"],
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application next steps: {inbox_email.subject}",
            body,
            scenario="screening_questions",
            application=application,
        )

    def reply_screening_missing_details(
        self,
        inbox_email: InboxEmail,
        answers: dict[str, Any],
        application: dict[str, Any] | None = None,
    ):
        missing = [
            label
            for key, label in [
                ("current_salary", "current salary"),
                ("expected_salary", "expected salary"),
                ("current_location", "current location"),
                ("comfortable_with_terms", "confirmation that the work terms suit you"),
            ]
            if answers.get(key) in (None, "", [])
        ]
        if joining_days_from_answers(answers) is None:
            missing.append("joining time")
        missing_text = ", ".join(missing) if missing else "a few remaining details"
        fallback_body = recruiter_email_body(
            "Thanks for sharing the details.",
            f"I still need {missing_text} to move your application forward.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate replied to screening questions but some required details are still missing; "
            "ask ONLY for the fields listed as still_missing and acknowledge the ones already received",
            {
                "already_received": {key: value for key, value in (answers or {}).items() if value not in (None, "", [])},
                "still_missing": missing,
                "work_terms": screening_work_terms(),
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Screening details required: {inbox_email.subject}",
            body,
            scenario="screening_missing_details",
            application=application,
        )

    def reply_location_not_fit(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        role = None
        if application:
            role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        role_text = f" for the {role} role" if role else ""
        fallback_body = recruiter_email_body(
            "Thank you for letting us know.",
            f"I understand that you are not ready to relocate to Mohali. Since this opening{role_text} is currently work from office in Mohali, we will not be able to proceed further with your application for this role right now.",
            "We will keep your profile in mind and reach out if we have a suitable opening in your preferred location in the future.",
            "Thank you again for your time.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate is not ready to relocate/work from Mohali for a Mohali work-from-office role; close politely and do not ask for salary details again",
            {
                "role": role,
                "candidate_declined_location": True,
                "office_location": RECRUITER_OFFICE_LOCATION,
                "do_not_ask_more_screening_questions": True,
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application update: {inbox_email.subject}",
            body,
            scenario="location_not_fit",
            application=application,
        )

    def reply_budget_disclosure(self, inbox_email: InboxEmail, application: dict[str, Any]):
        """State the range once and ask a closed question.

        This replaces the old open-ended negotiation, which had no exit state and
        re-asked the same question every time the candidate replied. It is sent
        at most once per application, enforced both here and by the reply ledger.
        """
        budget_text = requirement_budget_text(application)
        fallback_body = recruiter_email_body(
            "Thanks for sharing your details.",
            f"For this role the approved range is {budget_text}. I know that is below the figure "
            "you mentioned, so I wanted to be upfront before we go any further.",
            "Could you let me know if that works for you? A simple yes or no is fine. "
            "If it does not, tell me what you had in mind and I will see what I can do.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "state the approved salary range for this role exactly once and ask the candidate a "
            "direct yes/no question about whether it works for them; do not negotiate, do not "
            "invite an open discussion, and do not mention anyone else being involved",
            {
                "budget": budget_text,
                "candidate_expected_salary": (application or {}).get("screening_expected_salary"),
                "ask_yes_or_no": True,
                "role": (application or {}).get("requirement_position"),
            },
            fallback_body,
        )
        return self.send_candidate_reply(
            inbox_email,
            f"Compensation for this role: {inbox_email.subject}",
            body,
            scenario="budget_disclosure",
            application=application,
        )

    def reply_not_selected(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any],
        is_referral: bool = False,
    ):
        """Tell them the answer, warmly, once the agent has decided.

        No scores, no mention of how the decision was reached, and nothing that
        invites a negotiation - just a clear answer so the candidate can move on
        instead of waiting on a thread that was never going to be picked up.
        """
        role = (application or {}).get("requirement_position")
        fallback_body = recruiter_email_body(
            "Thank you for applying and for taking the time to send your CV.",
            (
                f"I have gone through your profile against what the {role} role needs, and on this "
                "occasion it is not the right fit, so I will not be taking it forward."
                if role
                else "I have gone through your profile carefully, and on this occasion it is not the "
                     "right fit for what we are hiring for, so I will not be taking it forward."
            ),
            "Thank you again for your interest. Do apply again if you see something that suits you "
            "better - I would be glad to take another look.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "tell the candidate warmly and briefly that after reviewing their CV against this role "
            "they have not been shortlisted, thank them, and invite them to apply for future "
            "openings; give no scores and no detailed critique, do not invite a discussion, and do "
            "not mention anyone else being involved or any review still to happen",
            {
                "role": role,
                "final": True,
                "ask_for_nothing": True,
                "referral": bool(is_referral),
            },
            fallback_body,
        )
        return self.send_candidate_reply(
            inbox_email,
            f"Update on your application: {inbox_email.subject}",
            body,
            scenario="not_selected",
            application=application,
        )

    def reply_budget_out_of_range(self, inbox_email: InboxEmail, application: dict[str, Any]):
        """Close the loop kindly when the figures are nowhere near each other.

        No range is quoted and no negotiation is invited, because there is
        nothing to negotiate; the point is that the candidate hears back and is
        not left waiting on a thread nobody is going to answer.
        """
        fallback_body = recruiter_email_body(
            "Thank you for sharing your details and for the time you have put into this.",
            "Having looked at what you are expecting alongside what has been approved for this "
            "role, the two are too far apart for this one to work out.",
            "I would rather tell you now than keep you waiting. Do keep an eye on our openings - "
            "if something closer to your range comes up, I would be glad to hear from you.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "tell the candidate warmly and briefly that their salary expectation is too far above "
            "what is approved for this role for it to work out, thank them, and encourage them to "
            "apply again for a future opening; do not quote figures, do not negotiate, and do not "
            "mention anyone else being involved",
            {
                "role": (application or {}).get("requirement_position"),
                "final": True,
                "ask_for_nothing": True,
            },
            fallback_body,
        )
        return self.send_candidate_reply(
            inbox_email,
            f"Update on your application: {inbox_email.subject}",
            body,
            scenario="budget_out_of_range",
            application=application,
        )

    def reply_holding(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        """One short acknowledgement while a human owns the thread.

        Not silence (which is what candidates got before) and not a negotiation
        (which is what created the race). Sent at most once per application.
        """
        fallback_body = recruiter_email_body(
            "Thanks for getting back to me.",
            "I have everything I need for now and I am looking into it. I will come back to you shortly.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "acknowledge the candidate's reply warmly in one or two sentences and tell them you "
            "will come back to them shortly; ask for nothing, promise no specific date, and do "
            "not mention anyone else being involved",
            {"acknowledge_only": True, "ask_for_nothing": True},
            fallback_body,
        )
        return self.send_candidate_reply(
            inbox_email,
            f"Thanks for your reply: {inbox_email.subject}",
            body,
            scenario="holding_reply",
            application=application,
        )

    def reply_interview_availability_request(self, inbox_email: InboxEmail, application: dict[str, Any] | None = None):
        if application:
            token = self.db.ensure_interview_link(application["id"])
            link = candidate_interview_url(token)
            fallback_body = recruiter_email_body(
                "Thanks for confirming the details.",
                "We are good to move ahead with your interview.",
                f"You can start it here whenever you are ready: {link}",
                "Please use Google Chrome on a laptop or desktop with a working microphone, and choose a quiet place before starting.",
            )
            body = self.ai.draft_reply(
                inbox_email,
                "candidate screening details are acceptable; send candidate the AI interview link",
                {
                    "ready_for_interview": True,
                    "application": application_prompt_facts(application),
                    "interview_link": link,
                },
                fallback_body,
            )
            sent = self.send_candidate_reply(
                inbox_email,
                f"Interview link: {inbox_email.subject}",
                body,
                scenario="interview_link",
                application=application,
            )
            if sent:
                # Explicit transition. Previously this only happened as a side
                # effect of minting a token, so a resent link left the status
                # unchanged and the candidate looped forever.
                self.db.mark_interview_link_sent(application["id"])
                self.db.log_email_event(
                    inbox_email,
                    "interview_link_sent",
                    {"application_id": application["id"], "interview_link": link},
                )
            return
        fallback_body = recruiter_email_body(
            "Thanks for confirming the details.",
            "We are good to move ahead with an interview. Please share a few time slots when you will be available, and I will schedule it accordingly.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate screening details are acceptable; ask candidate for interview availability",
            {"ready_for_interview": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Interview availability: {inbox_email.subject}",
            body,
            scenario="interview_availability_request",
        )

    def escalate_to_hr(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any],
        answers: dict[str, Any],
        issues: list[str],
        candidate_reply: str | None = None,
    ):
        """Hand the application to a human and stop candidate-facing automation.

        This used to email HR *and* invite the candidate to keep negotiating,
        which is what created the race: whichever side answered first silently
        decided the outcome. Now the candidate gets one holding note and nothing
        else until a person acts.
        """
        application_id = application["id"]
        dashboard_url = dashboard_application_url(application_id)
        reason = "; ".join(issues)
        subject = f"HR approval required: {application.get('full_name') or application.get('candidate_email') or application_id}"
        body = recruiter_email_body(
            "Please review this candidate application.",
            f"The candidate's screening reply needs HR approval: {reason}.",
            f"Application: {dashboard_url}",
            f"Candidate email: {application.get('candidate_email') or application.get('source_email') or '-'}",
            f"Role: {application.get('requirement_position') or application.get('matched_position') or application.get('detected_position') or '-'}",
            f"Screening details: {json.dumps(answers, default=str)}",
            f"Candidate's latest reply: {candidate_reply}" if candidate_reply else "",
        )
        self.notify_hr_rate_limited(inbox_email, application, subject, body, "hr_escalation_notice")
        self.db.update_application_screening(application_id, "hr_escalated", answers, reason)
        self.db.log_email_event(
            inbox_email,
            "hr_escalated",
            {
                "application_id": application_id,
                "issues": issues,
                "answers": answers,
                "dashboard_url": dashboard_url,
                "candidate_reply": (candidate_reply or "")[:1000],
            },
        )
        trace_recruiter_event(
            "hr_escalated",
            inputs=inbox_email_summary(inbox_email),
            outputs={
                "application_id": application_id,
                "issues": issues,
                "next_action": "hold_until_human_acts",
            },
        )
        self.reply_holding(inbox_email, application)

    def notify_hr_rate_limited(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any] | None,
        subject: str,
        body: str,
        scenario: str,
        hours: int = 24,
    ):
        """One HR email per application per day; further replies land in the dashboard.

        Without this, every candidate follow-up on an escalated thread produced
        another "manual review needed" email.
        """
        application_id = (application or {}).get("id")
        recipients = hr_notification_recipients()
        key_recipient = recipients[0] if recipients else RECRUITER_HR_ESCALATION_EMAIL
        if self.db.sent_reply_count(application_id, key_recipient, scenario, hours=hours) > 0:
            log_json(
                logging.INFO,
                "hr_notification_rate_limited",
                **inbox_email_summary(inbox_email),
                scenario=scenario,
                application_id=application_id,
            )
            return
        send_hr_notification(
            self.mailer if hasattr(self.mailer, "send_direct_email") else RecruiterMailer(),
            subject,
            body,
        )
        self.db.record_sent_reply(application_id, key_recipient, scenario, body)

    def handle_interview_availability_reply(self, inbox_email: InboxEmail, application: dict[str, Any]) -> bool:
        schedule = self.ai.extract_interview_schedule(inbox_email, application)
        availability = schedule.get("availability_text") or inbox_email.body.strip()
        scheduled_at = parse_iso_datetime(schedule.get("scheduled_at"))
        if not scheduled_at:
            scheduled_at = parse_interview_datetime_fallback(inbox_email.body)
        flexible_availability = any(
            phrase in normalize_position_text(inbox_email.body)
            for phrase in [
                "anytime",
                "any time",
                "as per your convenience",
                "your convenience",
                "you can schedule",
                "schedule it",
            ]
        )
        scheduled_at = coerce_final_hr_slot(scheduled_at, flexible=flexible_availability and not scheduled_at)
        if not scheduled_at:
            self.db.update_interview_schedule(
                application["id"],
                "interview_time_requested",
                availability=availability,
            )
            self.send_candidate_reply(
                inbox_email,
                f"Interview timing required: {inbox_email.subject}",
                self.ai.draft_reply(
                    inbox_email,
                    "candidate replied about interview but did not provide a clear date and time; ask for clear slots",
                    {"availability_text": availability, "needs_clear_datetime": True},
                    recruiter_email_body(
                        "Thanks for sharing this.",
                        "Could you please share a clear date and time slot for the interview? Once I have that I will confirm it and send the Teams link.",
                    ),
                ),
                scenario="interview_timing_required",
                application=application,
            )
            return True

        duration_minutes = safe_int(schedule.get("duration_minutes")) or FINAL_HR_DEFAULT_DURATION_MINUTES
        end_at = scheduled_at + timedelta(minutes=duration_minutes)
        recipient = application_candidate_recipient(application) or inbox_email.sender
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        subject = f"Interview - {role or 'Candidate'}"
        interviewer = choose_final_hr_interviewer(self.db, scheduled_at, application.get("hr_interviewer_email"))
        event_body = recruiter_email_body(
            "Your interview has been scheduled.",
            f"Role: {role or 'Candidate'}",
            f"Time: {recruiter_time_text(scheduled_at)}",
            f"Interviewer: {interviewer['name']}",
        )
        teams_event_id = ""
        teams_join_url = ""
        old_teams_event_id = application.get("teams_event_id")
        try:
            if isinstance(self.mailer, MicrosoftGraphProvider):
                calendar_provider = MicrosoftGraphProvider(interviewer["email"])
                teams_event_id, teams_join_url = calendar_provider.create_teams_calendar_event(
                    recipient,
                    subject,
                    scheduled_at,
                    end_at,
                    event_body,
                )
                if not teams_join_url:
                    teams_join_url = calendar_provider.create_online_meeting(subject, scheduled_at, end_at)
                if teams_event_id and old_teams_event_id and old_teams_event_id != teams_event_id:
                    old_calendar_provider = MicrosoftGraphProvider(application.get("hr_interviewer_email") or interviewer["email"])
                    cancelled = old_calendar_provider.cancel_calendar_event(
                        old_teams_event_id,
                        f"This interview has been rescheduled to {recruiter_time_text(scheduled_at)}.",
                    )
                    self.db.log_email_event(
                        inbox_email,
                        "previous_teams_meeting_cancelled" if cancelled else "previous_teams_meeting_not_found",
                        {
                            "application_id": application["id"],
                            "old_teams_event_id": old_teams_event_id,
                            "new_teams_event_id": teams_event_id,
                            "scheduled_at": scheduled_at.isoformat(),
                        },
                    )
        except Exception as exc:
            self.db.log_email_event(
                inbox_email,
                "teams_meeting_create_failed",
                {"application_id": application["id"], "recipient": recipient, "error": str(exc), "schedule": schedule},
            )

        status = "interview_scheduled" if teams_join_url else "interview_availability_received"
        self.db.update_interview_schedule(
            application["id"],
            status,
            availability=availability,
            scheduled_at=scheduled_at,
            teams_event_id=teams_event_id or None,
            teams_join_url=teams_join_url or None,
            interviewer_email=interviewer["email"],
            interviewer_name=interviewer["name"],
        )
        if teams_join_url:
            fallback = recruiter_email_body(
                "Thanks for sharing your availability.",
                f"Your interview has been scheduled for {recruiter_time_text(scheduled_at)}.",
                f"You will be speaking with {interviewer['name']}.",
                f"Teams link: {teams_join_url}",
            )
            scenario = "Teams interview has been scheduled; share the meeting link"
            facts = {
                "scheduled_at": scheduled_at.isoformat(),
                "teams_join_url": teams_join_url,
                "interviewer": interviewer,
                "allowed_window": "Monday-Friday, 6 PM to 1 AM IST",
            }
        else:
            fallback = recruiter_email_body(
                "Thanks for sharing your availability.",
                "We have noted the timing and will share the Teams link shortly.",
            )
            scenario = "candidate shared clear interview availability, but Teams link is not created yet"
            facts = {"scheduled_at": scheduled_at.isoformat(), "teams_link_pending": True}
        self.send_candidate_reply(
            inbox_email,
            f"Interview schedule: {inbox_email.subject}",
            self.ai.draft_reply(inbox_email, scenario, facts, fallback),
            scenario="interview_scheduled",
            application=application,
        )
        return True

    def reply_referral_received(self, inbox_email: InboxEmail, candidate_email: str, position: str | None):
        role_text = f" for {position}" if position else ""
        fallback_body = recruiter_email_body(
            f"Your CV has been shared with us{role_text}.",
            "I have received it and will review your profile. If it matches the role, I will come back to you with the next steps.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "someone referred or shared a candidate CV; reply directly to the candidate email from the CV",
            {"candidate_email": candidate_email, "position": position, "application_received": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application received: {inbox_email.subject}",
            body,
            scenario="referral_received",
            to_email=candidate_email,
        )

    def reply_referral_missing_candidate_email(self, inbox_email: InboxEmail, position: str | None):
        role_text = f" for {position}" if position else ""
        fallback_body = recruiter_email_body(
            f"Thanks for sharing your friend's CV{role_text}.",
            "I could not find the candidate's email address in the CV. Please share it, or ask them to send the CV directly, so I can continue the review.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "sender shared a friend's CV, but candidate email is missing from the CV",
            {"position": position, "candidate_email_required": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Candidate email required: {inbox_email.subject}",
            body,
            scenario="referral_missing_candidate_email",
        )

    def reply_status_followup(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        if application:
            position = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
            position_text = f" for {position}" if position else ""
            status = (application.get("application_status") or "").lower()
            if status in {"interview_link_sent", "interview_started"} and not application.get("interview_completed_at"):
                token = application.get("interview_link_token")
                link = candidate_interview_url(token) if token else None
                fallback_lines = [
                    "Thanks for following up.",
                    f"Your AI interview{position_text} is still pending.",
                ]
                if link:
                    fallback_lines.append(f"You can complete it here: {link}")
                fallback_lines.extend(
                    [
                        "Please use Google Chrome on a laptop or desktop, with a working microphone and a quiet place.",
                        "Once you complete it, I will review your interview and get back to you with the next update.",
                    ]
                )
                fallback_body = recruiter_email_body(*fallback_lines)
                body = self.ai.draft_reply(
                    inbox_email,
                    "candidate is asking for an update while their AI interview is pending; share the existing interview link and do not create a new link",
                    {
                        "application_found": True,
                        "position": position,
                        "status": application.get("application_status"),
                        "interview_pending": True,
                        "interview_link": link,
                        "reminder_policy": "only one automatic reminder is sent after 24 hours if the interview remains incomplete",
                    },
                    fallback_body,
                )
                self.send_candidate_reply(
                    inbox_email,
                    f"AI interview pending: {inbox_email.subject}",
                    body,
                    scenario="interview_pending_reminder",
                    application=application,
                )
                return
            fallback_body = recruiter_email_body(
                "Thanks for following up.",
                f"I have your application{position_text} and it is still under review. I will come back to you as soon as I have an update.",
            )
            body = self.ai.draft_reply(
                inbox_email,
                "candidate is asking for an update on an application you already have",
                {"application_found": True, "position": position, "status": application.get("application_status")},
                fallback_body,
            )
            self.send_candidate_reply(
                inbox_email,
                f"Application status: {inbox_email.subject}",
                body,
                scenario="status_followup",
                application=application,
            )
        else:
            fallback_body = recruiter_email_body(
                "Thanks for following up.",
                "I could not find an earlier application linked to this email address. Please share your CV and the role you are interested in, and I will review it.",
            )
            body = self.ai.draft_reply(
                inbox_email,
                "candidate is asking for an update, but you have no earlier application from this address",
                {"application_found": False, "ask_for_cv_and_role": True},
                fallback_body,
            )
            self.send_candidate_reply(
                inbox_email,
                f"Application status: {inbox_email.subject}",
                body,
                scenario="status_followup_no_application",
            )

    def reply_interview_delay_acknowledged(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any],
        asked_for_new_link: bool = False,
    ):
        position = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        position_text = f" for the {position} role" if position else ""
        token = application.get("interview_link_token")
        link = candidate_interview_url(token) if token else None
        lines = [
            "No problem, thank you for letting me know.",
            f"You can complete the AI interview{position_text} whenever you are available over the next few days.",
        ]
        if link and asked_for_new_link:
            # He asked for a new one; the honest answer is that he does not need
            # one. Issuing a fresh token would invalidate the link he already has.
            lines.append(
                f"You do not need a new link - the one already sent to you still works: {link}"
            )
        elif link:
            lines.append(f"The same link will remain active: {link}")
        lines.append("Once you complete it, I will review it and get back to you with the next update.")
        fallback_body = recruiter_email_body(*lines)
        body = self.ai.draft_reply(
            inbox_email,
            "candidate says they will complete the pending AI interview later, and may have asked "
            "for a new link; acknowledge warmly, tell them plainly that the link they already have "
            "still works and no new one is needed, keep it active, and do not pressure them",
            {
                "application_id": application.get("id"),
                "position": position,
                "interview_pending": True,
                "interview_link": link,
                "asked_for_new_link": asked_for_new_link,
                "same_link_still_valid": True,
                "automatic_reminder_suppressed": True,
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"AI interview link: {inbox_email.subject}",
            body,
            scenario="interview_delay_acknowledged",
            application=application,
        )

    def reply_withdrawal_confirmed(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        position = None
        if application:
            position = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        position_text = f" for {position}" if position else ""
        fallback_body = recruiter_email_body(
            "Thanks for letting me know.",
            f"I have marked your application{position_text} as withdrawn. Wishing you all the best.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate wants to withdraw their application",
            {"application_found": application is not None, "position": position, "withdrawn": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Application withdrawn: {inbox_email.subject}",
            body,
            scenario="withdrawal_confirmed",
            application=application,
        )

    def reply_unreadable_cv(self, inbox_email: InboxEmail, filename: str):
        """The file was a CV by every outward sign, but held no readable text.

        Almost always a scan or a photo of a printout. Without this the
        candidate simply never heard back.
        """
        display = attachment_display_name(filename) or "the file"
        fallback_body = recruiter_email_body(
            "Thanks for sending your CV.",
            f"I could not read any text from {display} - it looks like a scanned image "
            "rather than a text document, so nothing came through on my side.",
            "Could you send it again as a PDF exported from Word or Google Docs, or as a "
            "DOCX? As soon as I can read it I will review your profile.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "the candidate attached what looks like a CV but it contains no readable text, "
            "most likely a scan or photo; ask warmly for a text-based PDF or DOCX and do "
            "not suggest anything is wrong with their application",
            {
                "attachment_name": display,
                "unreadable_attachment": True,
                "supported_cv_formats": ["PDF exported from a document editor", "DOCX", "TXT"],
            },
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"Could not read your CV: {inbox_email.subject}" if inbox_email.subject else "Could not read your CV",
            body,
            scenario="unreadable_cv",
        )

    def reply_supported_cv_required(self, inbox_email: InboxEmail):
        fallback_body = recruiter_email_body(
            "Thanks for your message.",
            "I could not open the attachment you sent. Please send the CV as a PDF, DOCX, or TXT file and I will review it.",
        )
        body = self.ai.draft_reply(
            inbox_email,
            "candidate sent an attachment, but it was not a supported/readable CV file",
            {"supported_cv_formats": ["PDF", "DOCX", "TXT"], "ask_for_readable_cv": True},
            fallback_body,
        )
        self.send_candidate_reply(
            inbox_email,
            f"CV attachment required: {inbox_email.subject}",
            body,
            scenario="unsupported_attachment",
        )

    def route_completed_screening(
        self,
        inbox_email: InboxEmail,
        application: dict[str, Any],
        answers: dict[str, Any],
    ) -> bool:
        """Decide what happens once all screening answers are in.

        Exactly three outcomes, and every failure mode has an exit:
          fit                       -> interview
          budget gap, not yet told  -> state the range once
          anything else             -> a human decides

        The old code had a fourth path that re-entered "screening_negotiation"
        forever whenever the blocker was the work terms rather than salary.
        """
        application_id = application["id"]
        is_fit, issues = screening_fit(answers, application)
        if is_fit:
            self.db.update_application_screening(application_id, "interview_link_pending", answers)
            self.reply_interview_availability_request(inbox_email, application)
            return True

        issue_kind = screening_issue_kind(issues)
        already_disclosed = bool(application.get("budget_disclosed_at")) or self.db.scenario_ever_sent(
            application_id,
            inbox_email.sender,
            "budget_disclosure",
        )

        gap_ratio = budget_gap_ratio(answers, application)
        if (
            issue_kind == "budget"
            and not already_disclosed
            and BUDGET_GAP_MAX_RATIO > 0
            and gap_ratio is not None
            and BUDGET_GAP_MAX_RATIO < gap_ratio <= BUDGET_IMPLAUSIBLE_GAP_RATIO
        ):
            # Too far apart to be worth stating the range and asking. Close it
            # here rather than spending a disclosure, a reply and then a human.
            log_json(
                logging.INFO,
                "budget_gap_beyond_disclosure",
                **inbox_email_summary(inbox_email),
                application_id=application_id,
                expected_salary=answers.get("expected_salary"),
                budget_max=application.get("budget_max"),
                gap_ratio=round(gap_ratio, 2),
            )
            self.db.log_email_event(
                inbox_email,
                "budget_gap_beyond_disclosure",
                {
                    "application_id": application_id,
                    "expected_salary": answers.get("expected_salary"),
                    "budget_max": application.get("budget_max"),
                    "gap_ratio": round(gap_ratio, 2),
                },
            )
            self.db.update_application_screening(
                application_id,
                "rejected",
                {**answers, "issues": issues, "rejection_reason": "expectation far above the approved range"},
            )
            self.reply_budget_out_of_range(inbox_email, application)
            return True

        if issue_kind == "budget" and not already_disclosed:
            self.db.mark_budget_disclosed(application_id, {**answers, "issues": issues})
            self.db.log_email_event(
                inbox_email,
                "budget_disclosed",
                {
                    "application_id": application_id,
                    "issues": issues,
                    "budget_max": application.get("budget_max"),
                    "candidate_expected_salary": answers.get("expected_salary"),
                },
            )
            trace_recruiter_event(
                "budget_disclosed",
                inputs=inbox_email_summary(inbox_email),
                outputs={"application_id": application_id, "next_action": "await_yes_or_no"},
            )
            self.reply_budget_disclosure(inbox_email, application)
            return True

        self.escalate_to_hr(
            inbox_email,
            application,
            answers,
            issues,
            candidate_reply=latest_reply_text(inbox_email.body)[:1500],
        )
        return True

    def handle_budget_response(self, inbox_email: InboxEmail, application: dict[str, Any]) -> bool:
        """Classify the answer to the one budget question, then act once.

        Intent is read from the latest reply only. The previous implementation
        scanned for any number below the ceiling, so "my notice period is 90
        days" counted as accepting the salary and skipped HR entirely.
        """
        application_id = application["id"]
        answers = json_dict(application.get("screening_details"))
        latest = latest_reply_text(inbox_email.body)

        decision = self.ai.classify_budget_response(inbox_email, application)
        response = str(decision.get("response") or "").lower().strip()
        if response not in {"accepts", "rejects", "counter", "unclear"}:
            response = deterministic_budget_signal(latest, application) or "unclear"

        counter_amount = score_number(decision.get("counter_amount"))
        evidence = str(decision.get("evidence") or "")[:500]
        answers = {
            **answers,
            "budget_response": response,
            "budget_response_evidence": evidence,
        }
        if counter_amount is not None:
            answers["budget_counter_amount"] = counter_amount

        log_json(
            logging.INFO,
            "budget_response_classified",
            **inbox_email_summary(inbox_email),
            application_id=application_id,
            response=response,
            counter_amount=counter_amount,
        )
        trace_recruiter_event(
            "budget_response_classified",
            inputs=inbox_email_summary(inbox_email),
            outputs={
                "application_id": application_id,
                "response": response,
                "counter_amount": counter_amount,
                "evidence": evidence,
            },
        )
        self.db.log_email_event(
            inbox_email,
            "budget_response_classified",
            {
                "application_id": application_id,
                "response": response,
                "counter_amount": counter_amount,
                "evidence": evidence,
                "latest_reply": latest[:1000],
            },
        )

        if response == "accepts":
            # The candidate's own expected salary is preserved. Only the fact of
            # acceptance is recorded; the figure they stated is never overwritten
            # with the budget ceiling the way it used to be.
            answers["accepted_budget"] = True
            budget_max = score_number(application.get("budget_max"))
            if budget_max is not None:
                answers["agreed_salary"] = budget_max
            self.db.record_budget_response(application_id, response, answers)
            self.db.update_application_screening(application_id, "interview_link_pending", answers)
            self.reply_interview_availability_request(inbox_email, application)
            return True

        if response == "unclear":
            clarified_before = self.db.sent_reply_count(
                application_id,
                inbox_email.sender,
                "budget_clarification",
                hours=24 * 365,
            )
            if not clarified_before:
                self.db.record_budget_response(application_id, response, answers)
                fallback_body = recruiter_email_body(
                    "Thanks for getting back to me.",
                    f"Just to confirm before I take the next step: does the range of "
                    f"{requirement_budget_text(application)} work for you? A yes or no is all I need.",
                )
                body = self.ai.draft_reply(
                    inbox_email,
                    "the candidate did not clearly answer whether the stated salary range works "
                    "for them; ask that one question again in a single short sentence and ask "
                    "nothing else",
                    {"budget": requirement_budget_text(application), "ask_yes_or_no": True},
                    fallback_body,
                )
                self.send_candidate_reply(
                    inbox_email,
                    f"Quick confirmation: {inbox_email.subject}",
                    body,
                    scenario="budget_clarification",
                    application=application,
                )
                return True

        reason = {
            "rejects": "candidate declined the stated budget",
            "counter": f"candidate countered at {counter_amount}" if counter_amount else "candidate proposed a different figure",
            "unclear": "candidate did not give a clear answer on the budget after being asked twice",
        }.get(response, "candidate response to the budget needs a human decision")
        self.db.record_budget_response(application_id, response, answers)
        self.escalate_to_hr(inbox_email, application, answers, [reason], candidate_reply=latest[:1500])
        return True

    @traceable(name="process_recruiting_email")
    def process_email(self, inbox_email: InboxEmail) -> bool:
        started_at = time.monotonic()
        # Bound up front because the follow-up path reads these before the
        # attachment loop that assigns them, and Python makes them locals for
        # the whole method. That was 8 UnboundLocalError crashes in a fortnight,
        # each one an email the agent silently never answered.
        cv_role_summary: dict[str, Any] = {}
        extracted: dict[str, Any] = {}
        cv_text: str = ""
        log_json(logging.INFO, "email_processing_started", **inbox_email_summary(inbox_email))
        trace_recruiter_event(
            "email_processing_started",
            inputs=inbox_email_summary(inbox_email),
        )
        thread_context = build_thread_context(inbox_email)

        # A colleague replying from the shared mailbox leaves no status behind,
        # so this is checked before anything else. Once a human has spoken in a
        # thread, the agent stays out of it permanently.
        if self.thread_taken_over_by_human(inbox_email):
            handover_application = self.db.latest_application_for_inbox_email(inbox_email)
            if handover_application:
                self.db.mark_human_handled(
                    handover_application["id"],
                    "A team member replied to this thread directly.",
                )
            log_json(
                logging.INFO,
                "human_takeover_detected",
                **inbox_email_summary(inbox_email),
                application_id=handover_application["id"] if handover_application else None,
            )
            trace_recruiter_event(
                "human_takeover_detected",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": handover_application["id"] if handover_application else None,
                    "next_action": "no_auto_reply_ever",
                },
                tags=["guardrail"],
            )
            self.db.log_email_event(
                inbox_email,
                "human_takeover_no_auto_reply",
                {
                    "application_id": handover_application["id"] if handover_application else None,
                    "no_candidate_reply_sent": True,
                },
            )
            return True

        classification = self.ai.classify_email(inbox_email)
        thread_application = self.db.latest_application_for_thread(inbox_email)
        latest_application = thread_application or self.db.latest_application_for_email(inbox_email.sender)
        active_application = (
            self.db.application_with_requirement(latest_application["id"])
            if latest_application
            else None
        )
        application_lookup_scope = "thread" if thread_application else "sender" if latest_application else None
        if active_application and not thread_application and classification_has_specific_role(classification):
            active_role = (
                active_application.get("requirement_position")
                or active_application.get("matched_position")
                or active_application.get("detected_position")
            )
            requested_role = classification.get("detected_position")
            if active_role and requested_role and not roles_are_compatible(active_role, requested_role, thread_context):
                log_json(
                    logging.INFO,
                    "sender_application_match_ignored_role_conflict",
                    **inbox_email_summary(inbox_email),
                    application_id=active_application["id"],
                    active_role=active_role,
                    requested_role=requested_role,
                )
                trace_recruiter_event(
                    "sender_application_match_ignored_role_conflict",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "application_id": active_application["id"],
                        "active_role": active_role,
                        "requested_role": requested_role,
                        "next_action": "process_as_new_thread",
                    },
                )
                latest_application = None
                active_application = None
                application_lookup_scope = None
        log_json(
            logging.INFO,
            "email_classified",
            **inbox_email_summary(inbox_email),
            classification=classification,
            latest_application_id=latest_application["id"] if latest_application else None,
            application_lookup_scope=application_lookup_scope,
            active_application_status=active_application.get("application_status") if active_application else None,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        trace_recruiter_event(
            "email_classified",
            inputs=inbox_email_summary(inbox_email),
            outputs={
                "classification": classification,
                "latest_application_id": latest_application["id"] if latest_application else None,
                "application_lookup_scope": application_lookup_scope,
                "active_application_status": active_application.get("application_status") if active_application else None,
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            },
        )
        if inbox_email.attachments and any(is_cv_attachment(filename, payload) for filename, payload in inbox_email.attachments):
            if not classification.get("is_employment_related"):
                classification = {
                    **classification,
                    "is_employment_related": True,
                    "latest_intent": "submitting_cv",
                    "reason": (
                        "Readable CV attachment is present, so the email should be processed "
                        "as a recruiting CV submission even though subject/body are empty."
                    ),
                }
                log_json(
                    logging.INFO,
                    "classification_overridden_by_cv_attachment",
                    **inbox_email_summary(inbox_email),
                    classification=classification,
                )
                trace_recruiter_event(
                    "classification_overridden_by_cv_attachment",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={"classification": classification},
                )

        # A human owns this application. Store the reply, tell HR at most once a
        # day, and send the candidate one holding note. No screening, no status
        # changes, no negotiation - that parallel track is what created the race
        # between HR approving and the candidate replying.
        if active_application and (active_application.get("application_status") or "").lower() in HUMAN_HOLD_STATUSES:
            status = (active_application.get("application_status") or "").lower()
            budget_signal = deterministic_budget_signal(inbox_email.body, active_application)
            self.db.log_email_event(
                inbox_email,
                "candidate_reply_while_on_hold",
                {
                    **classification,
                    "application_id": active_application["id"],
                    "application_status": status,
                    "budget_signal": budget_signal,
                    "latest_reply": latest_reply_text(inbox_email.body)[:1000],
                },
            )
            trace_recruiter_event(
                "candidate_reply_while_on_hold",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": active_application["id"],
                    "status": status,
                    "budget_signal": budget_signal,
                    "next_action": "hold_for_human",
                },
            )
            if status != "human_handled":
                hint = ""
                if budget_signal == "accepts":
                    hint = " The reply reads like the candidate accepting the stated budget."
                elif budget_signal == "rejects":
                    hint = " The reply reads like the candidate declining the stated budget."
                self.notify_hr_rate_limited(
                    inbox_email,
                    active_application,
                    f"Candidate replied while awaiting your decision: {inbox_email.sender}",
                    recruiter_email_body(
                        "This application is waiting on a decision from you and the candidate has replied.",
                        f"Candidate: {inbox_email.sender}",
                        f"Status: {status}",
                        f"Application: {dashboard_application_url(active_application['id'])}",
                        f"Their message:{hint}",
                        latest_reply_text(inbox_email.body)[:1500] or "-",
                    ),
                    "hold_reply_notice",
                )
                self.reply_holding(inbox_email, active_application)
            return True

        # Courtesy notes need no answer. Replying to "Thanks, I will do that" is
        # what made the agent feel relentless and gave candidates no way out.
        if is_pure_acknowledgement(inbox_email.body) and not inbox_email.attachments:
            log_json(
                logging.INFO,
                "acknowledgement_no_reply",
                **inbox_email_summary(inbox_email),
                application_id=active_application["id"] if active_application else None,
            )
            trace_recruiter_event(
                "acknowledgement_no_reply",
                inputs=inbox_email_summary(inbox_email),
                outputs={"next_action": "no_reply"},
            )
            self.db.log_email_event(
                inbox_email,
                "acknowledgement_no_reply",
                {
                    **classification,
                    "application_id": active_application["id"] if active_application else None,
                    "no_candidate_reply_sent": True,
                },
            )
            return True

        if active_application and is_final_agent_status(active_application.get("application_status")):
            trace_recruiter_event(
                "final_status_no_auto_reply",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": active_application["id"],
                    "status": active_application.get("application_status"),
                    "next_action": "manual_hr_review_notification",
                },
            )
            self.db.log_email_event(
                inbox_email,
                "final_status_no_auto_reply",
                {
                    **classification,
                    "application_id": active_application["id"],
                    "status": active_application.get("application_status"),
                    "no_candidate_reply_sent": True,
                },
            )
            if (active_application.get("application_status") or "").lower() != "manual_hr_review":
                if not message_needs_human_attention(inbox_email.body):
                    # Nothing was asked. The thread is closed and the candidate
                    # has been told; there is nothing for a person to do.
                    log_json(
                        logging.INFO,
                        "final_status_reply_needs_nobody",
                        **inbox_email_summary(inbox_email),
                        application_id=active_application["id"],
                        status=active_application.get("application_status"),
                    )
                    self.db.log_email_event(
                        inbox_email,
                        "final_status_reply_needs_nobody",
                        {
                            "application_id": active_application["id"],
                            "status": active_application.get("application_status"),
                        },
                    )
                else:
                    self.notify_hr_rate_limited(
                        inbox_email,
                        active_application,
                        f"Candidate replied after a final update: {inbox_email.sender}",
                        recruiter_email_body(
                            "This application is closed and the candidate has written back with a question.",
                            f"Candidate: {inbox_email.sender}",
                            f"Current status: {active_application.get('application_status')}",
                            f"Application: {dashboard_application_url(active_application['id'])}",
                            "Their message:",
                            latest_reply_text(inbox_email.body)[:1200] or "-",
                        ),
                        "final_status_handoff",
                    )
            return True

        if self.db.recent_event_count(
            inbox_email.sender,
            [
                "manual_hr_review_requested",
                "final_status_handoff_to_hr",
                "repeated_missing_cv_handoff_to_hr",
                "repeated_unclear_query_handoff_to_hr",
            ],
            hours=72,
        ):
            trace_recruiter_event(
                "manual_review_thread_no_auto_reply",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": active_application["id"] if active_application else None,
                    "next_action": "no_auto_reply",
                },
            )
            self.db.log_email_event(
                inbox_email,
                "manual_review_thread_no_auto_reply",
                {
                    **classification,
                    "application_id": active_application["id"] if active_application else None,
                    "no_candidate_reply_sent": True,
                },
            )
            return True

        if is_withdrawal_request(inbox_email.body):
            withdrawn_application = latest_application
            if withdrawn_application:
                self.db.execute(
                    """
                    UPDATE recruiter_applications
                    SET application_status = 'withdrawn'
                    WHERE id = %s
                    """,
                    (withdrawn_application["id"],),
                )
                withdrawn_application["application_status"] = "withdrawn"
            else:
                withdrawn_application = self.db.mark_latest_application_withdrawn(inbox_email.sender)
            trace_recruiter_event(
                "withdrawal_request",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_found": withdrawn_application is not None,
                    "application_id": withdrawn_application["id"] if withdrawn_application else None,
                },
            )
            self.db.log_email_event(
                inbox_email,
                "withdrawal_request",
                {
                    **classification,
                    "application_found": withdrawn_application is not None,
                    "application_id": withdrawn_application["id"] if withdrawn_application else None,
                },
            )
            self.reply_withdrawal_confirmed(inbox_email, withdrawn_application)
            return True

        if (
            active_application
            and not inbox_email.attachments
            and (active_application.get("application_status") or "").lower() in {"interview_link_sent", "interview_started"}
            and not active_application.get("interview_completed_at")
            and (
                is_interview_delay_reply(inbox_email.body)
                or wants_a_fresh_interview_link(inbox_email.body)
            )
        ):
            self.db.mark_interview_reminder_handled(active_application["id"])
            self.db.log_email_event(
                inbox_email,
                "interview_delay_acknowledged",
                {
                    **classification,
                    "application_id": active_application["id"],
                    "automatic_reminder_suppressed": True,
                },
            )
            trace_recruiter_event(
                "interview_delay_acknowledged",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": active_application["id"],
                    "automatic_reminder_suppressed": True,
                    "next_action": "reply_with_same_interview_link",
                },
            )
            self.reply_interview_delay_acknowledged(
                inbox_email,
                active_application,
                asked_for_new_link=wants_a_fresh_interview_link(inbox_email.body),
            )
            return True

        if active_application and not inbox_email.attachments:
            current_status = (active_application.get("application_status") or "").lower()

            # The candidate is answering the one budget question we asked.
            # Exactly one of three things happens: proceed, escalate, or a single
            # clarification. There is no state to loop back into.
            if current_status == "budget_disclosed":
                return self.handle_budget_response(inbox_email, active_application)

            if current_status in {"screening_questions_sent", "screening_under_review"}:
                extracted_answers = self.ai.extract_screening_answers(inbox_email, active_application)
                answers = merge_screening_answers(active_application.get("screening_details"), extracted_answers)
                if candidate_declined_required_location(inbox_email.body) or extracted_answers.get("comfortable_with_terms") is False:
                    answers = {**answers, "comfortable_with_terms": False}
                    reason = "candidate declined Mohali/work-from-office location terms"
                    self.db.update_application_screening(
                        active_application["id"],
                        "screening_location_not_fit",
                        {**answers, "issues": [reason]},
                    )
                    self.db.log_email_event(
                        inbox_email,
                        "screening_location_not_fit",
                        {
                            "application_id": active_application["id"],
                            "extracted_answers": extracted_answers,
                            "merged_answers": answers,
                            "reason": reason,
                            "latest_reply": latest_reply_text(inbox_email.body)[:1000],
                        },
                    )
                    log_json(
                        logging.INFO,
                        "screening_location_not_fit",
                        **inbox_email_summary(inbox_email),
                        application_id=active_application["id"],
                        reason=reason,
                    )
                    trace_recruiter_event(
                        "screening_location_not_fit",
                        inputs=inbox_email_summary(inbox_email),
                        outputs={
                            "application_id": active_application["id"],
                            "reason": reason,
                            "next_action": "reply_location_not_fit",
                        },
                    )
                    self.reply_location_not_fit(inbox_email, active_application)
                    return True
                self.db.log_email_event(
                    inbox_email,
                    "screening_reply_received",
                    {
                        "application_id": active_application["id"],
                        "extracted_answers": extracted_answers,
                        "merged_answers": answers,
                    },
                )
                if not screening_answers_complete(answers):
                    self.db.update_application_screening(active_application["id"], "screening_questions_sent", answers)
                    self.reply_screening_missing_details(inbox_email, answers, active_application)
                    return True

                return self.route_completed_screening(inbox_email, active_application, answers)

            if current_status in {"interview_time_requested", "interview_link_pending"}:
                self.reply_interview_availability_request(inbox_email, active_application)
                return True

            if current_status == "hr_round_time_requested":
                self.handle_interview_availability_reply(inbox_email, active_application)
                return True

        if not classification.get("is_employment_related"):
            trace_recruiter_event(
                "ignored_non_employment",
                inputs=inbox_email_summary(inbox_email),
                outputs={"classification": classification, "next_action": "leave_unread"},
            )
            self.db.log_email_event(inbox_email, "ignored_non_employment", classification)
            return False

        requirements = self.db.open_requirements()
        is_referral = is_referral_thread(thread_context)
        use_cv_role_override = is_cv_role_override_request(inbox_email.body, thread_context)
        log_json(
            logging.INFO,
            "open_requirements_loaded",
            **inbox_email_summary(inbox_email),
            open_requirement_count=len(requirements),
            open_requirements=[row.get("position_title") for row in requirements],
            is_referral=is_referral,
            use_cv_role_override=use_cv_role_override,
        )
        trace_recruiter_event(
            "open_requirements_loaded",
            inputs=inbox_email_summary(inbox_email),
            outputs={
                "open_requirement_count": len(requirements),
                "open_requirements": [row.get("position_title") for row in requirements],
                "is_referral": is_referral,
                "use_cv_role_override": use_cv_role_override,
            },
        )

        cv_attachments = [
            (attachment_display_name(filename), payload)
            for filename, payload in inbox_email.attachments
            if is_cv_attachment(filename, payload)
        ]
        log_json(
            logging.INFO,
            "cv_attachments_detected",
            **inbox_email_summary(inbox_email),
            cv_attachment_count=len(cv_attachments),
        )
        trace_recruiter_event(
            "cv_attachments_detected",
            inputs=inbox_email_summary(inbox_email),
            outputs={
                "cv_attachment_count": len(cv_attachments),
                "cv_attachment_names": [filename for filename, _ in cv_attachments],
            },
        )
        if not cv_attachments:
            saved_cv_application = (
                active_application
                if application_has_saved_cv(active_application)
                else latest_application
                if application_has_saved_cv(latest_application)
                else None
            )
            existing_application_followup = thread_application or (
                latest_application if is_status_followup(inbox_email.body) else None
            ) or (
                saved_cv_application
                if saved_cv_application
                and application_role_is_compatible(saved_cv_application, classification, thread_context)
                else None
            )
            if existing_application_followup:
                self.db.log_email_event(
                    inbox_email,
                    "existing_application_followup_without_cv",
                    {
                        **classification,
                        "application_id": existing_application_followup["id"],
                        "application_status": existing_application_followup.get("application_status"),
                        "matched_requirement": existing_application_followup.get("requirement_position"),
                        "application_lookup_scope": (
                            "thread"
                            if thread_application
                            else "sender_status_followup"
                            if is_status_followup(inbox_email.body)
                            else "sender_saved_cv"
                        ),
                        "saved_cv_found": application_has_saved_cv(existing_application_followup),
                        "no_cv_request_sent": True,
                    },
                )
                log_json(
                    logging.INFO,
                    "existing_application_followup_without_cv",
                    **inbox_email_summary(inbox_email),
                    application_id=existing_application_followup["id"],
                    application_status=existing_application_followup.get("application_status"),
                    application_lookup_scope=(
                        "thread"
                        if thread_application
                        else "sender_status_followup"
                        if is_status_followup(inbox_email.body)
                        else "sender_saved_cv"
                    ),
                    saved_cv_found=application_has_saved_cv(existing_application_followup),
                )
                trace_recruiter_event(
                    "existing_application_followup_without_cv",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "application_id": existing_application_followup["id"],
                        "application_status": existing_application_followup.get("application_status"),
                        "application_lookup_scope": (
                            "thread"
                            if thread_application
                            else "sender_status_followup"
                            if is_status_followup(inbox_email.body)
                            else "sender_saved_cv"
                        ),
                        "saved_cv_found": application_has_saved_cv(existing_application_followup),
                        "next_action": "reply_status_followup",
                    },
                )
                self.reply_status_followup(inbox_email, existing_application_followup)
                return True

            if self.db.recent_event_count(
                inbox_email.sender,
                ["missing_cv", "followup_missing_cv", "unsupported_attachment"],
                hours=72,
            ):
                self.notify_manual_hr_review(
                    inbox_email,
                    active_application,
                    "The agent already asked this candidate for a CV/readable CV, but the candidate replied again without a usable CV. HR should handle the thread manually.",
                    event_type="repeated_missing_cv_handoff_to_hr",
                    mark_application=bool(active_application),
                )
                return True
            if inbox_email.attachments:
                self.db.log_email_event(
                    inbox_email,
                    "unsupported_attachment",
                    {
                        **classification,
                        "attachments": [attachment_display_name(filename) for filename, _ in inbox_email.attachments],
                    },
                )
                self.reply_supported_cv_required(inbox_email)
                return True

            match = (
                deterministic_requirement_match(
                    {"target_position": classification.get("detected_position")},
                    classification,
                    requirements,
                    source_text=thread_context,
                )
                if requirements
                else {"requirement_id": None, "confidence": 0, "reason": "No open requirements in database"}
            )
            if requirements and not match.get("requirement_id"):
                sole = single_family_requirement(requirements, cv_role_summary, extracted, cv_text)
                if sole:
                    match = {
                        "requirement_id": sole["id"],
                        "confidence": 0.5,
                        "reason": (
                            f"Only open role in the candidate's field "
                            f"({', '.join(sorted(requirement_role_families(sole))) or 'unknown'}); "
                            "JD score decides suitability"
                        ),
                    }
                    log_json(
                        logging.INFO,
                        "requirement_matched_by_field",
                        **inbox_email_summary(inbox_email),
                        requirement=sole.get("position_title"),
                    )

            matched_requirement_id = safe_int(match.get("requirement_id"))
            requirement = next((row for row in requirements if row["id"] == matched_requirement_id), None)
            if requirement and not requirement_is_compatible_with_candidate_role(
                {"target_position": classification.get("detected_position")},
                classification,
                requirement,
            ):
                match = {
                    "requirement_id": None,
                    "confidence": 0,
                    "reason": (
                        f"Rejected incompatible requirement match: candidate role "
                        f"{classification.get('detected_position')!r} is not compatible with "
                        f"{requirement.get('position_title')!r}"
                    ),
                }
                requirement = None
            log_json(
                logging.INFO,
                "missing_cv_requirement_decision",
                **inbox_email_summary(inbox_email),
                match=match,
                matched_requirement=requirement["position_title"] if requirement else None,
                detected_position=classification.get("detected_position"),
            )
            trace_recruiter_event(
                "missing_cv_requirement_decision",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "match": match,
                    "matched_requirement": requirement["position_title"] if requirement else None,
                    "detected_position": classification.get("detected_position"),
                },
            )

            if is_status_followup(inbox_email.body):
                self.db.log_email_event(
                    inbox_email,
                    "status_followup",
                    {
                        **classification,
                        "application_found": latest_application is not None,
                        "application_id": latest_application["id"] if latest_application else None,
                        "match": match,
                        "matched_requirement": requirement["position_title"] if requirement else None,
                    },
                )
                if latest_application:
                    self.reply_status_followup(inbox_email, latest_application)
                elif requirement:
                    self.db.log_email_event(
                        inbox_email,
                        "followup_missing_cv",
                        {
                            **classification,
                            "matched_requirement": requirement["position_title"],
                            "no_candidate_reply_sent": False,
                        },
                    )
                    self.reply_followup_missing_cv(inbox_email, requirement, active_application)
                else:
                    if self.db.recent_event_count(inbox_email.sender, ["status_followup_no_application"], hours=72):
                        self.notify_manual_hr_review(
                            inbox_email,
                            None,
                            "The candidate keeps asking for an update, but the agent cannot link the thread to an application. HR should review manually.",
                            event_type="repeated_unclear_query_handoff_to_hr",
                            mark_application=False,
                        )
                        return True
                    self.db.log_email_event(
                        inbox_email,
                        "status_followup_no_application",
                        {
                            **classification,
                            "application_found": False,
                            "no_candidate_reply_sent": False,
                        },
                    )
                    self.reply_status_followup(inbox_email, None)
                return True
            self.db.log_email_event(
                inbox_email,
                "missing_cv",
                {
                    **classification,
                    "match": match,
                    "matched_requirement": requirement["position_title"] if requirement else None,
                    "open_requirement_count": len(requirements),
                    "open_requirements": [row["position_title"] for row in requirements],
                    "is_referral": is_referral,
                },
            )

            if requirement:
                log_json(
                    logging.INFO,
                    "reply_missing_cv_for_open_requirement",
                    **inbox_email_summary(inbox_email),
                    requirement_id=requirement.get("id"),
                    requirement_position=requirement.get("position_title"),
                )
                self.reply_missing_cv(inbox_email)
            else:
                log_json(
                    logging.INFO,
                    "reply_no_opening_no_cv",
                    **inbox_email_summary(inbox_email),
                    detected_position=classification.get("detected_position"),
                )
                self.reply_no_opening(inbox_email, classification.get("detected_position"))
            return True

        for filename, payload in cv_attachments:
            # Bound per attachment. Everything from the scoring log onwards reads
            # this, and it is only assigned once the application row is inserted
            # further down, so any path that logged or replied before that raised
            # UnboundLocalError and the candidate got no reply at all. Reset per
            # iteration so a second attachment cannot report the first one's row.
            application_id = None
            log_json(
                logging.INFO,
                "cv_processing_started",
                **inbox_email_summary(inbox_email),
                filename=filename,
                payload_bytes=len(payload or b""),
            )
            trace_recruiter_event(
                "cv_processing_started",
                inputs=inbox_email_summary(inbox_email),
                outputs={"filename": filename, "payload_bytes": len(payload or b"")},
            )
            cv_text = extract_cv_text(filename, payload)
            if not cv_text:
                log_json(
                    logging.WARNING,
                    "cv_text_extract_failed",
                    **inbox_email_summary(inbox_email),
                    filename=filename,
                )
                trace_recruiter_event(
                    "cv_text_extract_failed",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={"filename": filename},
                    tags=["warning"],
                )
                self.db.log_email_event(
                    inbox_email,
                    "cv_text_extract_failed",
                    {"filename": filename, "payload_bytes": len(payload or b"")},
                )
                # Silently dropping the candidate here meant a scanned CV got no
                # reply at all - not a rejection, not a request for a better
                # file, nothing. Tell them, and let HR know if it keeps happening.
                self.reply_unreadable_cv(inbox_email, filename)
                if self.db.recent_event_count(inbox_email.sender, ["cv_text_extract_failed"], hours=72) > 1:
                    # Rate limited: the same unreadable file arriving again is
                    # not new information. One file was escalated four times in
                    # the sampled fortnight.
                    self.notify_hr_rate_limited(
                        inbox_email,
                        active_application,
                        f"Unreadable CV from {inbox_email.sender}",
                        recruiter_email_body(
                            f"The CV attachment '{attachment_display_name(filename)}' could not be read as text "
                            "(it is most likely a scan or image). The candidate has now sent an unreadable file "
                            "more than once and has been asked for a text-based file.",
                            f"Candidate: {inbox_email.sender}",
                            f"Subject: {inbox_email.subject}",
                            RECRUITER_DASHBOARD_BASE_URL
                            if not active_application
                            else dashboard_application_url(active_application["id"]),
                        ),
                        "repeated_unreadable_cv",
                        hours=72,
                    )
                continue

            extracted = self.ai.extract_cv_details(cv_text, thread_context)
            log_json(
                logging.INFO,
                "cv_details_extracted",
                **inbox_email_summary(inbox_email),
                filename=filename,
                target_position=extracted.get("target_position"),
                current_title=extracted.get("current_title"),
                candidate_email=candidate_email_from_cv(extracted, cv_text),
                skill_count=len(ensure_list(extracted.get("skills"))),
            )
            trace_recruiter_event(
                "cv_details_extracted",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "filename": filename,
                    "target_position": extracted.get("target_position"),
                    "current_title": extracted.get("current_title"),
                    "candidate_email": candidate_email_from_cv(extracted, cv_text),
                    "skill_count": len(ensure_list(extracted.get("skills"))),
                },
            )
            cv_role_summary = self.ai.summarize_cv_role(cv_text, extracted)
            if cv_role_summary.get("primary_role") and not extracted.get("target_position"):
                extracted["target_position"] = cv_role_summary.get("primary_role")
            log_json(
                logging.INFO,
                "cv_role_summarized",
                **inbox_email_summary(inbox_email),
                filename=filename,
                primary_role=cv_role_summary.get("primary_role"),
                role_family=cv_role_summary.get("role_family"),
                confidence=cv_role_summary.get("confidence"),
                evidence=cv_role_summary.get("evidence"),
            )
            trace_recruiter_event(
                "cv_role_summarized",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "filename": filename,
                    "primary_role": cv_role_summary.get("primary_role"),
                    "role_family": cv_role_summary.get("role_family"),
                    "confidence": cv_role_summary.get("confidence"),
                    "evidence": cv_role_summary.get("evidence"),
                    "cv_summary": cv_role_summary.get("cv_summary"),
                },
            )
            india_phone_rejection_reason = non_indian_phone_reason(extracted, cv_text)
            if india_phone_rejection_reason:
                log_json(
                    logging.INFO,
                    "india_location_only_rejected",
                    **inbox_email_summary(inbox_email),
                    filename=filename,
                    extracted_phone=extracted.get("phone"),
                    reason=india_phone_rejection_reason,
                )
                trace_recruiter_event(
                    "india_location_only_rejected",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "filename": filename,
                        "extracted_phone": extracted.get("phone"),
                        "reason": india_phone_rejection_reason,
                        "next_action": "reply_india_location_only",
                    },
                )
                self.db.log_email_event(
                    inbox_email,
                    "india_location_only_rejected",
                    {
                        "filename": filename,
                        "extracted_phone": extracted.get("phone"),
                        "reason": india_phone_rejection_reason,
                        "candidate_email": candidate_email_from_cv(extracted, cv_text),
                    },
                )
                self.reply_india_location_only(inbox_email)
                continue
            if use_cv_role_override:
                requested_match = {"requirement_id": None, "confidence": 0, "reason": "Sender corrected thread; using CV role"}
            else:
                requested_match = (
                    deterministic_requirement_match(
                        {"target_position": classification.get("detected_position")},
                        classification,
                        requirements,
                        source_text=thread_context,
                    )
                    if requirements
                    else {"requirement_id": None, "confidence": 0, "reason": "No open requirements in database"}
                )
            requested_requirement_id = safe_int(requested_match.get("requirement_id"))
            requested_requirement = next(
                (row for row in requirements if row["id"] == requested_requirement_id),
                None,
            )
            requested_position = (
                requested_requirement["position_title"]
                if requested_requirement
                else classification.get("detected_position")
            )
            cv_position = extracted.get("target_position") or extracted.get("current_title")
            if not use_cv_role_override and not roles_are_compatible(requested_position, cv_position, cv_text):
                log_json(
                    logging.INFO,
                    "wrong_cv_for_requested_role",
                    **inbox_email_summary(inbox_email),
                    requested_position=requested_position,
                    cv_position=cv_position,
                    requested_match=requested_match,
                    use_cv_role_override=use_cv_role_override,
                )
                trace_recruiter_event(
                    "wrong_cv_for_requested_role",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "requested_position": requested_position,
                        "cv_position": cv_position,
                        "requested_match": requested_match,
                        "use_cv_role_override": use_cv_role_override,
                    },
                )
                self.db.log_email_event(
                    inbox_email,
                    "wrong_cv_for_requested_role",
                    {
                        "requested_position": requested_position,
                        "requested_match": requested_match,
                        "cv_position": cv_position,
                        "current_title": extracted.get("current_title"),
                        "skills": extracted.get("skills", []),
                        "is_referral": is_referral,
                        "use_cv_role_override": use_cv_role_override,
                    },
                )
                self.reply_wrong_cv(inbox_email, requested_position, cv_position, active_application)
                continue

            if not extracted.get("target_position") and not use_cv_role_override:
                extracted["target_position"] = classification.get("detected_position")

            email_has_role = classification_has_specific_role(classification)
            cv_inferred_role = cv_role_summary.get("primary_role") or cv_position
            source_text_for_match = cv_text[:4000] if use_cv_role_override or not email_has_role else f"{thread_context}\n{cv_text[:4000]}"
            classification_for_match = (
                {"detected_position": cv_inferred_role}
                if use_cv_role_override or not email_has_role
                else classification
            )
            match = requested_match if requested_requirement else (
                deterministic_requirement_match(
                    extracted,
                    classification_for_match,
                    requirements,
                    source_text=source_text_for_match,
                )
                if requirements
                else {"requirement_id": None, "confidence": 0, "reason": "No open requirements in database"}
            )
            if requirements and not match.get("requirement_id"):
                match = self.ai.match_requirement(
                    {**extracted, "cv_role_summary": cv_role_summary},
                    requirements,
                    cv_text=cv_text,
                )
                if score_number(match.get("confidence")) is not None and score_number(match.get("confidence")) < LLM_MATCH_MIN_CONFIDENCE:
                    match = {
                        "requirement_id": None,
                        "confidence": score_number(match.get("confidence")) or 0,
                        "reason": f"LLM requirement match confidence below threshold: {match.get('reason')}",
                    }

            matched_requirement_id = safe_int(match.get("requirement_id"))
            requirement = next((row for row in requirements if row["id"] == matched_requirement_id), None)
            match_confidence = score_number(match.get("confidence"))
            title_veto = bool(
                requirement
                and not requirement_is_compatible_with_candidate_role(
                    extracted, classification_for_match, requirement
                )
            )
            if title_veto and match_confidence is not None and match_confidence >= LLM_MATCH_TRUST_CONFIDENCE:
                # Shared words are not what makes two job titles the same job.
                # Keep the match; passes_screening_threshold decides on the JD
                # score a few lines below, and a bad match fails there with a
                # reason the candidate can be told.
                log_json(
                    logging.INFO,
                    "requirement_title_veto_overridden",
                    **inbox_email_summary(inbox_email),
                    requirement=requirement.get("position_title"),
                    candidate_role=extracted.get("current_title") or extracted.get("target_position"),
                    confidence=match_confidence,
                )
                title_veto = False
            if title_veto:
                match = {
                    "requirement_id": None,
                    "confidence": 0,
                    "reason": (
                        f"Rejected incompatible requirement match: candidate role "
                        f"{extracted.get('target_position') or extracted.get('current_title') or classification_for_match.get('detected_position')!r} "
                        f"is not compatible with {requirement.get('position_title')!r}"
                    ),
                }
                requirement = None
            self.db.log_email_event(
                inbox_email,
                "requirement_match",
                {
                    "target_position": extracted.get("target_position"),
                    "current_title": extracted.get("current_title"),
                    "detected_position": classification.get("detected_position"),
                    "match": match,
                    "matched_requirement": requirement["position_title"] if requirement else None,
                    "open_requirement_count": len(requirements),
                    "open_requirements": [row["position_title"] for row in requirements],
                    "is_referral": is_referral,
                    "candidate_email": candidate_email_from_cv(extracted, cv_text),
                    "use_cv_role_override": use_cv_role_override,
                },
            )
            trace_recruiter_event(
                "requirement_match",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "target_position": extracted.get("target_position"),
                    "current_title": extracted.get("current_title"),
                    "detected_position": classification.get("detected_position"),
                    "match": match,
                    "matched_requirement": requirement["position_title"] if requirement else None,
                    "open_requirement_count": len(requirements),
                    "is_referral": is_referral,
                    "candidate_email": candidate_email_from_cv(extracted, cv_text),
                    "use_cv_role_override": use_cv_role_override,
                },
            )

            scored_requirements: list[dict[str, Any]] = []
            fallback_evaluation: dict[str, Any] | None = None
            if requirement is None and requirements and cv_text:
                # Nothing matched by title. Decide it here on the JD score
                # instead of mailing HR "possible match needs your call".
                requirement, fallback_evaluation, scored_requirements = best_requirement_by_score(
                    self.ai, requirements, cv_role_summary, extracted, cv_text
                )
                log_json(
                    logging.INFO,
                    "requirement_resolved_by_score" if requirement else "requirement_scoring_found_no_fit",
                    **inbox_email_summary(inbox_email),
                    application_id=application_id,
                    resolved=requirement.get("position_title") if requirement else None,
                    scored=scored_requirements,
                )
                self.db.log_email_event(
                    inbox_email,
                    "requirement_resolved_by_score" if requirement else "requirement_scoring_found_no_fit",
                    {
                        "application_id": application_id,
                        "resolved_requirement": requirement.get("position_title") if requirement else None,
                        "scored": scored_requirements,
                    },
                )

            if requirement is not None and fallback_evaluation is not None:
                evaluation = fallback_evaluation      # already scored, do not pay twice
            else:
                evaluation = self.ai.evaluate_cv(cv_text, extracted, requirement)
            log_json(
                logging.INFO,
                "cv_evaluated",
                **inbox_email_summary(inbox_email),
                filename=filename,
                requirement_id=requirement.get("id") if requirement else None,
                matched_requirement=requirement.get("position_title") if requirement else None,
                ats_score=evaluation.get("ats_score"),
                jd_match_score=evaluation.get("jd_match_score"),
                recommendation=evaluation.get("recommendation"),
            )
            trace_recruiter_event(
                "cv_evaluated",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "filename": filename,
                    "requirement_id": requirement.get("id") if requirement else None,
                    "matched_requirement": requirement.get("position_title") if requirement else None,
                    "ats_score": evaluation.get("ats_score"),
                    "jd_match_score": evaluation.get("jd_match_score"),
                    "recommendation": evaluation.get("recommendation"),
                },
            )
            candidate_email = candidate_email_from_cv(extracted, cv_text)
            submission_type = "referral" if is_referral else "self_application"
            referrer_email = inbox_email.sender if is_referral else None
            candidate_id = self.db.insert_candidate(
                inbox_email,
                cv_text,
                extracted,
                evaluation,
                submission_type,
                candidate_email,
                referrer_email,
            )
            attachment_sha256 = hashlib.sha256(payload).hexdigest()
            status = "matched_requirement" if requirement else "no_open_requirement"

            existing_open = self.db.open_application_for_candidate(
                candidate_email or inbox_email.sender,
                requirement.get("id") if requirement else None,
            )
            if existing_open and not thread_application:
                # Same person, same opening, new email thread. Continue the
                # existing application rather than re-running intake and asking
                # again for a CV we already hold.
                log_json(
                    logging.INFO,
                    "existing_application_reused_for_new_thread",
                    **inbox_email_summary(inbox_email),
                    application_id=existing_open["id"],
                    status=existing_open.get("application_status"),
                )
                trace_recruiter_event(
                    "existing_application_reused_for_new_thread",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "application_id": existing_open["id"],
                        "status": existing_open.get("application_status"),
                        "next_action": "reply_status_followup",
                    },
                )
                self.db.log_email_event(
                    inbox_email,
                    "existing_application_reused_for_new_thread",
                    {
                        "application_id": existing_open["id"],
                        "application_status": existing_open.get("application_status"),
                    },
                )
                self.reply_status_followup(
                    inbox_email,
                    self.db.application_with_requirement(existing_open["id"]) or existing_open,
                )
                continue

            application_id = self.db.insert_application(
                inbox_email,
                candidate_id,
                requirement,
                extracted,
                evaluation,
                filename,
                attachment_sha256,
                None,
                status,
                submission_type,
                candidate_email,
                referrer_email,
            )
            log_json(
                logging.INFO,
                "application_saved",
                **inbox_email_summary(inbox_email),
                application_id=application_id,
                candidate_id=candidate_id,
                status=status,
                requirement_id=requirement.get("id") if requirement else None,
                candidate_email=candidate_email,
                submission_type=submission_type,
            )
            trace_recruiter_event(
                "application_saved",
                inputs=inbox_email_summary(inbox_email),
                outputs={
                    "application_id": application_id,
                    "candidate_id": candidate_id,
                    "status": status,
                    "requirement_id": requirement.get("id") if requirement else None,
                    "candidate_email": candidate_email,
                    "submission_type": submission_type,
                },
            )
            if application_id:
                try:
                    saved_filename = save_cv_attachment_file(application_id, filename, payload)
                    self.db.update_application_attachment_filename(application_id, saved_filename)
                    self.db.log_email_event(
                        inbox_email,
                        "cv_saved_to_folder",
                        {
                            "application_id": application_id,
                            "original_filename": filename,
                            "attachment_filename": saved_filename,
                            "attachment_sha256": attachment_sha256,
                        },
                    )
                    log_json(
                        logging.INFO,
                        "cv_saved_to_folder",
                        **inbox_email_summary(inbox_email),
                        application_id=application_id,
                        attachment_filename=saved_filename,
                        attachment_sha256=attachment_sha256,
                    )
                    trace_recruiter_event(
                        "cv_saved_to_folder",
                        inputs=inbox_email_summary(inbox_email),
                        outputs={
                            "application_id": application_id,
                            "attachment_filename": saved_filename,
                            "attachment_sha256": attachment_sha256,
                        },
                    )
                except Exception as exc:
                    LOGGER.exception(
                        "cv_save_failed %s",
                        json.dumps(
                            {
                                **inbox_email_summary(inbox_email),
                                "application_id": application_id,
                                "filename": filename,
                                "error": str(exc),
                            },
                            default=str,
                        ),
                    )
                    self.db.log_email_event(
                        inbox_email,
                        "cv_save_failed",
                        {
                            "application_id": application_id,
                            "original_filename": filename,
                            "attachment_sha256": attachment_sha256,
                            "error": str(exc),
                        },
                    )

            if requirement:
                if application_id and passes_screening_threshold(evaluation, requirement, extracted):
                    log_json(
                        logging.INFO,
                        "screening_threshold_passed",
                        **inbox_email_summary(inbox_email),
                        application_id=application_id,
                        ats_score=evaluation.get("ats_score"),
                        jd_match_score=evaluation.get("jd_match_score"),
                        requirement_position=requirement.get("position_title"),
                    )
                    trace_recruiter_event(
                        "screening_threshold_passed",
                        inputs=inbox_email_summary(inbox_email),
                        outputs={
                            "application_id": application_id,
                            "ats_score": evaluation.get("ats_score"),
                            "jd_match_score": evaluation.get("jd_match_score"),
                            "requirement_position": requirement.get("position_title"),
                            "next_action": "screening_questions_sent",
                        },
                    )
                    self.db.update_application_screening(
                        application_id,
                        "screening_questions_sent",
                        {
                            "ats_score": evaluation.get("ats_score"),
                            "jd_match_score": evaluation.get("jd_match_score"),
                            "work_terms": screening_work_terms(),
                        },
                    )
                    self.reply_screening_questions(
                        inbox_email,
                        requirement,
                        {"id": application_id} if application_id else None,
                    )
                    continue
                jd_rejected = False
                if application_id:
                    jd_rejected = True
                    reason = jd_rejection_reason(evaluation, requirement, extracted)
                    log_json(
                        logging.INFO,
                        "screening_threshold_failed",
                        **inbox_email_summary(inbox_email),
                        application_id=application_id,
                        reason=reason,
                        ats_score=evaluation.get("ats_score"),
                        jd_match_score=evaluation.get("jd_match_score"),
                        requirement_position=requirement.get("position_title"),
                    )
                    trace_recruiter_event(
                        "screening_threshold_failed",
                        inputs=inbox_email_summary(inbox_email),
                        outputs={
                            "application_id": application_id,
                            "reason": reason,
                            "ats_score": evaluation.get("ats_score"),
                            "jd_match_score": evaluation.get("jd_match_score"),
                            "requirement_position": requirement.get("position_title"),
                            "next_action": "jd_score_rejected",
                        },
                    )
                    self.db.mark_jd_score_rejected(application_id, reason)
                    try:
                        notify_jd_score_rejection_to_hr(application_id, evaluation, requirement)
                    except Exception as exc:
                        self.db.log_email_event(
                            inbox_email,
                            "jd_score_rejection_hr_notification_failed",
                            {"application_id": application_id, "error": str(exc), "reason": reason},
                        )
                    self.db.log_email_event(
                        inbox_email,
                        "jd_score_rejected",
                        {
                            "application_id": application_id,
                            "reason": reason,
                            "ats_score": evaluation.get("ats_score"),
                            "jd_match_score": evaluation.get("jd_match_score"),
                            "recommendation": evaluation.get("recommendation"),
                        },
                    )
                if jd_rejected:
                    # The decision has already been made and recorded. Telling
                    # the candidate their application is "under review" and
                    # waiting for HR to confirm a rejection the agent is
                    # confident about leaves them waiting for a letter nobody
                    # was going to write.
                    self.reply_not_selected(
                        inbox_email,
                        {"id": application_id, "requirement_position": requirement.get("position_title")},
                        is_referral=is_referral,
                    )
                elif is_referral and candidate_email:
                    self.reply_referral_received(inbox_email, candidate_email, requirement["position_title"])
                elif is_referral:
                    self.reply_referral_missing_candidate_email(inbox_email, requirement["position_title"])
                else:
                    self.reply_received(inbox_email, {"id": application_id} if application_id else None)
            else:
                near_misses = near_miss_requirements(requirements, cv_role_summary, extracted, cv_text)
                best_scored = max(
                    (score_number(row.get("jd_match_score")) or 0.0 for row in scored_requirements),
                    default=None,
                )
                # Every open role was scored just above and none cleared the bar.
                # Only a CV that came close is worth a person's time; the rest
                # get told there is no opening, which is the honest answer.
                if best_scored is not None and best_scored < RECRUITER_SCREENING_JD_MIN - NEAR_MISS_AMBIGUOUS_BAND:
                    log_json(
                        logging.INFO,
                        "near_miss_scored_below_band_no_handoff",
                        **inbox_email_summary(inbox_email),
                        application_id=application_id,
                        best_jd_match_score=best_scored,
                        scored=scored_requirements,
                    )
                    near_misses = []
                if near_misses:
                    # Same domain, different title. Auto-rejecting these loses
                    # genuinely relevant people, so a human decides instead.
                    near_miss_titles = [row.get("position_title") for row in near_misses]
                    log_json(
                        logging.INFO,
                        "near_miss_requirement_handoff_to_hr",
                        **inbox_email_summary(inbox_email),
                        application_id=application_id,
                        candidate_role=cv_role_summary.get("primary_role"),
                        candidate_role_family=cv_role_summary.get("role_family"),
                        near_miss_requirements=near_miss_titles,
                        ats_score=evaluation.get("ats_score"),
                    )
                    trace_recruiter_event(
                        "near_miss_requirement_handoff_to_hr",
                        inputs=inbox_email_summary(inbox_email),
                        outputs={
                            "application_id": application_id,
                            "candidate_role": cv_role_summary.get("primary_role"),
                            "near_miss_requirements": near_miss_titles,
                            "next_action": "hr_decides",
                        },
                    )
                    self.db.log_email_event(
                        inbox_email,
                        "near_miss_requirement_handoff_to_hr",
                        {
                            "application_id": application_id,
                            "candidate_role": cv_role_summary.get("primary_role"),
                            "candidate_role_family": cv_role_summary.get("role_family"),
                            "near_miss_requirements": near_miss_titles,
                            "ats_score": evaluation.get("ats_score"),
                        },
                    )
                    self.notify_hr_rate_limited(
                        inbox_email,
                        {"id": application_id} if application_id else None,
                        f"Possible match needs your call: {inbox_email.sender}",
                        recruiter_email_body(
                            "A candidate did not match any open role automatically, but works in the same area.",
                            f"Candidate: {extracted.get('full_name') or inbox_email.sender}",
                            f"Their role: {cv_role_summary.get('primary_role') or extracted.get('current_title') or '-'}",
                            f"They asked about: {classification.get('detected_position') or '-'}",
                            f"ATS score: {evaluation.get('ats_score')}",
                            f"Open roles in the same area: {', '.join(str(title) for title in near_miss_titles)}",
                            f"Application: {dashboard_application_url(application_id)}"
                            if application_id
                            else RECRUITER_DASHBOARD_BASE_URL,
                            "Assign a requirement on the dashboard to continue, or reject to close it.",
                        ),
                        "near_miss_notice",
                    )
                    if application_id:
                        self.db.mark_manual_hr_review(
                            application_id,
                            f"No exact requirement match; same-domain openings: {', '.join(str(t) for t in near_miss_titles)}",
                        )
                    self.reply_profile_under_review(
                        inbox_email,
                        {"id": application_id} if application_id else None,
                    )
                    continue

                log_json(
                    logging.INFO,
                    "reply_no_opening_with_cv",
                    **inbox_email_summary(inbox_email),
                    application_id=application_id,
                    target_position=extracted.get("target_position"),
                    current_title=extracted.get("current_title"),
                )
                trace_recruiter_event(
                    "reply_no_opening_with_cv",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={
                        "application_id": application_id,
                        "target_position": extracted.get("target_position"),
                        "current_title": extracted.get("current_title"),
                    },
                )
                self.reply_no_opening(
                    inbox_email,
                    extracted.get("target_position"),
                    {"id": application_id} if application_id else None,
                )

        log_json(
            logging.INFO,
            "email_processing_finished",
            **inbox_email_summary(inbox_email),
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
        )
        trace_recruiter_event(
            "email_processing_finished",
            inputs=inbox_email_summary(inbox_email),
            outputs={"elapsed_ms": round((time.monotonic() - started_at) * 1000)},
        )
        return True

    def run_once(self):
        self.init_schema()
        LOGGER.info("Using mail provider: %s", self.inbox.provider_name)
        emails = self.inbox.fetch_unseen(RECRUITER_POLL_LIMIT)
        if not emails:
            LOGGER.info("No unread recruiter emails found.")
            return

        for inbox_email in emails:
            try:
                if not self.db.claim_provider_message(inbox_email.uid.decode(errors="ignore")):
                    log_json(logging.INFO, "email_already_processed_skipped", **inbox_email_summary(inbox_email))
                    self.inbox.mark_seen(inbox_email.uid)
                    continue
                should_mark_seen = self.process_email(inbox_email)
                # Ignored mail is marked read too. Leaving it unread meant a
                # handful of non-recruiting emails permanently filled the poll
                # window and starved every new candidate email behind them.
                self.inbox.mark_seen(inbox_email.uid)
                if should_mark_seen:
                    log_json(logging.INFO, "email_processed_marked_seen", **inbox_email_summary(inbox_email))
                else:
                    log_json(logging.INFO, "email_ignored_marked_seen", **inbox_email_summary(inbox_email))
            except Exception as exc:
                LOGGER.exception(
                    "email_processing_failed %s",
                    json.dumps(
                        {**inbox_email_summary(inbox_email), "error": str(exc)},
                        default=str,
                    ),
                )
                # The failing statement aborted the transaction; without this
                # every cleanup call below dies with InFailedSqlTransaction and
                # the message is left claimed and un-retryable.
                self.db.rollback()
                try:
                    self.db.log_email_event(
                        inbox_email,
                        "processing_failed",
                        {"error": str(exc)},
                    )
                except Exception as log_exc:
                    LOGGER.exception("Could not log processing failure: %s", log_exc)
                try:
                    self.db.release_provider_message(inbox_email.uid.decode(errors="ignore"))
                except Exception as release_exc:
                    LOGGER.exception("Could not release message claim: %s", release_exc)

    def process_one_graph_message(self, message_id: str, resource_path: str | None = None) -> bool:
        self.init_schema()
        if not isinstance(self.inbox, MicrosoftGraphProvider):
            LOGGER.error("Single-message Graph processing is only available with MAIL_PROVIDER=microsoft_graph.")
            return False

        # Graph webhooks are at-least-once, and isRead is only set ~50s after
        # processing begins, so the unread flag alone let one email be answered
        # twice. Claiming the id is atomic and survives a restart.
        if not self.db.claim_provider_message(message_id):
            log_json(
                logging.INFO,
                "graph_message_already_processed",
                graph_message_id=message_id,
            )
            trace_recruiter_event(
                "graph_message_already_processed",
                inputs={"graph_message_id": message_id},
                tags=["graph", "guardrail"],
            )
            return True

        log_json(
            logging.INFO,
            "graph_message_fetch_started",
            graph_message_id=message_id,
            resource_path=resource_path,
        )
        trace_recruiter_event(
            "graph_message_fetch_started",
            inputs={"graph_message_id": message_id, "resource_path": resource_path},
            tags=["graph"],
        )
        inbox_email = self.inbox.fetch_message_by_id(message_id, resource_path=resource_path)
        if not inbox_email:
            log_json(
                logging.WARNING,
                "graph_message_not_found",
                graph_message_id=message_id,
                resource_path=resource_path,
            )
            trace_recruiter_event(
                "graph_message_not_found",
                inputs={"graph_message_id": message_id, "resource_path": resource_path},
                tags=["graph", "warning"],
            )
            self.db.release_provider_message(message_id)
            return False

        try:
            should_mark_seen = self.process_email(inbox_email)
            if should_mark_seen:
                self.inbox.mark_seen(inbox_email.uid)
                log_json(
                    logging.INFO,
                    "graph_message_processed_marked_seen",
                    **inbox_email_summary(inbox_email),
                    graph_message_id=message_id,
                )
                trace_recruiter_event(
                    "graph_message_processed_marked_seen",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={"graph_message_id": message_id},
                    tags=["graph"],
                )
                return True
            else:
                self.inbox.mark_unseen(inbox_email.uid)
                log_json(
                    logging.INFO,
                    "graph_message_ignored_left_unread",
                    **inbox_email_summary(inbox_email),
                    graph_message_id=message_id,
                )
                trace_recruiter_event(
                    "graph_message_ignored_left_unread",
                    inputs=inbox_email_summary(inbox_email),
                    outputs={"graph_message_id": message_id},
                    tags=["graph"],
                )
                return True
        except Exception as exc:
            LOGGER.exception(
                "graph_message_processing_failed %s",
                json.dumps(
                    {**inbox_email_summary(inbox_email), "graph_message_id": message_id, "error": str(exc)},
                    default=str,
                ),
            )
            trace_recruiter_event(
                "graph_message_processing_failed",
                inputs=inbox_email_summary(inbox_email),
                outputs={"graph_message_id": message_id, "error": str(exc)},
                tags=["graph", "error"],
            )
            self.db.rollback()
            try:
                self.inbox.mark_unseen(inbox_email.uid)
            except Exception as mark_exc:
                LOGGER.exception("Could not mark failed Graph message unread: %s", mark_exc)
            try:
                self.db.log_email_event(
                    inbox_email,
                    "processing_failed",
                    {"error": str(exc), "graph_message_id": message_id},
                )
            except Exception as log_exc:
                LOGGER.exception("Could not log processing failure: %s", log_exc)
            # Release the claim so a genuine failure can be retried. The reply
            # ledger stops the retry from re-sending anything already sent.
            try:
                self.db.release_provider_message(message_id)
            except Exception as release_exc:
                LOGGER.exception("Could not release message claim: %s", release_exc)
            return False

    def run_forever(self, poll_seconds: int):
        self.init_schema()
        print(f"Recruiter agent is watching {RECRUITER_MAILBOX}. Polling every {poll_seconds}s.")
        print("Press Ctrl+C to stop.")

        while True:
            try:
                self.run_once()
            except Exception as exc:
                print(f"Recruiter watcher error: {exc}")
            time.sleep(poll_seconds)

    def handle_gmail_push(self, notification: dict[str, Any]):
        email_address = notification.get("emailAddress")
        history_id = notification.get("historyId")
        print(f"Gmail trigger received for {email_address} historyId={history_id}")
        self.run_once()


def reevaluate_application_against_requirement(application_id: int) -> dict[str, Any] | None:
    """Re-score a CV against the requirement it is now assigned to.

    When HR assigns a requirement by hand, the stored jd_match_score, strengths,
    risks and missing_requirements still describe whatever the CV was originally
    compared against - usually nothing at all. Without this the candidate reaches
    the interview with a null JD score and HR is deciding on stale data.
    """
    db = RecruiterDatabase()
    try:
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        if not application.get("requirement_id"):
            return None

        cv_text = application.get("raw_cv_text") or ""
        if not cv_text:
            log_json(
                logging.WARNING,
                "reevaluate_skipped_no_cv_text",
                application_id=application_id,
            )
            return None

        requirement = db.one(
            "SELECT * FROM recruitment_requirements WHERE id = %s",
            (application["requirement_id"],),
        )
        if not requirement:
            return None

        ai = RecruiterAI()
        extracted = normalize_cv_details(
            {
                "full_name": application.get("full_name"),
                "current_title": application.get("detected_position"),
                "target_position": application.get("matched_position") or application.get("detected_position"),
                "total_experience_years": None,
            }
        )
        evaluation = ai.evaluate_cv(cv_text, extracted, requirement)

        db.execute(
            """
            UPDATE recruiter_applications
            SET matched_position = %s,
                ats_score = COALESCE(%s, ats_score),
                jd_match_score = %s,
                strengths = %s::jsonb,
                risks = %s::jsonb,
                missing_requirements = %s::jsonb,
                ai_short_description = COALESCE(%s, ai_short_description),
                ai_evaluation = %s::jsonb
            WHERE id = %s
            """,
            (
                requirement.get("position_title"),
                numeric_value(evaluation.get("ats_score")),
                numeric_value(evaluation.get("jd_match_score")),
                json.dumps(ensure_list(evaluation.get("strengths"))),
                json.dumps(ensure_list(evaluation.get("risks"))),
                json.dumps(ensure_list(evaluation.get("missing_requirements"))),
                evaluation.get("short_description"),
                json.dumps(evaluation),
                application_id,
            ),
        )
        log_json(
            logging.INFO,
            "application_reevaluated_against_requirement",
            application_id=application_id,
            requirement=requirement.get("position_title"),
            ats_score=evaluation.get("ats_score"),
            jd_match_score=evaluation.get("jd_match_score"),
            recommendation=evaluation.get("recommendation"),
        )
        trace_recruiter_event(
            "application_reevaluated_against_requirement",
            inputs={"application_id": application_id, "requirement": requirement.get("position_title")},
            outputs={
                "ats_score": evaluation.get("ats_score"),
                "jd_match_score": evaluation.get("jd_match_score"),
                "recommendation": evaluation.get("recommendation"),
            },
        )
        return evaluation
    finally:
        db.close()


def send_tracked_direct_email(
    db: "RecruiterDatabase",
    mailer,
    application_id: int | None,
    recipient: str,
    subject: str,
    body: str,
    scenario: str,
) -> str | None:
    """Send a direct candidate email and record it in the reply ledger.

    Recording matters for two reasons: the duplicate-reply guard needs to know
    what has already gone out, and human-takeover detection needs to be able to
    recognise the agent's own messages. Anything sent outside this helper looks
    like a stranger wrote it.
    """
    sender = mailer if hasattr(mailer, "send_direct_email") else RecruiterMailer()
    provider_message_id = sender.send_direct_email(recipient, subject, body)
    try:
        db.record_sent_reply(application_id, recipient, scenario, body, provider_message_id)
    except Exception as exc:
        LOGGER.warning("Could not record sent reply (%s) for %s: %s", scenario, application_id, exc)
    log_json(
        logging.INFO,
        "candidate_direct_email_sent",
        application_id=application_id,
        scenario=scenario,
        recipient=recipient,
    )
    return provider_message_id


REMATCH_DEFAULT_STATUSES = ("manual_hr_review", "no_open_requirement")


# What an outside dashboard may ask the agent to do. Each maps to a function
# that already exists and does the real work - the email, the meeting, the
# scoring - which a plain UPDATE never would.
def agent_action_handlers() -> dict[str, Any]:
    return {
        "approve_interview": lambda app_id, payload: send_interview_request_after_hr_approval(app_id),
        "approve_hr_round": lambda app_id, payload: send_final_hr_round_request(app_id, approved_by_hr=True),
        "reject_after_interview": lambda app_id, payload: send_interview_rejection(
            app_id, payload.get("reason")
        ),
        "send_interview_link": lambda app_id, payload: send_interview_link_for_application(app_id),
        "send_teams_link": lambda app_id, payload: send_teams_link_for_application(app_id),
        "revoke_jd_rejection": lambda app_id, payload: revoke_jd_score_rejection(app_id),
        "reevaluate": lambda app_id, payload: reevaluate_application_against_requirement(app_id),
        "select_after_hr_round": lambda app_id, payload: select_candidate_after_hr_round(app_id),
        "reject_after_hr_round": lambda app_id, payload: reject_candidate_after_hr_round(app_id),
        "hold_after_hr_round": lambda app_id, payload: hold_candidate_after_hr_round(app_id),
        "reopen_interview": lambda app_id, payload: reopen_interview_attempts(app_id),
    }


def reopen_interview_attempts(application_id: int):
    db = RecruiterDatabase()
    try:
        db.execute(
            "UPDATE recruiter_applications SET interview_attempts = 0 WHERE id = %s",
            (application_id,),
        )
    finally:
        db.close()


def process_action_queue(limit: int = 20) -> int:
    """Run whatever the dashboard has asked for.

    An external app can read and write this database directly, but a row change
    cannot send an email or book a Teams meeting. Writing requested_action puts
    a job here, and this performs it with the same code path the built-in
    dashboard uses, so both interfaces behave identically.
    """
    db = RecruiterDatabase()
    handlers = agent_action_handlers()
    done = 0
    try:
        db.init_schema()
        pending = db.rows(
            """
            SELECT id, application_id, action, payload, requested_by
            FROM agent_action_queue
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT %s
            """,
            (limit,),
        )
        for job in pending:
            # Claim it first so two workers cannot run the same action twice.
            claimed = db.one(
                """
                UPDATE agent_action_queue
                SET status = 'running', started_at = NOW()
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (job["id"],),
            )
            db.conn.commit()
            if not claimed:
                continue

            action = str(job.get("action") or "").strip()
            handler = handlers.get(action)
            if not handler:
                db.execute(
                    "UPDATE agent_action_queue SET status='failed', error=%s, finished_at=NOW() WHERE id=%s",
                    (f"unknown action '{action}'. Known: {', '.join(sorted(handlers))}", job["id"]),
                )
                log_json(logging.WARNING, "agent_action_unknown", action=action, job_id=job["id"])
                continue

            try:
                handler(job["application_id"], json_dict(job.get("payload")))
                db.execute(
                    "UPDATE agent_action_queue SET status='done', finished_at=NOW() WHERE id=%s",
                    (job["id"],),
                )
                done += 1
                log_json(
                    logging.INFO,
                    "agent_action_completed",
                    job_id=job["id"],
                    action=action,
                    application_id=job["application_id"],
                    requested_by=job.get("requested_by"),
                )
            except Exception as exc:
                LOGGER.exception("agent action %s failed: %s", action, exc)
                db.rollback()
                db.execute(
                    "UPDATE agent_action_queue SET status='failed', error=%s, finished_at=NOW() WHERE id=%s",
                    (str(exc)[:800], job["id"]),
                )
                log_json(
                    logging.ERROR,
                    "agent_action_failed",
                    job_id=job["id"],
                    action=action,
                    application_id=job["application_id"],
                    error=str(exc)[:400],
                )
        return done
    finally:
        db.close()


def rematch_stored_applications(
    statuses: tuple[str, ...] = REMATCH_DEFAULT_STATUSES,
    apply_changes: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Re-run matching over applications that were parked for a human.

    Most of them were parked because title matching could not bridge, say,
    "Senior Accountant" to "US Bookkeeper" - not because anyone judged them
    unsuitable. Re-running now sends each to the JD it should have been measured
    against, and the JD score decides: screening questions, or a rejection with
    a reason HR can revoke.

    Defaults to a dry run. Nothing is written or emailed unless apply_changes is
    explicitly set, because this sends real mail to real candidates.
    """
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    results: list[dict[str, Any]] = []
    try:
        db.init_schema()
        ai = RecruiterAI()
        requirements = db.open_requirements()
        if not requirements:
            print("No open requirements; nothing to match against.")
            return results

        placeholders = ", ".join(["%s"] * len(statuses))
        rows = db.rows(
            f"""
            SELECT ra.id, ra.application_status, ra.candidate_email, ra.source_email,
                   ra.detected_position, ra.matched_position, ra.requirement_id,
                   rc.full_name, rc.current_title, rc.skills, rc.raw_cv_text, rc.cv_summary
            FROM recruiter_applications ra
            JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
            WHERE LOWER(COALESCE(ra.application_status, '')) IN ({placeholders})
            ORDER BY ra.id DESC
            LIMIT %s
            """,
            (*[status.lower() for status in statuses], limit),
        )
        print(f"{len(rows)} application(s) in {', '.join(statuses)}"
              f"{'' if apply_changes else '   [DRY RUN - nothing will be sent]'}\n")

        for row in rows:
            application_id = row["id"]
            who = row.get("candidate_email") or row.get("source_email") or "-"
            cv_text = row.get("raw_cv_text") or ""
            outcome = {"application_id": application_id, "candidate": who, "action": None, "detail": ""}

            if not cv_text.strip():
                outcome.update(action="skipped", detail="no CV text stored")
                results.append(outcome)
                print(f"  {application_id:>5}  {who:<38} SKIP    no CV text stored")
                continue

            extracted = normalize_cv_details(
                {
                    "full_name": row.get("full_name"),
                    "current_title": row.get("current_title"),
                    "target_position": row.get("matched_position") or row.get("detected_position"),
                    "skills": json_list_or_empty(row.get("skills")),
                }
            )
            classification = {"detected_position": row.get("detected_position")}
            try:
                cv_role_summary = ai.summarize_cv_role(cv_text, extracted)
            except Exception as exc:
                LOGGER.warning("Could not summarise role for %s: %s", application_id, exc)
                cv_role_summary = {}

            match = deterministic_requirement_match(
                extracted, classification, requirements, source_text=cv_text[:4000]
            )
            if not match.get("requirement_id"):
                try:
                    llm_match = ai.match_requirement(
                        {**extracted, "cv_role_summary": cv_role_summary}, requirements, cv_text=cv_text
                    )
                    confidence = score_number(llm_match.get("confidence"))
                    if llm_match.get("requirement_id") and (
                        confidence is None or confidence >= LLM_MATCH_MIN_CONFIDENCE
                    ):
                        match = llm_match
                except Exception as exc:
                    LOGGER.warning("LLM match failed for %s: %s", application_id, exc)
            requirement = next(
                (r for r in requirements if r["id"] == safe_int(match.get("requirement_id"))), None
            )
            if not requirement:
                requirement = single_family_requirement(requirements, cv_role_summary, extracted, cv_text)

            if not requirement:
                outcome.update(action="no_match", detail="no open role in this candidate's field")
                results.append(outcome)
                print(f"  {application_id:>5}  {who:<38} KEEP    no open role in their field - left for HR")
                continue

            evaluation = ai.evaluate_cv(cv_text, extracted, requirement)
            ats = numeric_value(evaluation.get("ats_score"))
            jd = numeric_value(evaluation.get("jd_match_score"))
            role = requirement.get("position_title")
            passes = passes_screening_threshold(evaluation, requirement, extracted)
            outcome.update(
                role=role, ats_score=ats, jd_match_score=jd,
                action="screening_questions" if passes else "reject_jd_score",
                detail=(evaluation.get("short_description") or evaluation.get("reasoning") or "")[:160],
            )
            verdict = "SCREEN " if passes else "REJECT "
            print(f"  {application_id:>5}  {who:<38} {verdict} {role:<18} ATS={ats} JD={jd}")

            if apply_changes:
                db.execute(
                    """
                    UPDATE recruiter_applications
                    SET requirement_id = %s,
                        matched_position = %s,
                        ats_score = %s,
                        jd_match_score = %s,
                        strengths = %s::jsonb,
                        risks = %s::jsonb,
                        missing_requirements = %s::jsonb,
                        ai_short_description = %s,
                        ai_evaluation = %s::jsonb
                    WHERE id = %s
                    """,
                    (
                        requirement["id"], role, ats, jd,
                        json.dumps(ensure_list(evaluation.get("strengths"))),
                        json.dumps(ensure_list(evaluation.get("risks"))),
                        json.dumps(ensure_list(evaluation.get("missing_requirements"))),
                        evaluation.get("short_description"),
                        json.dumps(evaluation),
                        application_id,
                    ),
                )
                if passes:
                    recipient = clean_email(row.get("candidate_email") or row.get("source_email"))
                    if recipient:
                        send_screening_questions_direct(db, mailer, application_id, recipient, role)
                        db.update_application_screening(
                            application_id, "screening_questions_sent",
                            {"ats_score": ats, "jd_match_score": jd, "work_terms": screening_work_terms()},
                        )
                    else:
                        outcome["action"] = "skipped"
                        outcome["detail"] = "no candidate email address"
                else:
                    db.execute(
                        "UPDATE recruiter_applications SET application_status = 'rejected_jd_score' WHERE id = %s",
                        (application_id,),
                    )
                    try:
                        notify_jd_score_rejection_to_hr(application_id, evaluation, requirement)
                    except Exception as exc:
                        LOGGER.warning("Could not notify HR of rejection for %s: %s", application_id, exc)
            results.append(outcome)

        screened = sum(1 for r in results if r["action"] == "screening_questions")
        rejected = sum(1 for r in results if r["action"] == "reject_jd_score")
        kept = sum(1 for r in results if r["action"] == "no_match")
        skipped = sum(1 for r in results if r["action"] == "skipped")
        print(f"\n  screening questions: {screened}   rejected on JD score: {rejected}"
              f"   left for HR: {kept}   skipped: {skipped}")
        if not apply_changes:
            print("\n  Dry run. Re-run with --apply to write these changes and email candidates.")
        return results
    finally:
        db.close()


def json_list_or_empty(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def screening_questions_body(role_title: str | None) -> str:
    terms = screening_work_terms()
    return recruiter_email_body(
        "Thanks for your patience while I reviewed your profile.",
        f"I would like to take your application{f' for {role_title}' if role_title else ''} forward.",
        f"Before we set up the interview, could you confirm you are comfortable with "
        f"{terms['shift']}, {terms['work_mode']} in {terms['office_location']}, "
        f"{terms['working_days']}, with cab facility {terms['cab_facility']}?",
        "Please also share your current salary, expected salary, current location, "
        "and how soon you could join.",
    )


def send_screening_questions_direct(
    db: "RecruiterDatabase",
    mailer,
    application_id: int,
    recipient: str,
    role_title: str | None,
) -> str | None:
    return send_tracked_direct_email(
        db,
        mailer,
        application_id,
        recipient,
        "Next steps on your application",
        screening_questions_body(role_title),
        "screening_questions",
    )


def send_interview_request_after_hr_approval(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")

        # HR approving a candidate means "pursue this person", not "skip the
        # pipeline". A candidate who reached HR without ever being screened must
        # answer the screening questions before an interview link goes out,
        # otherwise we interview someone whose salary, location, notice period
        # and shift availability are all still unknown.
        answers = json_dict(application.get("screening_details"))
        missing = missing_screening_fields(application)
        if missing:
            role_title = (
                application.get("requirement_position")
                or application.get("matched_position")
                or application.get("detected_position")
            )
            send_screening_questions_direct(db, mailer, application_id, recipient, role_title)
            db.update_application_screening(application_id, "screening_questions_sent", answers)
            log_json(
                logging.INFO,
                "hr_approved_screening_questions_sent",
                application_id=application_id,
                reason="screening answers were incomplete, so no interview link was sent yet",
                missing=missing,
            )
            trace_recruiter_event(
                "hr_approved_screening_questions_sent",
                inputs={"application_id": application_id},
                outputs={"next_action": "await_screening_answers"},
            )
            return

        db.mark_hr_approved_for_interview(application_id)
        token = db.ensure_interview_link(application_id)
        link = candidate_interview_url(token)
        # ensure_interview_link only sets the status when it mints a NEW token,
        # so an existing link left the row in interview_time_requested and every
        # later candidate reply re-sent the link. Set it explicitly.
        db.mark_interview_link_sent(application_id)
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        body = recruiter_email_body(
            "Thanks for confirming the details.",
            f"We are good to move ahead with the interview{f' for {role}' if role else ''}.",
            f"You can start it here whenever you are ready: {link}",
            "Please use a laptop or desktop with a working microphone, and choose a quiet place before starting.",
        )
        send_tracked_direct_email(
            db, mailer, application_id, recipient, "Interview link", body, "interview_link",
        )
    finally:
        db.close()


def send_interview_link_for_application(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")

        # Same rule as the approval path: never interview someone whose salary,
        # location, notice period and shift availability are unknown. If HR
        # already has these, entering them on the application unblocks the send.
        missing = missing_screening_fields(application)
        if missing:
            raise RuntimeError(
                f"Cannot send the interview link for application {application_id}: "
                f"screening is incomplete ({', '.join(missing)}). "
                "Use 'Approve For Interview' to ask the candidate for these, "
                "or fill them in on this application and try again."
            )

        token = db.ensure_interview_link(application_id)
        link = candidate_interview_url(token)
        db.mark_interview_link_sent(application_id)
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        body = recruiter_email_body(
            "We are good to move ahead with your interview.",
            f"Role: {role or 'Candidate'}",
            f"You can start it here whenever you are ready: {link}",
            "Please use a laptop or desktop with a working microphone, and choose a quiet place before starting.",
        )
        send_tracked_direct_email(
            db, mailer, application_id, recipient, "Interview link", body, "interview_link",
        )
        print(f"Interview link sent for application {application_id}: {link}")
    finally:
        db.close()


def send_pending_interview_reminders(
    hours_after_link: int = 24,
    reminder_gap_hours: int = 24,
    max_reminders: int = 1,
    limit: int = 100,
) -> int:
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    sent_count = 0
    try:
        db.init_schema()
        now = recruiter_now()
        link_cutoff = now - timedelta(hours=hours_after_link)
        reminder_cutoff = now - timedelta(hours=reminder_gap_hours)
        applications = db.pending_interview_reminders(
            link_cutoff=link_cutoff,
            reminder_cutoff=reminder_cutoff,
            max_reminders=max_reminders,
            limit=limit,
        )
        for application in applications:
            recipient = application_candidate_recipient(application)
            if not recipient:
                log_json(
                    logging.WARNING,
                    "interview_reminder_skipped_missing_recipient",
                    application_id=application.get("id"),
                )
                continue
            link = candidate_interview_url(application["interview_link_token"])
            role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
            body = recruiter_email_body(
                "I hope you are doing well.",
                f"This is a gentle reminder to complete your AI interview{f' for the {role} role' if role else ''}.",
                f"You can open the interview here: {link}",
                "Please use Google Chrome on a laptop or desktop, with a working microphone and a quiet place.",
                "Once you complete it, I will review your interview and get back to you with the next update.",
            )
            try:
                send_tracked_direct_email(
                    db, mailer, application["id"], recipient,
                    "Reminder: please complete your interview", body, "interview_reminder",
                )
                db.mark_interview_reminder_sent(application["id"])
                sent_count += 1
                log_json(
                    logging.INFO,
                    "interview_reminder_sent",
                    application_id=application.get("id"),
                    recipient=recipient,
                    reminder_count=(application.get("interview_reminder_count") or 0) + 1,
                    link_created_at=application.get("interview_link_created_at"),
                )
            except Exception as exc:
                LOGGER.exception("Could not send interview reminder for application %s: %s", application.get("id"), exc)
                log_json(
                    logging.ERROR,
                    "interview_reminder_failed",
                    application_id=application.get("id"),
                    recipient=recipient,
                    error=str(exc),
                )
        return sent_count
    finally:
        db.close()


def create_interview_link_for_application(application_id: int, reset: bool = False):
    db = RecruiterDatabase()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        token = db.ensure_interview_link(application_id, reset=reset)
        link = candidate_interview_url(token)
        print(link)
    finally:
        db.close()


def interview_report_recommendation(report: dict[str, Any]) -> str:
    overall_score = score_number(report.get("overall_score"))
    if overall_score is not None and overall_score > RECRUITER_INTERVIEW_PASS_SCORE:
        return "hire"
    if overall_score is not None and overall_score >= RECRUITER_INTERVIEW_HOLD_MIN_SCORE:
        return "hold"
    if overall_score is not None:
        return "reject"
    recommendation = str(report.get("recommendation") or "").strip().lower()
    if recommendation in {"strong_hire", "hire", "hold", "reject"}:
        return recommendation
    return "hold"


def interview_feedback_summary(report: dict[str, Any]) -> str:
    summary = str(report.get("summary") or "").strip()
    negatives = report.get("negative_points") or []
    if isinstance(negatives, str):
        negatives = [negatives]
    if summary:
        return summary
    if negatives:
        return "Areas noted during the interview: " + "; ".join(str(item) for item in negatives[:3])
    return "Based on the interview, we are not moving ahead with the next round at this time."


def send_final_hr_round_request(application_id: int, approved_by_hr: bool = False):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        db.mark_post_interview_outcome(application_id, "hr_round_time_requested")
        if not interview_actually_happened(application):
            # Advanced without an AI interview - a legitimate decision, but the
            # candidate must not be congratulated on clearing a round they never
            # sat.
            intro = "Thank you for your patience while we reviewed your application."
        elif approved_by_hr:
            intro = "Thank you for your patience while we reviewed your interview."
        else:
            intro = "Congratulations, you have cleared the AI technical interview round."
        body = recruiter_email_body(
            intro,
            f"We would like to move ahead with the final round with our HR Manager{f' for the {role} role' if role else ''}.",
            "Could you please share two or three date and time slots that work for you between Monday and Friday, 6 PM to 1 AM IST?",
            "Once you share your availability, I will schedule the meeting and send you the Teams link.",
        )
        send_tracked_direct_email(
            db, mailer, application_id, recipient,
            f"Final HR round availability{f' - {role}' if role else ''}", body, "final_hr_round_request",
        )
        print(f"Final HR round request sent for application {application_id}.")
    finally:
        db.close()


def interview_actually_happened(application: dict[str, Any]) -> bool:
    """Did this candidate sit the AI interview?

    Thirteen applications reached interview_on_hold_hr_review without one - the
    status can be set by hand from the dashboard, and nothing there checks. The
    post-interview emails talk about "the conversation we had", so they must not
    be sent to someone who never had it.
    """
    if application.get("interview_completed_at"):
        return True
    report = application.get("interview_report")
    if isinstance(report, str):
        try:
            report = json.loads(report or "{}")
        except ValueError:
            report = {}
    return bool(report)


def send_interview_rejection(application_id: int, reason: str | None = None):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        db.mark_post_interview_outcome(application_id, "interview_rejected", reason)
        if interview_actually_happened(application):
            body = recruiter_email_body(
                f"Thank you for taking the time to interview with me{f' for the {role} role' if role else ''}.",
                "I appreciate the effort you put into the conversation and the experience you shared.",
                "You did well in the discussion, but at the moment we have decided to move forward with another candidate whose profile is a closer match for this opening.",
                "Thank you again for your interest, and I wish you the very best in your job search.",
            )
        else:
            # No interview took place, so thanking them for one would be a lie
            # the candidate can see through.
            body = recruiter_email_body(
                f"Thank you for your interest{f' in the {role} role' if role else ''} and for the time you have spent with us.",
                "Having reviewed your application, we have decided to move forward with another candidate "
                "whose profile is a closer match for this opening.",
                "Thank you again, and I wish you the very best in your job search.",
            )
        send_tracked_direct_email(
            db, mailer, application_id, recipient,
            f"Interview feedback{f' - {role}' if role else ''}", body, "interview_feedback",
        )
        print(f"Interview rejection sent for application {application_id}.")
    finally:
        db.close()


def notify_post_interview_outcome(application_id: int, report: dict[str, Any]):
    recommendation = interview_report_recommendation(report)
    log_json(
        logging.INFO,
        "post_interview_recommendation_decided",
        application_id=application_id,
        overall_score=report.get("overall_score"),
        recommendation=recommendation,
        pass_score=RECRUITER_INTERVIEW_PASS_SCORE,
        hold_min_score=RECRUITER_INTERVIEW_HOLD_MIN_SCORE,
    )
    if recommendation in {"strong_hire", "hire"}:
        send_final_hr_round_request(application_id)
        return

    if recommendation == "reject":
        send_interview_rejection(application_id, interview_feedback_summary(report))
        return

    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        dashboard_url = dashboard_application_url(application_id)
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        reason = interview_feedback_summary(report)
        db.mark_post_interview_outcome(application_id, "interview_on_hold_hr_review", reason)
        candidate_email = application.get("candidate_email") or application.get("source_email") or "-"
        send_hr_notification(
            mailer,
            f"Interview review required: {application.get('full_name') or candidate_email}",
            recruiter_email_body(
                "Please review this completed AI interview.",
                f"Candidate: {application.get('full_name') or '-'}",
                f"Email: {candidate_email}",
                f"Role: {role or '-'}",
                f"Recommendation: {recommendation}",
                f"Overall score: {report.get('overall_score', '-')}",
                f"Reason: {reason}",
                f"Application dashboard: {dashboard_url}",
                "Please approve the candidate for the final HR round or reject the application from the dashboard.",
            ),
        )
        recipient = application_candidate_recipient(application)
        if recipient:
            mailer.send_direct_email(
                recipient,
                f"Interview update{f' - {role}' if role else ''}",
                recruiter_email_body(
                    "Thank you for completing the interview.",
                    "I am reviewing your interview details and will update you soon with the next step.",
                ),
            )
        print(f"Post-interview hold escalation sent for application {application_id}.")
    finally:
        db.close()


def jd_rejection_reason(
    evaluation: dict[str, Any],
    requirement: dict[str, Any] | None,
    extracted: dict[str, Any] | None = None,
) -> str:
    shortfall = experience_shortfall(requirement, extracted)
    if shortfall:
        return f"Below the experience requirement: {shortfall}."
    role = requirement.get("position_title") if requirement else None
    jd_score = evaluation.get("jd_match_score")
    recommendation = evaluation.get("recommendation")
    reasoning = evaluation.get("reasoning") or evaluation.get("short_description") or ""
    return (
        f"Rejected by AI based on JD match score"
        f"{f' for {role}' if role else ''}. "
        f"JD match score: {jd_score}; recommendation: {recommendation or '-'}. {reasoning}"
    ).strip()


def notify_jd_score_rejection_to_hr(application_id: int, evaluation: dict[str, Any], requirement: dict[str, Any] | None):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        dashboard_url = dashboard_application_url(application_id)
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        candidate_label = application.get("full_name") or application.get("candidate_email") or application.get("source_email") or application_id
        recipients = []
        for interviewer in final_hr_interviewers():
            email_address = interviewer.get("email")
            if email_address and clean_email(email_address) not in {clean_email(item) for item in recipients}:
                recipients.append(email_address)
        if RECRUITER_HR_ESCALATION_EMAIL and clean_email(RECRUITER_HR_ESCALATION_EMAIL) not in {clean_email(item) for item in recipients}:
            recipients.append(RECRUITER_HR_ESCALATION_EMAIL)
        body = recruiter_email_body(
            "The AI recruiter rejected this CV based on the JD match score.",
            f"Candidate: {candidate_label}",
            f"Role: {role or '-'}",
            f"ATS score: {evaluation.get('ats_score', '-')}",
            f"JD match score: {evaluation.get('jd_match_score', '-')}",
            f"Recommendation: {evaluation.get('recommendation') or '-'}",
            f"Reason: {evaluation.get('reasoning') or evaluation.get('short_description') or '-'}",
            f"Dashboard: {dashboard_url}",
            "The candidate has been told. If this was the wrong call, open the application "
            "and click Revoke JD Rejection to reopen it and send the screening questions.",
        )
        for recipient in recipients:
            mailer.send_direct_email(
                recipient,
                f"CV rejected by JD score: {candidate_label}",
                body,
            )
        print(f"JD-score rejection notification sent for application {application_id}.")
    finally:
        db.close()


def revoke_jd_score_rejection(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        screening_details = {
            "ats_score": application.get("ats_score"),
            "jd_match_score": application.get("jd_match_score"),
            "work_terms": screening_work_terms(),
            "rejection_revoked_by_hr": True,
        }
        db.update_application_screening(application_id, "screening_questions_sent", screening_details)
        mailer.send_direct_email(
            recipient,
            f"Application next steps{f' - {role}' if role else ''}",
            recruiter_email_body(
                f"Thank you for applying{f' for the {role} role' if role else ''}.",
                "We have reviewed your profile again and would like to continue with the next step.",
                "Please confirm if you are comfortable with night shift, work from office in Mohali, 5 days working, and cab facility available.",
                "Also please share your current salary, expected salary, current location, and how soon you can join.",
            ),
        )
        print(f"JD-score rejection revoked for application {application_id}.")
    finally:
        db.close()


def send_teams_link_for_application(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")

        scheduled_at = parse_iso_datetime(application.get("interview_scheduled_at"))
        if not scheduled_at:
            scheduled_at = parse_interview_datetime_fallback(application.get("interview_availability") or "")
        if not scheduled_at:
            raise RuntimeError(
                f"Application {application_id} does not have a clear interview_scheduled_at or availability."
            )
        scheduled_at = coerce_final_hr_slot(scheduled_at)

        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        subject = f"Interview - {role or 'Candidate'}"
        interviewer = choose_final_hr_interviewer(db, scheduled_at, application.get("hr_interviewer_email"))
        end_at = scheduled_at + timedelta(minutes=FINAL_HR_DEFAULT_DURATION_MINUTES)
        body = recruiter_email_body(
            "Your interview has been scheduled.",
            f"Role: {role or 'Candidate'}",
            f"Time: {recruiter_time_text(scheduled_at)}",
            f"Interviewer: {interviewer['name']}",
        )
        calendar_provider = MicrosoftGraphProvider(interviewer["email"])
        event_id, join_url = calendar_provider.create_teams_calendar_event(
            recipient,
            subject,
            scheduled_at,
            end_at,
            body,
        )
        if not join_url:
            join_url = calendar_provider.create_online_meeting(subject, scheduled_at, end_at)
        if not join_url:
            raise RuntimeError("Microsoft Graph created no Teams join URL.")
        old_event_id = application.get("teams_event_id")
        if event_id and old_event_id and old_event_id != event_id:
            old_calendar_provider = MicrosoftGraphProvider(application.get("hr_interviewer_email") or interviewer["email"])
            cancelled = old_calendar_provider.cancel_calendar_event(
                old_event_id,
                f"This interview has been rescheduled to {recruiter_time_text(scheduled_at)}.",
            )
            print(
                "Previous Teams calendar event "
                f"{'cancelled' if cancelled else 'not found'} for application {application_id}: {old_event_id}"
            )

        db.update_interview_schedule(
            application_id,
            "interview_scheduled",
            availability=application.get("interview_availability"),
            scheduled_at=scheduled_at,
            teams_event_id=event_id or None,
            teams_join_url=join_url,
            interviewer_email=interviewer["email"],
            interviewer_name=interviewer["name"],
        )
        send_tracked_direct_email(
            db,
            mailer,
            application_id,
            recipient,
            "Microsoft Teams interview link",
            recruiter_email_body(
                "Thank you for confirming your availability.",
                f"Your interview has been scheduled for {recruiter_time_text(scheduled_at)}.",
                f"You will be speaking with {interviewer['name']}.",
                f"Teams link: {join_url}",
            ),
            "interview_scheduled",
        )
        print(f"Teams link sent for application {application_id}: {join_url}")
    finally:
        db.close()


def schedule_final_hr_round_direct(
    application_id: int,
    scheduled_at: datetime,
    interviewer_email: str | None = None,
    availability_note: str | None = None,
):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        scheduled_at = coerce_final_hr_slot(scheduled_at)
        if not scheduled_at:
            raise RuntimeError("A valid final HR round date/time is required.")
        interviewer = choose_final_hr_interviewer(db, scheduled_at, interviewer_email)
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        subject = f"Final HR discussion - {role or 'Candidate'}"
        end_at = scheduled_at + timedelta(minutes=FINAL_HR_DEFAULT_DURATION_MINUTES)
        event_body = recruiter_email_body(
            "Your final HR discussion has been scheduled.",
            f"Role: {role or 'Candidate'}",
            f"Time: {recruiter_time_text(scheduled_at)}",
            f"Interviewer: {interviewer['name']}",
        )
        calendar_provider = MicrosoftGraphProvider(interviewer["email"])
        event_id, join_url = calendar_provider.create_teams_calendar_event(
            recipient,
            subject,
            scheduled_at,
            end_at,
            event_body,
        )
        if not join_url:
            join_url = calendar_provider.create_online_meeting(subject, scheduled_at, end_at)
        if not join_url:
            raise RuntimeError("Microsoft Graph created no Teams join URL.")
        old_event_id = application.get("teams_event_id")
        if event_id and old_event_id and old_event_id != event_id:
            old_calendar_provider = MicrosoftGraphProvider(application.get("hr_interviewer_email") or interviewer["email"])
            old_calendar_provider.cancel_calendar_event(
                old_event_id,
                f"This final HR discussion has been rescheduled to {recruiter_time_text(scheduled_at)}.",
            )
        db.update_interview_schedule(
            application_id,
            "interview_scheduled",
            availability=availability_note or application.get("interview_availability"),
            scheduled_at=scheduled_at,
            teams_event_id=event_id,
            teams_join_url=join_url,
            interviewer_email=interviewer["email"],
            interviewer_name=interviewer["name"],
        )
        mailer.send_direct_email(
            recipient,
            f"Final HR discussion scheduled{f' - {role}' if role else ''}",
            recruiter_email_body(
                "Thank you for confirming your availability.",
                f"Your final HR discussion has been scheduled for {recruiter_time_text(scheduled_at)}.",
                f"You will be speaking with {interviewer['name']}.",
                f"Teams link: {join_url}",
            ),
        )
        print(f"Final HR round scheduled for application {application_id}: {join_url}")
    finally:
        db.close()


def final_hr_round_due(application: dict[str, Any], grace_minutes: int = 5) -> bool:
    scheduled_at = parse_iso_datetime(application.get("interview_scheduled_at"))
    if not scheduled_at and isinstance(application.get("interview_scheduled_at"), datetime):
        scheduled_at = application.get("interview_scheduled_at")
    if not scheduled_at:
        return False
    return as_recruiter_time(scheduled_at) + timedelta(minutes=grace_minutes) <= recruiter_now()


def mark_due_final_hr_rounds_pending(grace_minutes: int = 5) -> int:
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    marked = 0
    try:
        db.init_schema()
        for row in db.scheduled_hr_rounds_for_decision_check():
            if not final_hr_round_due(row, grace_minutes=grace_minutes):
                continue
            application = db.application_with_requirement(row["id"])
            if not application:
                continue
            dashboard_url = dashboard_application_url(application["id"])
            role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
            scheduled_at = parse_iso_datetime(application.get("interview_scheduled_at"))
            scheduled_text = recruiter_time_text(scheduled_at) if scheduled_at else str(application.get("interview_scheduled_at") or "-")
            db.mark_post_interview_outcome(
                application["id"],
                "final_hr_round_completed_pending_decision",
                "Final HR Manager round is complete; HR decision is pending.",
            )
            send_hr_notification(
                mailer,
                f"Final HR decision required: {application.get('full_name') or application.get('candidate_email') or application['id']}",
                recruiter_email_body(
                    "The final HR Manager round appears to be completed.",
                    f"Candidate: {application.get('full_name') or '-'}",
                    f"Role: {role or '-'}",
                    f"Scheduled time: {scheduled_text}",
                    f"Application dashboard: {dashboard_url}",
                    "Please open the dashboard and select, reject, hold, or reschedule the candidate.",
                ),
            )
            marked += 1
    finally:
        db.close()
    return marked


def select_candidate_after_hr_round(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        db.mark_post_interview_outcome(application_id, "selected_documents_requested")
        mailer.send_direct_email(
            recipient,
            f"Selection update{f' - {role}' if role else ''}",
            recruiter_email_body(
                "Congratulations, you have been selected for the next onboarding step.",
                f"We are happy to move ahead with your profile{f' for the {role} role' if role else ''}.",
                "Please share the required joining documents so I can proceed with the onboarding formalities.",
                "Documents required: PAN card, Aadhaar card, address proof, latest salary slips, relieving letter if applicable, bank details, and emergency contact details.",
            ),
        )
        print(f"Selection email sent for application {application_id}.")
    finally:
        db.close()


def reject_candidate_after_hr_round(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        db.mark_post_interview_outcome(
            application_id,
            "rejected_after_hr_round",
            "Rejected after final HR Manager round.",
        )
        mailer.send_direct_email(
            recipient,
            f"Interview update{f' - {role}' if role else ''}",
            recruiter_email_body(
                f"Thank you for taking the time to speak with us{f' for the {role} role' if role else ''}.",
                "You did well in the discussion, but at the moment we have decided to move forward with another candidate whose profile is a closer match for this opening.",
                "I appreciate your time and interest, and I wish you the very best in your job search.",
            ),
        )
        print(f"Final HR rejection email sent for application {application_id}.")
    finally:
        db.close()


def hold_candidate_after_hr_round(application_id: int):
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        db.mark_post_interview_outcome(
            application_id,
            "hold_after_hr_round",
            "Candidate is on hold after final HR Manager round.",
        )
        mailer.send_direct_email(
            recipient,
            f"Interview update{f' - {role}' if role else ''}",
            recruiter_email_body(
                "Thank you for your time in the final discussion.",
                "I am still going through this and will update you soon with the next step.",
            ),
        )
        print(f"Final HR hold email sent for application {application_id}.")
    finally:
        db.close()


def request_hr_round_reschedule(
    application_id: int,
    scheduled_at: datetime | None = None,
    interviewer_email: str | None = None,
    ask_candidate: bool = True,
):
    if scheduled_at and not ask_candidate:
        schedule_final_hr_round_direct(
            application_id,
            scheduled_at,
            interviewer_email=interviewer_email,
            availability_note="Scheduled directly from dashboard.",
        )
        return
    db = RecruiterDatabase()
    mailer = MicrosoftGraphProvider() if MAIL_PROVIDER.lower() in {"graph", "microsoft_graph", "outlook_graph"} else RecruiterMailer()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        recipient = application_candidate_recipient(application)
        if not recipient:
            raise RuntimeError(f"Application {application_id} does not have a candidate/source email.")
        role = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        old_event_id = application.get("teams_event_id")
        if old_event_id and isinstance(mailer, MicrosoftGraphProvider):
            try:
                old_calendar_provider = MicrosoftGraphProvider(application.get("hr_interviewer_email") or MICROSOFT_MAILBOX)
                old_calendar_provider.cancel_calendar_event(
                    old_event_id,
                    "This final HR discussion needs to be rescheduled.",
                )
            except Exception as exc:
                print(f"Could not cancel old final HR calendar event for application {application_id}: {exc}")
        db.mark_post_interview_outcome(
            application_id,
            "hr_round_time_requested",
            "Final HR round reschedule requested.",
        )
        preferred_interviewer = final_hr_interviewer_by_email(interviewer_email) or {
            "email": application.get("hr_interviewer_email"),
            "name": application.get("hr_interviewer_name"),
        }
        db.update_interview_schedule(
            application_id,
            "hr_round_time_requested",
            availability=application.get("interview_availability"),
            scheduled_at=None,
            teams_event_id=None,
            teams_join_url=None,
            interviewer_email=preferred_interviewer.get("email"),
            interviewer_name=preferred_interviewer.get("name"),
        )
        mailer.send_direct_email(
            recipient,
            f"Reschedule final HR round{f' - {role}' if role else ''}",
            recruiter_email_body(
                "We need to reschedule your final HR Manager round.",
                "Could you please share two or three date and time slots that work for you between Monday and Friday, 6 PM to 1 AM IST?",
                "Once you share your availability, I will schedule the meeting and send you the Teams link.",
            ),
        )
        print(f"Final HR reschedule request sent for application {application_id}.")
    finally:
        db.close()


def show_teams_interview_info(application_id: int):
    db = RecruiterDatabase()
    try:
        db.init_schema()
        application = db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        join_url = application.get("teams_join_url")
        if not join_url:
            raise RuntimeError(f"Application {application_id} does not have a Teams join URL yet.")

        scheduled_at = parse_iso_datetime(application.get("interview_scheduled_at"))
        provider = MicrosoftGraphProvider()
        meeting = provider.get_online_meeting_by_join_url(join_url)
        output = {
            "application_id": application_id,
            "candidate_email": application.get("candidate_email") or application.get("source_email"),
            "role": application.get("requirement_position") or application.get("matched_position") or application.get("detected_position"),
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else application.get("interview_scheduled_at"),
            "scheduled_at_ist": recruiter_time_text(scheduled_at) if scheduled_at else None,
            "teams_event_id": application.get("teams_event_id"),
            "teams_join_url": join_url,
            "online_meeting": {
                "id": meeting.get("id") if meeting else None,
                "subject": meeting.get("subject") if meeting else None,
                "joinMeetingIdSettings": meeting.get("joinMeetingIdSettings") if meeting else None,
                "participants": meeting.get("participants") if meeting else None,
            },
            "next_step": (
                "Use this meeting metadata from a Teams calling/meeting bot. "
                "The terminal voice interviewer cannot directly join Teams as a participant."
            ),
        }
        print(json.dumps(output, indent=2, default=str))
    finally:
        db.close()


class GmailPushWebhookServer:
    def __init__(self):
        self.agent = AIRecruiterAgent()
        self.lock = Lock()

    def process_notification(self, notification: dict[str, Any]):
        with self.lock:
            self.agent.handle_gmail_push(notification)

    def make_handler(self):
        server = self

        class GmailPushHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != GMAIL_WEBHOOK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode("utf-8"))
                    encoded_data = payload.get("message", {}).get("data", "")
                    decoded_data = base64.urlsafe_b64decode(encoded_data + "===")
                    notification = json.loads(decoded_data.decode("utf-8"))
                except Exception as exc:
                    print(f"Invalid Pub/Sub push payload: {exc}")
                    self.send_response(400)
                    self.end_headers()
                    return

                Thread(
                    target=server.process_notification,
                    args=(notification,),
                    daemon=True,
                ).start()

                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                print(f"[gmail-webhook] {format % args}")

        return GmailPushHandler

    def serve_forever(self):
        self.agent.init_schema()
        httpd = ReusableThreadingHTTPServer(
            (GMAIL_WEBHOOK_HOST, GMAIL_WEBHOOK_PORT),
            self.make_handler(),
        )
        print(
            f"Gmail Pub/Sub webhook listening on "
            f"http://{GMAIL_WEBHOOK_HOST}:{GMAIL_WEBHOOK_PORT}{GMAIL_WEBHOOK_PATH}"
        )
        print("Configure Pub/Sub push subscription to call this endpoint.")
        try:
            httpd.serve_forever()
        finally:
            self.agent.close()


class GraphWebhookServer:
    def __init__(self):
        self.lock = Lock()
        self.retry_lock = Lock()
        self.pending_retries: set[tuple[str, str]] = set()

    def notification_is_for_configured_mailbox(self, notification: dict[str, Any]) -> bool:
        resource = (notification.get("resource") or "").lower()
        if not resource:
            return True
        normalized_resource = resource.replace("%40", "@")
        expected_prefix = f"users/{MICROSOFT_MAILBOX.lower()}/mailfolders('inbox')/messages"
        expected_prefix_alt = f"users/{quote(MICROSOFT_MAILBOX.lower(), safe='')}/mailfolders('inbox')/messages"
        if normalized_resource.startswith(expected_prefix) or resource.startswith(expected_prefix_alt):
            return True

        match = re.match(r"users/([^/]+)/", normalized_resource, flags=re.I)
        if not match:
            return True

        user_part = match.group(1)
        if "@" not in user_part:
            return True
        return clean_email(user_part) == clean_email(MICROSOFT_MAILBOX)

    def notification_message_id(self, notification: dict[str, Any]) -> str | None:
        resource_data = notification.get("resourceData") or {}
        if resource_data.get("id"):
            return resource_data["id"]

        resource = notification.get("resource") or ""
        match = re.search(r"/messages/([^/?]+)", resource, flags=re.I)
        if match:
            return match.group(1)
        return None

    def process_notifications(self, notifications: list[dict[str, Any]]):
        message_refs = []
        for notification in notifications:
            client_state = notification.get("clientState")
            if GRAPH_CLIENT_STATE and client_state != GRAPH_CLIENT_STATE:
                subscription_id = notification.get("subscriptionId") or "unknown"
                expected = short_fingerprint(GRAPH_CLIENT_STATE)
                received = short_fingerprint(client_state or "")
                print(
                    "Ignoring Microsoft Graph notification with invalid clientState "
                    f"(subscription={subscription_id}, expected={expected}, received={received})."
                )
                continue

            if not self.notification_is_for_configured_mailbox(notification):
                subscription_id = notification.get("subscriptionId") or "unknown"
                resource = notification.get("resource") or "unknown"
                print(
                    "Ignoring Microsoft Graph notification for a different mailbox "
                    f"(subscription={subscription_id}, resource={resource}). "
                    "Run --reset-graph-subscription to remove stale subscriptions."
                )
                continue

            message_id = self.notification_message_id(notification)
            if not message_id:
                print(f"Microsoft Graph notification did not include a message id: {notification}")
                continue
            message_refs.append((message_id, notification.get("resource") or ""))

        if not message_refs:
            return

        failed_refs = []
        with self.lock:
            unique_message_refs = list(dict.fromkeys(message_refs))
            log_json(
                logging.INFO,
                "graph_notification_received",
                message_count=len(unique_message_refs),
                message_refs=[
                    {"message_id": message_id, "resource_path": resource_path}
                    for message_id, resource_path in unique_message_refs
                ],
            )
            trace_recruiter_event(
                "graph_notification_received",
                inputs={
                    "message_count": len(unique_message_refs),
                    "message_refs": [
                        {"message_id": message_id, "resource_path": resource_path}
                        for message_id, resource_path in unique_message_refs
                    ],
                },
                tags=["graph", "webhook"],
            )
            agent = None
            try:
                agent = AIRecruiterAgent()
                for message_id, resource_path in unique_message_refs:
                    processed = agent.process_one_graph_message(message_id, resource_path=resource_path)
                    if not processed:
                        failed_refs.append((message_id, resource_path))
            except Exception as exc:
                LOGGER.exception("Microsoft Graph notification processing failed: %s", exc)
                trace_recruiter_event(
                    "graph_notification_processing_failed",
                    inputs={
                        "message_refs": [
                            {"message_id": message_id, "resource_path": resource_path}
                            for message_id, resource_path in unique_message_refs
                        ],
                    },
                    outputs={"error": str(exc)},
                    tags=["graph", "webhook", "error"],
                )
                failed_refs.extend(unique_message_refs)
            finally:
                if agent:
                    agent.close()

        for message_id, resource_path in failed_refs:
            self.schedule_retry(message_id, resource_path)

    def schedule_retry(self, message_id: str, resource_path: str):
        retry_key = (message_id, resource_path or "")
        with self.retry_lock:
            if retry_key in self.pending_retries:
                log_json(logging.INFO, "graph_retry_already_scheduled", graph_message_id=message_id)
                trace_recruiter_event(
                    "graph_retry_already_scheduled",
                    inputs={"graph_message_id": message_id, "resource_path": resource_path},
                    tags=["graph", "retry"],
                )
                return
            self.pending_retries.add(retry_key)

        log_json(
            logging.INFO,
            "graph_retry_scheduled",
            graph_message_id=message_id,
            resource_path=resource_path,
            retry_seconds=GRAPH_PROCESSING_RETRY_SECONDS,
        )
        trace_recruiter_event(
            "graph_retry_scheduled",
            inputs={"graph_message_id": message_id, "resource_path": resource_path},
            outputs={"retry_seconds": GRAPH_PROCESSING_RETRY_SECONDS},
            tags=["graph", "retry"],
        )
        Thread(
            target=self.retry_message_after_delay,
            args=(message_id, resource_path or ""),
            daemon=True,
        ).start()

    def retry_message_after_delay(self, message_id: str, resource_path: str):
        try:
            time.sleep(GRAPH_PROCESSING_RETRY_SECONDS)
            with self.lock:
                agent = None
                try:
                    agent = AIRecruiterAgent()
                    processed = agent.process_one_graph_message(message_id, resource_path=resource_path)
                    if processed:
                        log_json(logging.INFO, "graph_retry_processed", graph_message_id=message_id)
                        trace_recruiter_event(
                            "graph_retry_processed",
                            inputs={"graph_message_id": message_id, "resource_path": resource_path},
                            tags=["graph", "retry"],
                        )
                    else:
                        log_json(logging.WARNING, "graph_retry_failed_left_unread", graph_message_id=message_id)
                        trace_recruiter_event(
                            "graph_retry_failed_left_unread",
                            inputs={"graph_message_id": message_id, "resource_path": resource_path},
                            tags=["graph", "retry", "warning"],
                        )
                except Exception as exc:
                    LOGGER.exception(
                        "Microsoft Graph retry crashed; message was left unread: %s: %s",
                        message_id,
                        exc,
                    )
                    trace_recruiter_event(
                        "graph_retry_crashed",
                        inputs={"graph_message_id": message_id, "resource_path": resource_path},
                        outputs={"error": str(exc)},
                        tags=["graph", "retry", "error"],
                    )
                finally:
                    if agent:
                        agent.close()
        finally:
            with self.retry_lock:
                self.pending_retries.discard((message_id, resource_path or ""))

    def make_handler(self):
        server = self

        class GraphWebhookHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.handle_validation_or_not_found():
                    return
                if urlparse(self.path).path == "/health":
                    data = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if self.handle_validation_or_not_found():
                    return
                if urlparse(self.path).path != GRAPH_WEBHOOK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(content_length)
                    payload = json.loads(body.decode("utf-8")) if body else {}
                    notifications = payload.get("value", [])
                    if not isinstance(notifications, list):
                        raise ValueError("Microsoft Graph payload `value` must be a list")
                except Exception as exc:
                    print(f"Invalid Microsoft Graph webhook payload: {exc}")
                    self.send_response(400)
                    self.end_headers()
                    return

                Thread(
                    target=server.process_notifications,
                    args=(notifications,),
                    daemon=True,
                ).start()

                self.send_response(202)
                self.end_headers()

            def handle_validation_or_not_found(self) -> bool:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                validation_token = query.get("validationToken", [None])[0]
                if validation_token is None:
                    return False
                if parsed.path != GRAPH_WEBHOOK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return True
                data = validation_token.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return True

            def log_message(self, format, *args):
                print(f"[graph-webhook] {format % args}")

        return GraphWebhookHandler

    def keep_subscription_alive(self):
        """Renew the Inbox subscription well before Graph expires it.

        Subscriptions last at most 48 hours and nothing renewed them, so mail
        processing simply stopped whenever one lapsed - silently, with no error
        and no log line. Runs for the life of the webhook process.
        """
        interval = max(600, int(GRAPH_SUBSCRIPTION_HOURS * 3600 / 3))
        while True:
            try:
                provider = MicrosoftGraphProvider()
                provider.renew_or_create_inbox_subscription(
                    GRAPH_NOTIFICATION_URL,
                    GRAPH_CLIENT_STATE,
                    GRAPH_SUBSCRIPTION_HOURS,
                )
            except Exception as exc:
                LOGGER.exception("Graph subscription renewal failed: %s", exc)
                log_json(logging.ERROR, "graph_subscription_renew_error", error=str(exc)[:300])
            time.sleep(interval)

    def drain_action_queue_forever(self):
        """Perform whatever an external dashboard has asked for."""
        while True:
            try:
                process_action_queue()
            except Exception as exc:
                LOGGER.exception("Action queue pass failed: %s", exc)
            time.sleep(ACTION_QUEUE_POLL_SECONDS)

    def serve_forever(self):
        httpd = ReusableThreadingHTTPServer(
            (GRAPH_WEBHOOK_HOST, GRAPH_WEBHOOK_PORT),
            self.make_handler(),
        )
        print(
            f"Microsoft Graph webhook listening on "
            f"http://{GRAPH_WEBHOOK_HOST}:{GRAPH_WEBHOOK_PORT}{GRAPH_WEBHOOK_PATH}"
        )
        if GRAPH_NOTIFICATION_URL and GRAPH_CLIENT_STATE:
            Thread(target=self.keep_subscription_alive, daemon=True).start()
            print("Subscription auto-renewal is running.")
        else:
            print("GRAPH_NOTIFICATION_URL/GRAPH_CLIENT_STATE not set; auto-renewal is OFF.")
        Thread(target=self.drain_action_queue_forever, daemon=True).start()
        print("Watching agent_action_queue for dashboard requests.")
        httpd.serve_forever()


def build_gmail_service():
    if not GMAIL_PUBSUB_TOPIC:
        raise RuntimeError("GMAIL_PUBSUB_TOPIC is required, for example projects/my-project/topics/gmail-inbox")

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google API dependencies. Run: "
            "./venv/bin/python -m pip install -r requirements-recruiter.txt"
        ) from exc

    token_path = Path(GMAIL_TOKEN_FILE)
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            client_secret_path = Path(GMAIL_CREDENTIALS_FILE)
            if not client_secret_path.exists():
                raise RuntimeError(
                    f"Gmail OAuth client file not found: {client_secret_path}. "
                    "Create it in Google Cloud Console and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), GMAIL_SCOPES)
            credentials = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json())

    return build("gmail", "v1", credentials=credentials)


def register_gmail_watch():
    gmail = build_gmail_service()
    request = {
        "labelIds": ["INBOX"],
        "topicName": GMAIL_PUBSUB_TOPIC,
        "labelFilterBehavior": "INCLUDE",
    }
    response = gmail.users().watch(userId="me", body=request).execute()
    print("Gmail watch registered.")
    print(json.dumps(response, indent=2))
    print("Renew this watch at least once every 7 days. Google recommends daily renewal.")


def register_graph_subscription():
    provider = MicrosoftGraphProvider()
    response = provider.create_inbox_subscription(
        GRAPH_NOTIFICATION_URL,
        GRAPH_CLIENT_STATE,
        GRAPH_SUBSCRIPTION_HOURS,
    )
    print("Microsoft Graph subscription registered.")
    print(json.dumps(response, indent=2))
    print("Renew this subscription before expirationDateTime.")


def reset_graph_subscription():
    provider = MicrosoftGraphProvider()
    response = provider.create_inbox_subscription(
        GRAPH_NOTIFICATION_URL,
        GRAPH_CLIENT_STATE,
        GRAPH_SUBSCRIPTION_HOURS,
    )
    deleted = provider.delete_matching_inbox_subscriptions(
        GRAPH_NOTIFICATION_URL,
        exclude_subscription_id=response.get("id"),
    )
    print(f"Deleted {len(deleted)} existing Microsoft Graph Inbox subscription(s).")
    print("Microsoft Graph subscription registered.")
    print(json.dumps(response, indent=2))
    print("Renew this subscription before expirationDateTime.")


def list_graph_subscriptions():
    provider = MicrosoftGraphProvider()
    subscriptions = provider.list_subscriptions()
    print(json.dumps(subscriptions, indent=2))


def delete_graph_subscription(subscription_id: str):
    provider = MicrosoftGraphProvider()
    provider.delete_subscription(subscription_id)
    print(f"Microsoft Graph subscription deleted: {subscription_id}")


def get_ngrok_https_url() -> str:
    try:
        with urllib.request.urlopen(NGROK_API_URL, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "Could not read ngrok tunnel URL. Start ngrok first, for example: "
            "ngrok http 8081"
        ) from exc

    tunnels = payload.get("tunnels", [])
    for tunnel in tunnels:
        public_url = tunnel.get("public_url", "")
        if public_url.startswith("https://"):
            return public_url.rstrip("/")

    raise RuntimeError("ngrok is running, but no HTTPS tunnel was found.")


def register_graph_subscription_from_ngrok():
    public_url = get_ngrok_https_url()
    notification_url = f"{public_url}{GRAPH_WEBHOOK_PATH}"
    provider = MicrosoftGraphProvider()
    response = provider.create_inbox_subscription(
        notification_url,
        GRAPH_CLIENT_STATE,
        GRAPH_SUBSCRIPTION_HOURS,
    )
    print("Microsoft Graph subscription registered through ngrok.")
    print(f"Notification URL: {notification_url}")
    print(json.dumps(response, indent=2))
    print("Renew this subscription before expirationDateTime. Free ngrok URLs change when the tunnel restarts.")


def reset_graph_subscription_from_ngrok():
    public_url = get_ngrok_https_url()
    notification_url = f"{public_url}{GRAPH_WEBHOOK_PATH}"
    provider = MicrosoftGraphProvider()
    response = provider.create_inbox_subscription(
        notification_url,
        GRAPH_CLIENT_STATE,
        GRAPH_SUBSCRIPTION_HOURS,
    )
    deleted = provider.delete_matching_inbox_subscriptions(
        notification_url,
        exclude_subscription_id=response.get("id"),
    )
    print(f"Deleted {len(deleted)} existing Microsoft Graph Inbox subscription(s).")
    print("Microsoft Graph subscription registered through ngrok.")
    print(f"Notification URL: {notification_url}")
    print(json.dumps(response, indent=2))
    print("Renew this subscription before expirationDateTime. Free ngrok URLs change when the tunnel restarts.")


def main():
    parser = argparse.ArgumentParser(description="AI recruiter inbox agent")
    parser.add_argument("--init-db", action="store_true", help="Create recruiter database tables and exit")
    parser.add_argument("--run-once", action="store_true", help="Process unread inbox emails once")
    parser.add_argument(
        "--list-claimed",
        nargs="?",
        const=20,
        type=int,
        help="List recently claimed inbound message ids (idempotency ledger)",
    )
    parser.add_argument(
        "--release-message",
        help="Release a claimed message id so it can be processed again",
    )
    parser.add_argument(
        "--rematch-parked",
        action="store_true",
        help=(
            "Re-run matching over applications parked in manual_hr_review / no_open_requirement. "
            "Dry run unless --apply is also given."
        ),
    )
    parser.add_argument(
        "--rematch-status",
        action="append",
        help="Status to include in --rematch-parked (repeatable; default manual_hr_review and no_open_requirement)",
    )
    parser.add_argument(
        "--rematch-limit",
        type=int,
        default=100,
        help="Maximum applications to consider in --rematch-parked",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --rematch-parked, actually write the changes and email candidates",
    )
    parser.add_argument(
        "--renew-graph-subscription",
        action="store_true",
        help="Renew (or recreate) the Outlook Inbox subscription now",
    )
    parser.add_argument(
        "--list-unread",
        nargs="?",
        const=20,
        type=int,
        help="List unread inbox messages the agent has not answered, with their claim state",
    )
    parser.add_argument(
        "--list-human-handled",
        action="store_true",
        help="List every application the agent has stopped replying to (manual_hr_review, hr_escalated, human_handled, ...)",
    )
    parser.add_argument(
        "--process-actions",
        action="store_true",
        help="Run any actions an external dashboard has queued, then exit",
    )
    parser.add_argument(
        "--watch-actions",
        action="store_true",
        help="Continuously run actions queued by an external dashboard",
    )
    parser.add_argument(
        "--reopen-interview",
        type=int,
        help="Clear the interview attempt counter for an application so the candidate can start again",
    )
    parser.add_argument(
        "--show-candidate",
        help="Show everything stored for one candidate email address: status, scores, interview state, emails sent",
    )
    parser.add_argument(
        "--resume-application",
        type=int,
        help="Hand an application back to the agent, restoring the status it had before the takeover latch",
    )
    parser.add_argument(
        "--resume-status",
        default="hr_round_time_requested",
        help="Status to restore with --resume-application (default: hr_round_time_requested)",
    )
    parser.add_argument(
        "--forget-candidate",
        help=(
            "Delete every application, candidate row, reply-ledger entry and message claim "
            "for an email address so their mail can be processed from scratch (testing aid)"
        ),
    )
    parser.add_argument(
        "--release-failed-messages",
        action="store_true",
        help="Release every claimed message that has a processing_failed event and no successful reply",
    )
    parser.add_argument("--watch", action="store_true", help="Continuously watch inbox for unread emails")
    parser.add_argument("--serve-gmail-webhook", action="store_true", help="Receive Gmail Pub/Sub push triggers")
    parser.add_argument("--register-gmail-watch", action="store_true", help="Register Gmail API watch for INBOX")
    parser.add_argument("--serve-graph-webhook", action="store_true", help="Receive Microsoft Graph Outlook webhook triggers")
    parser.add_argument("--register-graph-subscription", action="store_true", help="Register Microsoft Graph subscription for Outlook inbox")
    parser.add_argument("--reset-graph-subscription", action="store_true", help="Delete old Outlook Inbox subscriptions and register a fresh subscription")
    parser.add_argument("--register-graph-ngrok", action="store_true", help="Register Microsoft Graph subscription using the current ngrok HTTPS tunnel")
    parser.add_argument("--reset-graph-ngrok", action="store_true", help="Delete old Outlook Inbox subscriptions and register a fresh ngrok subscription")
    parser.add_argument("--list-graph-subscriptions", action="store_true", help="List Microsoft Graph subscriptions for this app")
    parser.add_argument("--delete-graph-subscription", help="Delete a Microsoft Graph subscription by id")
    parser.add_argument("--create-interview-link", type=int, help="Create/print the browser AI interview link without sending email")
    parser.add_argument("--reset-interview-link", action="store_true", help="With --create-interview-link, clear previous interview completion and create a fresh token")
    parser.add_argument("--send-interview-link", type=int, help="Send the browser AI interview link for an application id")
    parser.add_argument("--send-interview-reminders", action="store_true", help="Send reminder emails for AI interview links older than 24 hours")
    parser.add_argument("--interview-reminder-hours", type=int, default=24, help="Hours after interview link creation before reminder is sent")
    parser.add_argument("--interview-reminder-gap-hours", type=int, default=24, help="Minimum hours between interview reminder emails")
    parser.add_argument("--interview-reminder-max", type=int, default=1, help="Maximum reminders per application")
    parser.add_argument("--interview-reminder-limit", type=int, default=100, help="Maximum reminder emails to send in one job run")
    parser.add_argument("--send-teams-link", type=int, help="Create/send Teams interview link for an application id")
    parser.add_argument("--teams-interview-info", type=int, help="Show Teams meeting metadata for a scheduled application")
    parser.add_argument("--mark-due-hr-rounds", action="store_true", help="Mark completed final HR rounds pending HR decision and notify HR")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=RECRUITER_POLL_SECONDS,
        help="Seconds between inbox checks in watch mode",
    )
    args = parser.parse_args()

    if args.list_claimed:
        db = RecruiterDatabase()
        try:
            rows = db.rows(
                "SELECT provider_message_id, processed_at FROM recruiter_processed_messages "
                "ORDER BY processed_at DESC LIMIT %s",
                (args.list_claimed,),
            )
            if not rows:
                print("No claimed messages.")
            for row in rows:
                print(f"{row['processed_at']}  {row['provider_message_id']}")
        finally:
            db.close()
        return

    if args.release_message:
        db = RecruiterDatabase()
        try:
            db.release_provider_message(args.release_message)
            print(f"Released {args.release_message}. It will be processed on the next pass.")
        finally:
            db.close()
        return

    if args.rematch_parked:
        statuses = tuple(args.rematch_status) if args.rematch_status else REMATCH_DEFAULT_STATUSES
        rematch_stored_applications(
            statuses=statuses,
            apply_changes=bool(args.apply),
            limit=args.rematch_limit,
        )
        return

    if args.renew_graph_subscription:
        provider = MicrosoftGraphProvider()
        result = provider.renew_or_create_inbox_subscription(
            GRAPH_NOTIFICATION_URL, GRAPH_CLIENT_STATE, GRAPH_SUBSCRIPTION_HOURS
        )
        print(f"Subscription {result.get('id')} valid until {result.get('expirationDateTime')}.")
        return

    if args.list_unread:
        provider = build_inbox_provider()
        if not isinstance(provider, MicrosoftGraphProvider):
            print("--list-unread requires MAIL_PROVIDER=microsoft_graph.")
            return
        db = RecruiterDatabase()
        try:
            data = provider.request(
                "GET",
                f"/users/{provider.mailbox}/mailFolders/inbox/messages",
                params={
                    "$filter": "isRead eq false",
                    "$top": str(args.list_unread),
                    "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,from,receivedDateTime",
                },
            )
            rows = data.get("value", [])
            if not rows:
                print("No unread inbox messages.")
            for message in rows:
                sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "?")
                claimed = db.one(
                    "SELECT processed_at FROM recruiter_processed_messages WHERE provider_message_id = %s",
                    (message["id"],),
                )
                state = f"claimed at {claimed['processed_at']}" if claimed else "not claimed"
                print(f"  {message['receivedDateTime']}  {sender:<34} {message['subject'][:44]:<46} [{state}]")
        finally:
            db.close()
        return

    if args.list_human_handled:
        db = RecruiterDatabase()
        try:
            # Every status where the agent has stopped replying, not just
            # human_handled - manual_hr_review holds a thread just as firmly.
            holds = tuple(sorted(HUMAN_HOLD_STATUSES))
            placeholders = ", ".join(["%s"] * len(holds))
            rows = db.rows(
                f"""
                SELECT id, candidate_email, source_email, application_status,
                       human_handled_at, hr_escalated_at, hr_escalation_reason
                FROM recruiter_applications
                WHERE LOWER(COALESCE(application_status, '')) IN ({placeholders})
                ORDER BY COALESCE(human_handled_at, hr_escalated_at) DESC NULLS LAST
                """,
                holds,
            )
            if not rows:
                print(f"No applications are held for a human ({', '.join(holds)}).")
            for row in rows:
                who = row.get("candidate_email") or row.get("source_email") or "-"
                when = row.get("human_handled_at") or row.get("hr_escalated_at") or ""
                print(f"  {row['id']:>6}  {who:<38} {row['application_status']:<20} {str(when)[:19]}")
                if row.get("hr_escalation_reason"):
                    print(f"          {str(row['hr_escalation_reason'])[:110]}")
        finally:
            db.close()
        return

    if args.process_actions:
        count = process_action_queue()
        print(f"Processed {count} queued action(s).")
        return

    if args.watch_actions:
        print("Watching agent_action_queue. Press Ctrl+C to stop.")
        while True:
            try:
                process_action_queue()
            except Exception as exc:
                LOGGER.exception("Action queue pass failed: %s", exc)
            time.sleep(ACTION_QUEUE_POLL_SECONDS)

    if args.reopen_interview:
        db = RecruiterDatabase()
        try:
            row = db.one(
                "SELECT interview_attempts, interview_completed_at, interview_link_token "
                "FROM recruiter_applications WHERE id = %s",
                (args.reopen_interview,),
            )
            if not row:
                print(f"Application {args.reopen_interview} not found.")
                return
            db.execute(
                "UPDATE recruiter_applications SET interview_attempts = 0 WHERE id = %s",
                (args.reopen_interview,),
            )
            print(
                f"Interview reopened for application {args.reopen_interview} "
                f"(attempts were {row.get('interview_attempts')})."
            )
            if row.get("interview_completed_at"):
                print("  Note: this interview is already marked completed; "
                      "use --create-interview-link --reset-interview-link to run a fresh one.")
            if row.get("interview_link_token"):
                print(f"  Their existing link still works: {candidate_interview_url(row['interview_link_token'])}")
        finally:
            db.close()
        return

    if args.show_candidate:
        email_address = clean_email(args.show_candidate) or args.show_candidate
        db = RecruiterDatabase()
        try:
            rows = db.rows(
                """
                SELECT ra.*, rc.full_name, rc.current_title, rc.total_experience_years,
                       rr.position_title AS requirement_position
                FROM recruiter_applications ra
                JOIN recruiter_candidates rc ON rc.id = ra.candidate_id
                LEFT JOIN recruitment_requirements rr ON rr.id = ra.requirement_id
                WHERE LOWER(COALESCE(ra.candidate_email, '')) = %s
                   OR LOWER(COALESCE(ra.source_email, '')) = %s
                ORDER BY ra.id DESC
                """,
                (email_address, email_address),
            )
            if not rows:
                print(f"No application found for {email_address}.")
                return
            for row in rows:
                print(f"\napplication {row['id']}   {row.get('full_name') or '-'}")
                print(f"  status            : {row.get('application_status')}")
                print(f"  requirement       : {row.get('requirement_position') or '(none)'}")
                print(f"  ats / jd score    : {row.get('ats_score')} / {row.get('jd_match_score')}")
                print(f"  their title       : {row.get('current_title') or '-'}"
                      f"   experience: {row.get('total_experience_years')}")
                print(f"  screening         : {json.dumps(json_dict(row.get('screening_details')), default=str)[:200]}")
                if row.get("interview_attempts"):
                    print(f"  interview attempts: {row['interview_attempts']}")
                for label, key in [
                    ("hr escalated", "hr_escalated_at"), ("hr approved", "hr_approved_at"),
                    ("human handled", "human_handled_at"), ("interview started", "interview_started_at"),
                    ("interview done", "interview_completed_at"), ("interview scheduled", "interview_scheduled_at"),
                ]:
                    if row.get(key):
                        print(f"  {label:<18}: {row[key]}")
                if row.get("hr_escalation_reason"):
                    print(f"  reason            : {str(row['hr_escalation_reason'])[:160]}")
                if row.get("interview_availability"):
                    print(f"  availability text : {str(row['interview_availability'])[:160]}")
                report = json_dict(row.get("interview_report"))
                if report:
                    print(f"  interview report  : score={report.get('overall_score')} "
                          f"rec={report.get('recommendation')} review={report.get('needs_human_review')}")
                    if report.get("human_review_reasons"):
                        print(f"    review reasons  : {report['human_review_reasons']}")
                sent = db.rows(
                    "SELECT scenario, sent_at FROM recruiter_sent_replies WHERE application_id = %s "
                    "ORDER BY sent_at DESC LIMIT 12",
                    (row["id"],),
                )
                if sent:
                    print("  emails sent       :")
                    for entry in sent:
                        print(f"    {str(entry['sent_at'])[:19]}  {entry['scenario']}")
        finally:
            db.close()
        return

    if args.resume_application:
        db = RecruiterDatabase()
        try:
            db.execute(
                """
                UPDATE recruiter_applications
                SET application_status = %s,
                    human_handled_at = NULL
                WHERE id = %s
                """,
                (args.resume_status, args.resume_application),
            )
            # Clearing the reply ledger is the point of handing it back: without
            # it the 24h duplicate guard suppresses exactly the message the
            # reset was meant to produce, and the candidate hears nothing.
            cleared = db.rows(
                "DELETE FROM recruiter_sent_replies WHERE application_id = %s RETURNING scenario",
                (args.resume_application,),
            )
            db.conn.commit()
            print(
                f"Application {args.resume_application} handed back to the agent "
                f"with status '{args.resume_status}'."
            )
            if cleared:
                print(f"  cleared {len(cleared)} sent-reply record(s) so the agent may resend: "
                      f"{', '.join(sorted({row['scenario'] for row in cleared}))}")
        finally:
            db.close()
        return

    if args.forget_candidate:
        email_address = clean_email(args.forget_candidate) or args.forget_candidate
        db = RecruiterDatabase()
        try:
            apps = db.rows(
                """
                SELECT id, application_status FROM recruiter_applications
                WHERE LOWER(COALESCE(candidate_email, '')) = %s
                   OR LOWER(COALESCE(source_email, '')) = %s
                """,
                (email_address, email_address),
            )
            for row in apps:
                db.execute("DELETE FROM recruiter_sent_replies WHERE application_id = %s", (row["id"],))
            db.execute("DELETE FROM recruiter_sent_replies WHERE recipient = %s", (email_address,))
            db.execute(
                """
                DELETE FROM recruiter_applications
                WHERE LOWER(COALESCE(candidate_email, '')) = %s OR LOWER(COALESCE(source_email, '')) = %s
                """,
                (email_address, email_address),
            )
            db.execute(
                """
                DELETE FROM recruiter_candidates
                WHERE LOWER(COALESCE(candidate_email, '')) = %s OR LOWER(COALESCE(source_email, '')) = %s
                """,
                (email_address, email_address),
            )
            db.execute("DELETE FROM recruiter_email_events WHERE LOWER(COALESCE(source_email, '')) = %s", (email_address,))
            # Any claim on their mail must go too, or the email is skipped.
            db.execute(
                """
                DELETE FROM recruiter_processed_messages
                WHERE provider_message_id IN (
                    SELECT provider_message_id FROM recruiter_processed_messages
                )
                AND provider_message_id NOT IN (
                    SELECT COALESCE(provider_message_id, '') FROM recruiter_sent_replies
                )
                """
            )
            print(
                f"Forgot {email_address}: removed {len(apps)} application(s) "
                f"{[row['id'] for row in apps]}, their candidate rows, events, replies and message claims."
            )
            print("Mark the email unread in the mailbox, then run --run-once.")
        finally:
            db.close()
        return

    if args.release_failed_messages:
        db = RecruiterDatabase()
        try:
            rows = db.rows(
                """
                SELECT provider_message_id FROM recruiter_processed_messages pm
                WHERE NOT EXISTS (
                    SELECT 1 FROM recruiter_sent_replies sr
                    WHERE sr.provider_message_id = pm.provider_message_id
                )
                ORDER BY processed_at DESC
                """
            )
            released = 0
            for row in rows:
                db.release_provider_message(row["provider_message_id"])
                released += 1
            print(f"Released {released} claimed message(s) with no recorded reply.")
        finally:
            db.close()
        return

    if args.init_db:
        db = RecruiterDatabase()
        try:
            db.init_schema()
            print("Recruiter tables are ready.")
            return
        finally:
            db.close()

    if args.register_gmail_watch:
        register_gmail_watch()
        return

    if args.register_graph_subscription:
        register_graph_subscription()
        return

    if args.reset_graph_subscription:
        reset_graph_subscription()
        return

    if args.register_graph_ngrok:
        register_graph_subscription_from_ngrok()
        return

    if args.reset_graph_ngrok:
        reset_graph_subscription_from_ngrok()
        return

    if args.list_graph_subscriptions:
        list_graph_subscriptions()
        return

    if args.delete_graph_subscription:
        delete_graph_subscription(args.delete_graph_subscription)
        return

    if args.create_interview_link:
        create_interview_link_for_application(args.create_interview_link, reset=args.reset_interview_link)
        return

    if args.send_interview_link:
        send_interview_link_for_application(args.send_interview_link)
        return

    if args.send_interview_reminders:
        sent = send_pending_interview_reminders(
            hours_after_link=args.interview_reminder_hours,
            reminder_gap_hours=args.interview_reminder_gap_hours,
            max_reminders=args.interview_reminder_max,
            limit=args.interview_reminder_limit,
        )
        print(f"Sent {sent} interview reminder email(s).")
        return

    if args.send_teams_link:
        send_teams_link_for_application(args.send_teams_link)
        return

    if args.teams_interview_info:
        show_teams_interview_info(args.teams_interview_info)
        return

    if args.mark_due_hr_rounds:
        marked = mark_due_final_hr_rounds_pending()
        print(f"Marked {marked} final HR round(s) pending decision.")
        return

    if args.serve_graph_webhook:
        GraphWebhookServer().serve_forever()
        return

    if args.serve_gmail_webhook:
        GmailPushWebhookServer().serve_forever()
        return

    agent = AIRecruiterAgent()
    try:
        if args.run_once:
            agent.run_once()
            print("Recruiter inbox pass complete.")
            return

        if args.watch:
            agent.run_forever(args.poll_seconds)
            return

        parser.print_help()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
