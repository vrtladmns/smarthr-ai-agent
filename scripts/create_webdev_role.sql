-- Least-privilege database role for an external web developer.
--
-- Why not just hand over the recruiter account: it owns every table, so it can
-- DROP them, and nothing in the logs would distinguish their queries from the
-- agent's. This role is read-only, scoped to the tables a front end needs, and
-- can be revoked on its own.
--
-- Run as the recruiter (owner) user:
--   psql "$DATABASE_URL" -v webdev_password="'choose-a-strong-one'" -f scripts/create_webdev_role.sql

\set ON_ERROR_STOP on

-- 1. The role. LOGIN only, no CREATEDB, no CREATEROLE, no SUPERUSER.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'webdev') THEN
        CREATE ROLE webdev LOGIN;
    END IF;
END
$$;

ALTER ROLE webdev WITH PASSWORD :webdev_password;
ALTER ROLE webdev WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
-- Stops one bad query pinning a connection forever.
ALTER ROLE webdev SET statement_timeout = '30s';
ALTER ROLE webdev SET idle_in_transaction_session_timeout = '60s';

-- 2. Connect and look, nothing more.
GRANT CONNECT ON DATABASE recruitment TO webdev;
GRANT USAGE ON SCHEMA public TO webdev;
REVOKE CREATE ON SCHEMA public FROM webdev;

-- 3. Read access, table by table, so adding a table later is a deliberate act.
GRANT SELECT ON recruitment_requirements TO webdev;
GRANT SELECT ON recruiter_email_events   TO webdev;

-- 4. Candidates and applications go through views that leave out the raw CV
--    bytes and full CV text. A front end needs the summary, not the document.
CREATE OR REPLACE VIEW webdev_candidates AS
SELECT id, candidate_uid, full_name, candidate_email, source_email, location,
       current_title, current_company, total_experience_years, skills,
       education, certifications, cv_summary, ats_score, created_at, updated_at
FROM recruiter_candidates;

CREATE OR REPLACE VIEW webdev_applications AS
SELECT id, application_uid, candidate_id, requirement_id, candidate_email,
       source_email, submission_type, detected_position, matched_position,
       application_status, ats_score, jd_match_score, ai_short_description,
       screening_current_salary, screening_expected_salary,
       screening_current_location, screening_joining_days,
       interview_scheduled_at, interview_completed_at, created_at
FROM recruiter_applications;

GRANT SELECT ON webdev_candidates   TO webdev;
GRANT SELECT ON webdev_applications TO webdev;

-- 5. Nothing granted by default on anything created in future.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM webdev;

-- 6. Show what the role ended up with.
SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'webdev'
ORDER BY table_name, privilege_type;
