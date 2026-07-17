import argparse
import base64
import email
import hashlib
import imaplib
import json
import mimetypes
import re
import smtplib
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from html import escape as html_escape
from html import unescape
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4
from xml.etree import ElementTree

from langsmith import traceable

from config import (
    DATABASE_URL,
    CV_UPLOAD_API_URL_TEMPLATE,
    CV_UPLOAD_TIMEOUT_SECONDS,
    DB_PROVIDER,
    GMAIL_CREDENTIALS_FILE,
    GMAIL_PUBSUB_TOPIC,
    GMAIL_TOKEN_FILE,
    GRAPH_CLIENT_STATE,
    GRAPH_NOTIFICATION_URL,
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
    MSSQL_CONNECTION_STRING,
    MSSQL_ODBC_DRIVER,
    NGROK_API_URL,
    OLLAMA_NUM_PREDICT,
    RECRUITER_EMAIL,
    RECRUITER_EMAIL_PASSWORD,
    RECRUITER_FROM_EMAIL,
    RECRUITER_IMAP_HOST,
    RECRUITER_IMAP_PORT,
    RECRUITER_MAILBOX,
    RECRUITER_APPEND_SIGNATURE,
    RECRUITER_POLL_LIMIT,
    RECRUITER_POLL_SECONDS,
    RECRUITER_REPLY_ENABLED,
    RECRUITER_SIGNATURE_COMPANY,
    RECRUITER_SIGNATURE_COMPANY_URL,
    RECRUITER_SIGNATURE_EMAIL,
    RECRUITER_SIGNATURE_LOGO_URL,
    RECRUITER_SIGNATURE_NAME,
    RECRUITER_SIGNATURE_SIGNOFF,
    RECRUITER_SMTP_HOST,
    RECRUITER_SMTP_PORT,
)
from llm_factory import make_chat_model


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


CV_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}


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
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS candidate_email TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS referrer_email TEXT;
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS submission_type TEXT NOT NULL DEFAULT 'self_application';
ALTER TABLE recruiter_applications ADD COLUMN IF NOT EXISTS attachment_payload BYTEA;

