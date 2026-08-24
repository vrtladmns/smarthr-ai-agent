-- Database access for an external .NET/React dashboard.
--
-- Read-only, through views that form a stable contract: the dashboard is
-- insulated from column changes inside recruiter_applications, and never sees
-- raw CV bytes or full CV text.
--
-- Writes are deliberately NOT granted. Every dashboard action that matters
-- (approve, reject, send link, schedule) also sends email, creates a Teams
-- meeting or re-runs an evaluation. A row update alone does none of that and
-- would leave the candidate waiting on a message nobody sent. Those actions go
-- through the agent's API, not through SQL.
--
-- Run as the owner:
--   psql "$DATABASE_URL" -v app_password="'strong-password'" -f scripts/create_dashboard_role.sql

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_app') THEN
        CREATE ROLE dashboard_app LOGIN;
    END IF;
END
$$;

ALTER ROLE dashboard_app WITH PASSWORD :app_password;
ALTER ROLE dashboard_app WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE dashboard_app SET statement_timeout = '15s';
ALTER ROLE dashboard_app SET idle_in_transaction_session_timeout = '30s';
-- A runaway dashboard must not starve the agent of connections.
ALTER ROLE dashboard_app CONNECTION LIMIT 20;

GRANT CONNECT ON DATABASE recruitment TO dashboard_app;
GRANT USAGE ON SCHEMA public TO dashboard_app;
REVOKE CREATE ON SCHEMA public FROM dashboard_app;

-- ---------------------------------------------------------------- the contract

CREATE OR REPLACE VIEW dash_requirements AS
SELECT id, position_title, experience_min_years, experience_max_years,
       budget_min, budget_max, currency, job_description, recommended_questions,
       urgently_required, needed_within_days, status, created_at, updated_at
FROM recruitment_requirements;

CREATE OR REPLACE VIEW dash_candidates AS
SELECT id, candidate_uid, full_name, candidate_email, source_email, phone,
       location, current_title, current_company, total_experience_years,
       skills, education, work_history, certifications, cv_summary,
       ats_score, created_at, updated_at
FROM recruiter_candidates;

-- Applications joined to the useful bits of both sides. No attachment_payload,
-- no raw_cv_text: a CV is fetched from the agent, not selected from a table.
CREATE OR REPLACE VIEW dash_applications AS
SELECT a.id,
       a.application_uid,
       a.candidate_id,
       a.requirement_id,
       r.position_title            AS requirement_position,
       c.full_name                 AS candidate_name,
       COALESCE(a.candidate_email, a.source_email) AS candidate_email,
       a.submission_type,
       a.detected_position,
       a.matched_position,
       a.application_status,
       a.ats_score,
       a.jd_match_score,
       a.ai_short_description,
       a.strengths,
       a.risks,
       a.missing_requirements,
       a.screening_details,
       a.screening_current_salary,
       a.screening_expected_salary,
       a.screening_current_location,
       a.screening_joining_days,
       a.budget_disclosed_at,
       a.budget_response,
       a.hr_escalated_at,
       a.hr_escalation_reason,
       a.hr_approved_at,
       a.human_handled_at,
       a.interview_link_created_at,
       a.interview_started_at,
       a.interview_completed_at,
       a.interview_scheduled_at,
       a.interview_attempts,
       a.interview_availability,
       a.hr_interviewer_name,
       a.teams_join_url,
       a.interview_report,
       a.attachment_filename,
       (a.attachment_payload IS NOT NULL) AS has_cv_file,
       a.received_at,
       a.created_at
FROM recruiter_applications a
LEFT JOIN recruiter_candidates       c ON c.id = a.candidate_id
LEFT JOIN recruitment_requirements   r ON r.id = a.requirement_id;

-- What the agent has said to a candidate, so the dashboard can show a timeline
-- without exposing message bodies.
CREATE OR REPLACE VIEW dash_activity AS
SELECT id, application_id, recipient, scenario, sent_at
FROM recruiter_sent_replies;

CREATE OR REPLACE VIEW dash_events AS
SELECT id, email_message_id, source_email, email_subject, event_type, created_at
FROM recruiter_email_events;

GRANT SELECT ON dash_requirements, dash_candidates, dash_applications,
                dash_activity, dash_events
TO dashboard_app;

-- Nothing automatic on anything created later.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM dashboard_app;

SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'dashboard_app'
ORDER BY table_name;
