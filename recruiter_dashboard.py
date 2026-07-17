import argparse
import json
import mimetypes
import re
from datetime import datetime
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from config import DB_PROVIDER
from recruiter_agent import RecruiterDatabase


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


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
            requirement_id = detail_path(parsed.path, "requirements")
            candidate_id = detail_path(parsed.path, "candidates")
            application_id = detail_path(parsed.path, "applications")
            event_id = detail_path(parsed.path, "events")
            candidate_cv_id = download_path(parsed.path, "candidates")
            application_cv_id = download_path(parsed.path, "applications")

            if parsed.path == "/":
                self.render_page("overview", self.render_overview())
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
            else:
                self.send_error(404, "Page not found")
        except Exception as exc:
            self.render_error(exc)

    def db(self) -> DashboardDB:
        return DashboardDB()

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
                    rr.position_title AS requirement_position, rr.job_description
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
        return f"""
        {self.back_link("/applications", "Back to applications")}
        <section class="panel">
            <div class="panel-head">
                <h2>{html_escape(title)}</h2>
                <div class="action-stack">
                    {status_badge(row["application_status"])}
                    <a class="link-button" href="/applications/{html_escape(application_id)}/cv">Download CV</a>
                </div>
            </div>
            {self.detail_grid(fields)}
        </section>
        """

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
        options = ["matched_requirement", "no_open_requirement", "reviewed", "shortlisted", "rejected", "withdrawn"]
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