CREATE TABLE IF NOT EXISTS recruiter_email_events (
    id BIGSERIAL PRIMARY KEY,
    email_message_id TEXT,
    source_email TEXT,
    email_subject TEXT,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


MSSQL_CREATE_TABLES_SQL = """
IF OBJECT_ID('recruitment_requirements', 'U') IS NULL
CREATE TABLE recruitment_requirements (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    position_title NVARCHAR(255) NOT NULL,
    experience_min_years DECIMAL(5, 2) NULL,
    experience_max_years DECIMAL(5, 2) NULL,
    budget_min DECIMAL(12, 2) NULL,
    budget_max DECIMAL(12, 2) NULL,
    currency NVARCHAR(20) DEFAULT 'INR',
    job_description NVARCHAR(MAX) NOT NULL,
    urgently_required BIT DEFAULT 0,
    needed_within_days INT NULL,
    status NVARCHAR(80) NOT NULL DEFAULT 'open',
    created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    updated_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF OBJECT_ID('recruiter_candidates', 'U') IS NULL
CREATE TABLE recruiter_candidates (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    candidate_uid UNIQUEIDENTIFIER NOT NULL UNIQUE,
    source_email NVARCHAR(500) NULL,
    candidate_email NVARCHAR(500) NULL,
    referrer_email NVARCHAR(500) NULL,
    submission_type NVARCHAR(120) NOT NULL DEFAULT 'self_application',
    full_name NVARCHAR(500) NULL,
    phone NVARCHAR(120) NULL,
    location NVARCHAR(500) NULL,
    linkedin_url NVARCHAR(1000) NULL,
    portfolio_url NVARCHAR(1000) NULL,
    current_title NVARCHAR(500) NULL,
    current_company NVARCHAR(500) NULL,
    total_experience_years DECIMAL(5, 2) NULL,
    skills NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    education NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    work_history NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    certifications NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    raw_cv_text NVARCHAR(MAX) NULL,
    cv_summary NVARCHAR(MAX) NULL,
    ats_score DECIMAL(5, 2) NULL,
    ai_evaluation NVARCHAR(MAX) NOT NULL DEFAULT '{}',
    created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    updated_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF OBJECT_ID('recruiter_applications', 'U') IS NULL
CREATE TABLE recruiter_applications (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    application_uid UNIQUEIDENTIFIER NOT NULL UNIQUE,
    candidate_id BIGINT NOT NULL REFERENCES recruiter_candidates(id),
    requirement_id BIGINT NULL REFERENCES recruitment_requirements(id),
    email_message_id NVARCHAR(1000) NULL,
    source_email NVARCHAR(500) NULL,
    candidate_email NVARCHAR(500) NULL,
    referrer_email NVARCHAR(500) NULL,
    submission_type NVARCHAR(120) NOT NULL DEFAULT 'self_application',
    email_subject NVARCHAR(1000) NULL,
    detected_position NVARCHAR(500) NULL,
    matched_position NVARCHAR(500) NULL,
    application_status NVARCHAR(120) NOT NULL,
    ats_score DECIMAL(5, 2) NULL,
    jd_match_score DECIMAL(5, 2) NULL,
    strengths NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    risks NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    missing_requirements NVARCHAR(MAX) NOT NULL DEFAULT '[]',
    ai_short_description NVARCHAR(MAX) NULL,
    ai_evaluation NVARCHAR(MAX) NOT NULL DEFAULT '{}',
    attachment_filename NVARCHAR(1000) NULL,
    attachment_sha256 NVARCHAR(128) NULL,
    attachment_payload VARBINARY(MAX) NULL,
    received_at DATETIMEOFFSET NULL,
    created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF OBJECT_ID('recruiter_email_events', 'U') IS NULL
CREATE TABLE recruiter_email_events (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    email_message_id NVARCHAR(1000) NULL,
    source_email NVARCHAR(500) NULL,
    email_subject NVARCHAR(1000) NULL,
    event_type NVARCHAR(200) NOT NULL,
    details NVARCHAR(MAX) NOT NULL DEFAULT '{}',
    created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF COL_LENGTH('recruitment_requirements', 'position_title') IS NOT NULL
ALTER TABLE recruitment_requirements ALTER COLUMN position_title NVARCHAR(255) NOT NULL;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'recruitment_requirements_position_title_idx')
CREATE UNIQUE INDEX recruitment_requirements_position_title_idx
ON recruitment_requirements (position_title);

IF COL_LENGTH('recruiter_applications', 'attachment_payload') IS NULL
ALTER TABLE recruiter_applications ADD attachment_payload VARBINARY(MAX) NULL;
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
        if module_name == "pyodbc" and "libodbc.so" in str(exc):
            raise RuntimeError(
                "SQL Server mode needs the system ODBC runtime. Install Microsoft ODBC Driver 18 "
                "and unixODBC, then restart the app. On Ubuntu/Debian run: "
                "sudo ./scripts/install_mssql_odbc_ubuntu.sh"
            ) from exc
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


def build_thread_context(inbox_email: InboxEmail) -> str:
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
    lines = []
    for index, message in enumerate(messages, start=1):
        date_text = message.received_at.isoformat() if message.received_at else "unknown date"
        lines.append(
            f"Message {index}\n"
            f"From: {message.sender}\n"
            f"Date: {date_text}\n"
            f"Subject: {message.subject}\n"
            f"Body:\n{message.body[:1800]}"
        )
    return "\n\n---\n\n".join(lines)


def extract_attachments(message: email.message.EmailMessage) -> list[tuple[str, bytes]]:
    attachments = []
    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue

        filename = decode_mime(part.get_filename()) or f"attachment-{len(attachments) + 1}"
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append((filename, payload))

    return attachments


def is_cv_filename(filename: str) -> bool:
    return Path(filename.lower()).suffix in CV_EXTENSIONS


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


def email_body_to_html(body: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\r?\n\r?\n", body) if part.strip()]
    html_parts = []
    for paragraph in paragraphs:
        normalized = paragraph.replace("\r\n", "\n").replace("\r", "\n")
        html_parts.append(f"<p>{html_escape(normalized).replace(chr(10), '<br>')}</p>")
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
    parts = [f"<p>{signoff}</p>", f"<p>{name}"]
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
    parts.append("</p>")
    if RECRUITER_SIGNATURE_LOGO_URL:
        logo_url = html_escape(RECRUITER_SIGNATURE_LOGO_URL.strip(), quote=True)
        alt = html_escape(RECRUITER_SIGNATURE_COMPANY.strip() or "Company logo", quote=True)
        parts.append(f'<p><img src="{logo_url}" alt="{alt}" width="180" style="width:180px;max-width:180px;height:auto;border:0;display:block;"></p>')
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
    text = normalize_position_text(latest_body)
    patterns = [
        r"\bstatus\b",
        r"\bupdate\b",
        r"\bfollow up\b",
        r"\bany update\b",
        r"\bshortlisted\b",
        r"\bnext step\b",
        r"\bnext steps\b",
        r"\binterview\b",
        r"\bwhen can i expect\b",
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
    return normalized


def normalize_position_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\bdev\b", "developer", value)
    value = re.sub(r"\bacct\b", "accountant", value)
    value = re.sub(r"\baccounts?\b", "accountant", value)
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


def is_design_role_text(value: str | None) -> bool:
    tokens = position_tokens(value)
    phrases = normalize_position_text(value)
    design_tokens = {
        "ui",
        "ux",
        "designer",
        "design",
        "figma",
        "wireframe",
        "wireframes",
        "prototype",
        "prototyping",
        "usability",
        "user",
        "interface",
        "experience",
        "product",
    }
    if tokens & design_tokens:
        return True
    return any(
        phrase in phrases
        for phrase in [
            "user interface",
            "user experience",
            "product designer",
            "visual designer",
            "interaction designer",
        ]
    )


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
    candidate_fields = candidate_positions + [
        " ".join(str(skill) for skill in ensure_list(extracted.get("skills"))),
        source_text,
    ]

    candidate_text = normalize_position_text(" ".join(value for value in candidate_fields if value))
    candidate_words = position_tokens(candidate_text)
    if not candidate_words:
        return {"requirement_id": None, "confidence": 0, "reason": "No candidate role detected"}

    best_requirement = None
    best_score = 0.0

    for requirement in requirements:
        title = requirement.get("position_title")
        title_text = normalize_position_text(title)
        title_words = position_tokens(title)
        if not title_words:
            continue

        if title_text and title_text in candidate_text:
            score = 1.0
        elif candidate_text and candidate_text in title_text:
            score = 0.95
        else:
            overlap = candidate_words & title_words
            score = len(overlap) / len(title_words)

        if score > best_score:
            best_score = score
            best_requirement = requirement

    if best_requirement and best_score >= 0.75:
        return {
            "requirement_id": best_requirement["id"],
            "confidence": round(best_score, 2),
            "reason": "Matched by normalized position title",
        }

    return {
        "requirement_id": None,
        "confidence": round(best_score, 2),
        "reason": "No deterministic position-title match",
    }


def roles_are_compatible(requested_role: str | None, cv_role: str | None, cv_text: str = "") -> bool:
    requested_text = normalize_position_text(requested_role)
    if not requested_text:
        return True

    cv_role_text = normalize_position_text(cv_role)
    cv_search_text = normalize_position_text(f"{cv_role or ''} {cv_text[:2500]}")
    requested_words = position_tokens(requested_text)
    if not requested_words:
        return True

    if requested_text in cv_search_text:
        return True
    if cv_role_text and cv_role_text in requested_text:
        return True

    if "accountant" in requested_words:
        accounting_keywords = {
            "accountant",
            "tally",
            "gst",
            "tds",
            "invoice",
            "invoices",
            "bookkeeping",
            "reconciliation",
            "ledger",
            "payable",
            "receivable",
        }
        if accounting_keywords & position_tokens(cv_search_text):
            return True

    if is_design_role_text(requested_text) and is_design_role_text(cv_search_text):
        return True

    overlap = requested_words & position_tokens(cv_search_text)
    return len(overlap) / len(requested_words) >= 0.66


def extract_docx_text(path: Path) -> str:
    paragraphs = []
    with zipfile.ZipFile(path) as docx:
        xml = docx.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for node in root.findall(".//w:t", namespace):
        if node.text:
            paragraphs.append(node.text)
    return " ".join(paragraphs)


def extract_pdf_text(path: Path) -> str:
    try:
        pypdf = require_package("pypdf", "./venv/bin/python -m pip install pypdf")
    except RuntimeError:
        pypdf = require_package("PyPDF2", "./venv/bin/python -m pip install pypdf")

    reader = pypdf.PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def extract_cv_text(filename: str, payload: bytes) -> str:
    suffix = Path(filename.lower()).suffix
    with TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(payload)

        if suffix == ".txt":
            return payload.decode("utf-8", errors="ignore")
        if suffix == ".docx":
            return extract_docx_text(path)
        if suffix == ".pdf":
            return extract_pdf_text(path)

    return ""


def upload_cv_attachment(application_id: int, filename: str, payload: bytes) -> str:
    if not CV_UPLOAD_API_URL_TEMPLATE:
        return filename

    requests = require_package("requests", "./venv/bin/python -m pip install requests")
    url = CV_UPLOAD_API_URL_TEMPLATE.format(application_id=application_id)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = requests.post(
        url,
        files={"file": (filename, payload, content_type)},
        headers={"accept": "*/*"},
        timeout=CV_UPLOAD_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"CV upload API failed: {response.status_code} {response.text}")

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"CV upload API returned non-JSON response: {response.text[:300]}") from exc

    uploaded_filename = data.get("attachmentFilename") or data.get("attachment_filename") or data.get("filename")
    if not uploaded_filename:
        raise RuntimeError(f"CV upload API response did not include attachmentFilename: {data}")
    return uploaded_filename


class RecruiterDatabase:
    def __init__(self):
        self.provider = DB_PROVIDER
        if self.provider in {"mssql", "sqlserver", "sql_server"}:
            if not MSSQL_CONNECTION_STRING:
                raise RuntimeError("MSSQL_CONNECTION_STRING is required when DB_PROVIDER=mssql.")
            pyodbc = require_package("pyodbc", "./venv/bin/python -m pip install pyodbc")
            self.pyodbc = pyodbc
            self.psycopg = None
            try:
                self.conn = pyodbc.connect(self.mssql_connection_string(MSSQL_CONNECTION_STRING))
            except Exception as exc:
                error_text = str(exc)
                if "Can't open lib" in error_text or "Data source name not found" in error_text:
                    raise RuntimeError(
                        "SQL Server mode could not find a registered SQL Server ODBC driver. "
                        f"Installed ODBC drivers: {self.pyodbc.drivers() or 'none'}. "
                        "Install Microsoft ODBC Driver 18 with: sudo ./scripts/install_mssql_odbc_ubuntu.sh"
                    ) from exc
                if "Invalid value specified for connection string attribute" in error_text:
                    raise RuntimeError(
                        "SQL Server rejected one of the connection string attributes. "
                        "For ODBC, use values like Encrypt=yes and TrustServerCertificate=yes. "
                        "The app normalizes common .NET values automatically; check MSSQL_CONNECTION_STRING "
                        f"if this still appears. Original error: {exc}"
                    ) from exc
                raise
            self.provider = "mssql"
        else:
            if not DATABASE_URL:
                raise RuntimeError("DATABASE_URL is required when DB_PROVIDER=postgres.")
            psycopg = require_package("psycopg", "./venv/bin/python -m pip install psycopg[binary]")
            self.pyodbc = None
            self.psycopg = psycopg
            self.conn = psycopg.connect(DATABASE_URL)
            self.provider = "postgres"

    def mssql_connection_string(self, value: str) -> str:
        connection_string = self.normalize_mssql_connection_string(value)
        if "driver=" not in connection_string.lower():
            driver = MSSQL_ODBC_DRIVER.strip() or self.detect_mssql_driver()
            connection_string = f"DRIVER={{{driver}}};{connection_string}"
        return connection_string

    def normalize_mssql_connection_string(self, value: str) -> str:
        normalized_parts = []
        for raw_part in value.strip().split(";"):
            part = raw_part.strip()
            if not part:
                continue
            if "=" not in part:
                normalized_parts.append(part)
                continue
            key, raw_value = part.split("=", 1)
            key = key.strip()
            raw_value = raw_value.strip()
            key_lower = key.lower().replace(" ", "")
            value_lower = raw_value.lower()

            if key_lower in {"server", "datasource", "address", "addr", "networkaddress"}:
                key = "SERVER"
            elif key_lower in {"database", "initialcatalog"}:
                key = "DATABASE"
            elif key_lower in {"userid", "user", "uid"}:
                key = "UID"
            elif key_lower in {"password", "pwd"}:
                key = "PWD"
            if key_lower in {"encrypt", "trustservercertificate"} and value_lower in {"true", "false"}:
                raw_value = "yes" if value_lower == "true" else "no"
            if key_lower == "multipleactiveresultsets":
                key = "MARS_Connection"
                if value_lower in {"true", "false"}:
                    raw_value = "yes" if value_lower == "true" else "no"

            normalized_parts.append(f"{key}={raw_value}")
        return ";".join(normalized_parts) + ";"

    def detect_mssql_driver(self) -> str:
        drivers = self.pyodbc.drivers() if self.pyodbc else []
        preferred = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        ]
        for driver in preferred:
            if driver in drivers:
                return driver
        sql_drivers = [driver for driver in drivers if "sql server" in driver.lower()]
        if sql_drivers:
            return sql_drivers[-1]
        raise RuntimeError(
            "No SQL Server ODBC driver is registered on this machine. "
            "Run: sudo ./scripts/install_mssql_odbc_ubuntu.sh. "
            "Then verify with: python -c \"import pyodbc; print(pyodbc.drivers())\""
        )

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def is_mssql(self) -> bool:
        return self.provider == "mssql"

    def sql(self, query: str) -> str:
        if not self.is_mssql():
            return query
        query = query.replace("%s::jsonb", "?")
        query = query.replace("%s", "?")
        query = query.replace("::jsonb", "")
        query = query.replace("NOW()", "SYSDATETIMEOFFSET()")
        query = query.replace("LOWER(status) = 'open' DESC", "CASE WHEN LOWER(status) = 'open' THEN 1 ELSE 0 END DESC")
        query = query.replace("LOWER(TRIM(status))", "LOWER(LTRIM(RTRIM(status)))")
        query = query.replace(
            "needed_within_days NULLS LAST",
            "CASE WHEN needed_within_days IS NULL THEN 1 ELSE 0 END, needed_within_days",
        )
        return self.apply_mssql_limit(query)

    def apply_mssql_limit(self, query: str) -> str:
        match = re.search(r"\s+LIMIT\s+(\d+)\s*$", query, flags=re.I)
        if not match:
            return query
        limit = match.group(1)
        without_limit = query[: match.start()]
        if re.search(r"\bORDER\s+BY\b", without_limit, flags=re.I):
            return f"{without_limit} OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"
        return re.sub(r"^\s*SELECT\b", f"SELECT TOP {limit}", without_limit, count=1, flags=re.I)

    def rows(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        if self.is_mssql():
            cursor = self.conn.cursor()
            try:
                cursor.execute(self.sql(query), params)
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()
        with self.conn.cursor(row_factory=self.psycopg.rows.dict_row) as cursor:
            cursor.execute(query, params)
            return list(cursor.fetchall())

    def one(self, query: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.rows(query, params)
        return rows[0] if rows else None

    def execute(self, query: str, params: tuple = ()):
        if self.is_mssql():
            cursor = self.conn.cursor()
            try:
                cursor.execute(self.sql(query), params)
                self.conn.commit()
            finally:
                cursor.close()
            return
        with self.conn.cursor() as cursor:
            cursor.execute(self.sql(query), params)
        self.conn.commit()

    def init_schema(self):
        if self.is_mssql():
            cursor = self.conn.cursor()
            try:
                for statement in MSSQL_CREATE_TABLES_SQL.split(";"):
                    statement = statement.strip()
                    if statement:
                        cursor.execute(statement)
                self.conn.commit()
            finally:
                cursor.close()
            return
        with self.conn.cursor() as cursor:
            for statement in CREATE_TABLES_SQL.split(";"):
                statement = statement.strip()
                if statement:
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
                json.dumps(details),
            ),
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
            extracted.get("total_experience_years"),
            json.dumps(extracted.get("skills", [])),
            json.dumps(extracted.get("education", [])),
            json.dumps(extracted.get("work_history", [])),
            json.dumps(extracted.get("certifications", [])),
            cv_text,
            evaluation.get("short_description"),
            evaluation.get("ats_score"),
            json.dumps(evaluation),
        )
        if self.is_mssql():
            cursor = self.conn.cursor()
            try:
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
                    OUTPUT INSERTED.id
                    VALUES
                    (
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
                candidate_id = cursor.fetchone()[0]
                self.conn.commit()
                return candidate_id
            finally:
                cursor.close()

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
            inbox_email.sender,
            candidate_email,
            referrer_email,
            submission_type,
            inbox_email.subject,
            extracted.get("target_position"),
            requirement["position_title"] if requirement else None,
            status,
            evaluation.get("ats_score"),
            evaluation.get("jd_match_score"),
            json.dumps(evaluation.get("strengths", [])),
            json.dumps(evaluation.get("risks", [])),
            json.dumps(evaluation.get("missing_requirements", [])),
            evaluation.get("short_description"),
            json.dumps(evaluation),
            attachment_filename,
            attachment_sha256,
            attachment_payload,
            inbox_email.received_at,
        )
        if self.is_mssql():
            existing = self.one(
                """
                SELECT id
                FROM recruiter_applications
                WHERE email_message_id = %s AND attachment_sha256 = %s
                LIMIT 1
                """,
                (inbox_email.message_id, attachment_sha256),
            )
            if existing:
                return existing["id"]
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO recruiter_applications
                    (
                        application_uid, candidate_id, requirement_id, email_message_id,
                        source_email, candidate_email, referrer_email, submission_type,
                        email_subject, detected_position, matched_position,
                        application_status, ats_score, jd_match_score, strengths, risks,
                        missing_requirements, ai_short_description, ai_evaluation,
                        attachment_filename, attachment_sha256, attachment_payload, received_at
                    )
                    OUTPUT INSERTED.id
                    VALUES
                    (
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    values,
                )
                application_id = cursor.fetchone()[0]
                self.conn.commit()
                return application_id
            finally:
                cursor.close()

        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO recruiter_applications
                (
                    application_uid, candidate_id, requirement_id, email_message_id,
                    source_email, candidate_email, referrer_email, submission_type,
                    email_subject, detected_position, matched_position,
                    application_status, ats_score, jd_match_score, strengths, risks,
                    missing_requirements, ai_short_description, ai_evaluation,
                    attachment_filename, attachment_sha256, attachment_payload, received_at
                )
                VALUES
                (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
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
        limit: int = 10,
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
        limit: int = 10,
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

    def __init__(self):
        missing = [
            name
            for name, value in {
                "MICROSOFT_TENANT_ID": MICROSOFT_TENANT_ID,
                "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
                "MICROSOFT_CLIENT_SECRET": MICROSOFT_CLIENT_SECRET,
                "MICROSOFT_MAILBOX": MICROSOFT_MAILBOX,
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
        self.mailbox = quote(MICROSOFT_MAILBOX, safe="")

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
        headers["Authorization"] = f"Bearer {self.token()}"
        headers.setdefault("Accept", "application/json")
        response = self.requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Microsoft Graph {method} {path} failed: {response.status_code} {response.text}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

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

    def fetch_message_by_id(self, message_id: str) -> InboxEmail | None:
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
            return None
        return self.message_to_inbox_email(message)

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

    def fetch_thread_messages(self, conversation_id: str, limit: int = 10) -> list[ThreadMessage]:
        if not conversation_id:
            return []
        safe_conversation_id = conversation_id.replace("'", "''")
        params = {
            "$filter": f"conversationId eq '{safe_conversation_id}'",
            "$top": str(limit),
            "$select": "id,internetMessageId,subject,body,bodyPreview,from,receivedDateTime",
        }
        data = self.request(
            "GET",
            f"/users/{self.mailbox}/messages",
            params=params,
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        messages = [parse_graph_thread_message(message) for message in data.get("value", [])]
        return sorted(messages, key=lambda item: item.received_at.isoformat() if item.received_at else "")

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
            if content:
                attachments.append((name, base64.b64decode(content)))
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

    def send_reply(self, inbox_email: InboxEmail, subject: str, body: str, to_email: str | None = None):
        recipient = to_email or inbox_email.sender
        if not RECRUITER_REPLY_ENABLED:
            print(f"[reply disabled] To: {recipient} | Subject: {subject}\n{body}")
            return

        body = body.replace("\n", "\r\n").replace("\r\r\n", "\r\n")
        message_id = inbox_email.uid.decode()
        if not to_email or clean_email(to_email) == clean_email(inbox_email.sender):
            draft = self.request(
                "POST",
                f"/users/{self.mailbox}/messages/{quote(message_id, safe='')}/createReply",
                json={},
            )
            draft_id = draft.get("id")
            if not draft_id:
                raise RuntimeError(f"Microsoft Graph createReply did not return a draft id: {draft}")
            draft_body = (draft.get("body") or {}).get("content") or ""
            reply_html = append_signature_if_needed(email_body_to_html(body), draft_body)
            content = f"{reply_html}<br>{draft_body}" if draft_body else reply_html
            self.request(
                "PATCH",
                f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}",
                json={"body": {"contentType": "HTML", "content": content}},
            )
            self.request(
                "POST",
                f"/users/{self.mailbox}/messages/{quote(draft_id, safe='')}/send",
                json={},
            )
            return

        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
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
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if inbox_email.message_id:
            message["In-Reply-To"] = inbox_email.message_id
            references = " ".join(
                value
                for value in [inbox_email.references, inbox_email.in_reply_to, inbox_email.message_id]
                if value
            )
            message["References"] = references
        if RECRUITER_APPEND_SIGNATURE:
            body = f"{body}\r\n\r\n{recruiter_plain_signature()}"
        message.set_content(body)

        with smtplib.SMTP(RECRUITER_SMTP_HOST, RECRUITER_SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(RECRUITER_EMAIL, RECRUITER_EMAIL_PASSWORD)
            smtp.send_message(message)


class RecruiterAI:
    def __init__(self):
        self.llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1400))
        self.repair_llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1400))

    def json_call(self, prompt: str) -> dict[str, Any]:
        response = self.llm.invoke(
            f"""
Return one valid JSON object only.
Do not include markdown, comments, trailing commas, or explanatory text.

{prompt}
"""
        ).content
        json_text = self.extract_json_object(response)
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as exc:
            repaired = self.repair_json(json_text, exc)
            return json.loads(repaired)

    def extract_json_object(self, response: str) -> str:
        match = re.search(r"\{.*\}", response, flags=re.S)
        if not match:
            raise ValueError(f"LLM did not return JSON: {response}")
        return match.group(0)

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

    @traceable(name="match_requirement")
    def match_requirement(self, extracted: dict[str, Any], requirements: list[dict[str, Any]]) -> dict[str, Any]:
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
                "job_description": row["job_description"][:1000],
            }
            for row in requirements
        ]
        return self.json_call(
            f"""
Return only valid JSON.
Choose the best open requirement for this candidate, or null if none fits the target role.

JSON schema:
{{
  "requirement_id": null,
  "confidence": 0,
  "reason": "short reason"
}}

Candidate:
{json.dumps(extracted)}

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

    def reply_missing_cv(self, inbox_email: InboxEmail):
        self.mailer.send_reply(
            inbox_email,
            f"CV required: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for reaching out.",
                "Please send your updated CV as a PDF, DOCX, or TXT attachment. Once we have it, we can review your profile for the role discussed in this thread.",
            ),
        )

    def reply_wrong_cv(self, inbox_email: InboxEmail, requested_position: str | None, cv_position: str | None):
        requested_text = f" for {requested_position}" if requested_position else ""
        cv_text = f" The CV looks closer to a {cv_position} profile." if cv_position else ""
        self.mailer.send_reply(
            inbox_email,
            f"Correct CV required: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for sharing the CV.",
                f"I checked it against this email thread, and we were expecting a CV{requested_text}. This attachment does not seem to match that role.{cv_text}",
                "Could you please verify and send the correct CV? We will review it as soon as we receive the right one.",
            ),
        )

    def reply_no_opening(self, inbox_email: InboxEmail, position: str | None):
        role_text = f" for {position}" if position else ""
        self.mailer.send_reply(
            inbox_email,
            f"Application update: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for sharing your profile.",
                f"At the moment, we do not have an active opening{role_text}. I have saved your details, and we will reach out if a suitable role opens up.",
            ),
        )

    def reply_received(self, inbox_email: InboxEmail):
        self.mailer.send_reply(
            inbox_email,
            f"Application received: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for applying and sharing your CV.",
                "We have received your application. Our team will review it and contact you if your profile matches the role.",
            ),
        )

    def reply_referral_received(self, inbox_email: InboxEmail, candidate_email: str, position: str | None):
        role_text = f" for {position}" if position else ""
        self.mailer.send_reply(
            inbox_email,
            f"Application received: {inbox_email.subject}",
            recruiter_email_body(
                f"Your CV has been shared with us{role_text}.",
                "We have received it and will review your profile. If it matches the role, our team will contact you with the next steps.",
            ),
            to_email=candidate_email,
        )

    def reply_referral_missing_candidate_email(self, inbox_email: InboxEmail, position: str | None):
        role_text = f" for {position}" if position else ""
        self.mailer.send_reply(
            inbox_email,
            f"Candidate email required: {inbox_email.subject}",
            recruiter_email_body(
                f"Thanks for sharing your friend's CV{role_text}.",
                "I could not find the candidate's email address in the CV. Please share their email address, or ask them to send the CV directly, so we can continue the review.",
            ),
        )

    def reply_status_followup(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        if application:
            position = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
            position_text = f" for {position}" if position else ""
            self.mailer.send_reply(
                inbox_email,
                f"Application status: {inbox_email.subject}",
                recruiter_email_body(
                    "Thanks for following up.",
                    f"We have your application{position_text} in our records. It is still under review, and our team will contact you if your profile is shortlisted.",
                ),
            )
        else:
            self.mailer.send_reply(
                inbox_email,
                f"Application status: {inbox_email.subject}",
                recruiter_email_body(
                    "Thanks for following up.",
                    "I could not find a previous application linked to this email address. Please share your CV and the role you are interested in, and we will review it.",
                ),
            )

    def reply_withdrawal_confirmed(self, inbox_email: InboxEmail, application: dict[str, Any] | None):
        position = None
        if application:
            position = application.get("requirement_position") or application.get("matched_position") or application.get("detected_position")
        position_text = f" for {position}" if position else ""
        self.mailer.send_reply(
            inbox_email,
            f"Application withdrawn: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for letting us know.",
                f"We have noted your request and marked your application{position_text} as withdrawn. Wishing you all the best.",
            ),
        )

    def reply_supported_cv_required(self, inbox_email: InboxEmail):
        self.mailer.send_reply(
            inbox_email,
            f"CV attachment required: {inbox_email.subject}",
            recruiter_email_body(
                "Thanks for your message.",
                "I could not find a readable CV attachment. Please send the CV as a PDF, DOCX, or TXT file, and we will review it.",
            ),
        )

    @traceable(name="process_recruiting_email")
    def process_email(self, inbox_email: InboxEmail) -> bool:
        thread_context = build_thread_context(inbox_email)
        classification = self.ai.classify_email(inbox_email)
        latest_application = self.db.latest_application_for_email(inbox_email.sender)

        if is_withdrawal_request(inbox_email.body):
            withdrawn_application = self.db.mark_latest_application_withdrawn(inbox_email.sender)
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

        if not classification.get("is_employment_related"):
            self.db.log_email_event(inbox_email, "ignored_non_employment", classification)
            return False

        requirements = self.db.open_requirements()
        is_referral = is_referral_thread(thread_context)
        use_cv_role_override = is_cv_role_override_request(inbox_email.body, thread_context)

        cv_attachments = [
            (filename, payload)
            for filename, payload in inbox_email.attachments
            if is_cv_filename(filename)
        ]
        if not cv_attachments:
            if inbox_email.attachments:
                self.db.log_email_event(
                    inbox_email,
                    "unsupported_attachment",
                    {
                        **classification,
                        "attachments": [filename for filename, _ in inbox_email.attachments],
                    },
                )
                self.reply_supported_cv_required(inbox_email)
                return True

            if is_status_followup(inbox_email.body):
                self.db.log_email_event(
                    inbox_email,
                    "status_followup",
                    {
                        **classification,
                        "application_found": latest_application is not None,
                        "application_id": latest_application["id"] if latest_application else None,
                    },
                )
                self.reply_status_followup(inbox_email, latest_application)
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
            matched_requirement_id = safe_int(match.get("requirement_id"))
            requirement = next((row for row in requirements if row["id"] == matched_requirement_id), None)
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
                self.reply_missing_cv(inbox_email)
            else:
                self.reply_no_opening(inbox_email, classification.get("detected_position"))
            return True

        for filename, payload in cv_attachments:
            cv_text = extract_cv_text(filename, payload)
            if not cv_text:
                self.db.log_email_event(
                    inbox_email,
                    "cv_text_extract_failed",
                    {"filename": filename},
                )
                continue

            extracted = self.ai.extract_cv_details(cv_text, thread_context)
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
                self.reply_wrong_cv(inbox_email, requested_position, cv_position)
                continue

            if not extracted.get("target_position") and not use_cv_role_override:
                extracted["target_position"] = classification.get("detected_position")

            source_text_for_match = cv_text[:4000] if use_cv_role_override else f"{thread_context}\n{cv_text[:4000]}"
            classification_for_match = (
                {"detected_position": cv_position}
                if use_cv_role_override
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
                match = self.ai.match_requirement(extracted, requirements)

            matched_requirement_id = safe_int(match.get("requirement_id"))
            requirement = next((row for row in requirements if row["id"] == matched_requirement_id), None)
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

            evaluation = self.ai.evaluate_cv(cv_text, extracted, requirement)
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
            if application_id:
                try:
                    uploaded_filename = upload_cv_attachment(application_id, filename, payload)
                    self.db.update_application_attachment_filename(application_id, uploaded_filename)
                    self.db.log_email_event(
                        inbox_email,
                        "cv_uploaded",
                        {
                            "application_id": application_id,
                            "original_filename": filename,
                            "attachment_filename": uploaded_filename,
                            "attachment_sha256": attachment_sha256,
                        },
                    )
                except Exception as exc:
                    self.db.log_email_event(
                        inbox_email,
                        "cv_upload_failed",
                        {
                            "application_id": application_id,
                            "original_filename": filename,
                            "attachment_sha256": attachment_sha256,
                            "error": str(exc),
                        },
                    )

            if requirement:
                if is_referral and candidate_email:
                    self.reply_referral_received(inbox_email, candidate_email, requirement["position_title"])
                elif is_referral:
                    self.reply_referral_missing_candidate_email(inbox_email, requirement["position_title"])
                else:
                    self.reply_received(inbox_email)
            else:
                self.reply_no_opening(inbox_email, extracted.get("target_position"))

        return True

    def run_once(self):
        self.init_schema()
        print(f"Using mail provider: {self.inbox.provider_name}")
        emails = self.inbox.fetch_unseen(RECRUITER_POLL_LIMIT)
        if not emails:
            print("No unread recruiter emails found.")
            return

        for inbox_email in emails:
            try:
                should_mark_seen = self.process_email(inbox_email)
                if should_mark_seen:
                    self.inbox.mark_seen(inbox_email.uid)
                    print(f"Processed: {inbox_email.subject} from {inbox_email.sender}")
                else:
                    self.inbox.mark_unseen(inbox_email.uid)
                    print(f"Ignored non-recruitment email and left unread: {inbox_email.subject} from {inbox_email.sender}")
            except Exception as exc:
                print(f"Failed to process {inbox_email.subject} from {inbox_email.sender}: {exc}")
                try:
                    self.db.log_email_event(
                        inbox_email,
                        "processing_failed",
                        {"error": str(exc)},
                    )
                except Exception as log_exc:
                    print(f"Could not log processing failure: {log_exc}")

    def process_one_graph_message(self, message_id: str):
        self.init_schema()
        if not isinstance(self.inbox, MicrosoftGraphProvider):
            print("Single-message Graph processing is only available with MAIL_PROVIDER=microsoft_graph.")
            return

        inbox_email = self.inbox.fetch_message_by_id(message_id)
        if not inbox_email:
            print(f"Microsoft Graph message not found or unavailable: {message_id}")
            return

        try:
            should_mark_seen = self.process_email(inbox_email)
            if should_mark_seen:
                self.inbox.mark_seen(inbox_email.uid)
                print(f"Processed Graph message only: {inbox_email.subject} from {inbox_email.sender}")
            else:
                self.inbox.mark_unseen(inbox_email.uid)
                print(f"Ignored non-recruitment Graph message and left unread: {inbox_email.subject} from {inbox_email.sender}")
        except Exception as exc:
            print(f"Failed to process Graph message {message_id}: {exc}")
            try:
                self.db.log_email_event(
                    inbox_email,
                    "processing_failed",
                    {"error": str(exc), "graph_message_id": message_id},
                )
            except Exception as log_exc:
                print(f"Could not log processing failure: {log_exc}")

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
        httpd = ThreadingHTTPServer(
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
        self.agent = AIRecruiterAgent()
        self.lock = Lock()

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
        message_ids = []
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
            message_ids.append(message_id)

        if not message_ids:
            return

        with self.lock:
            unique_message_ids = list(dict.fromkeys(message_ids))
            print(f"Microsoft Graph trigger received for {len(unique_message_ids)} message(s)")
            for message_id in unique_message_ids:
                self.agent.process_one_graph_message(message_id)

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

    def serve_forever(self):
        self.agent.init_schema()
        httpd = ThreadingHTTPServer(
            (GRAPH_WEBHOOK_HOST, GRAPH_WEBHOOK_PORT),
            self.make_handler(),
        )
        print(
            f"Microsoft Graph webhook listening on "
            f"http://{GRAPH_WEBHOOK_HOST}:{GRAPH_WEBHOOK_PORT}{GRAPH_WEBHOOK_PATH}"
        )
        print("Register the subscription with --register-graph-subscription after exposing this URL over HTTPS.")
        try:
            httpd.serve_forever()
        finally:
            self.agent.close()


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
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=RECRUITER_POLL_SECONDS,
        help="Seconds between inbox checks in watch mode",
    )
    args = parser.parse_args()

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

    agent = AIRecruiterAgent()
    try:
        if args.run_once:
            agent.run_once()
            print("Recruiter inbox pass complete.")
            return

        if args.watch:
            agent.run_forever(args.poll_seconds)
            return

        if args.serve_gmail_webhook:
            agent.close()
            GmailPushWebhookServer().serve_forever()
            return

        if args.serve_graph_webhook:
            agent.close()
            GraphWebhookServer().serve_forever()
            return

        parser.print_help()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
