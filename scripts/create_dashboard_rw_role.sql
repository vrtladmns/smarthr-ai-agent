-- Read/write database access for an external .NET/React dashboard.
--
-- Grants real write access, with two deliberate exceptions:
--
--   recruiter_sent_replies       the agent's record of what it has already said.
--   recruiter_processed_messages the record of which emails it has handled.
--
-- Those two are how the agent avoids emailing a candidate the same thing twice
-- and avoids answering one email twice. An outside writer editing them would
-- reintroduce the exact loops this system was fixed for, so they stay read-only.
--
-- To make the agent DO something (send an email, book a Teams meeting, re-score
-- a CV) set requested_action - see the audit. A plain status UPDATE changes the
-- row and nothing else.
--
--   psql "$DATABASE_URL" -v app_password="'strong-password'" \
--        -f scripts/create_dashboard_rw_role.sql

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_rw') THEN
        CREATE ROLE dashboard_rw LOGIN;
    END IF;
END
$$;

ALTER ROLE dashboard_rw WITH PASSWORD :app_password;
ALTER ROLE dashboard_rw WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE dashboard_rw SET statement_timeout = '30s';
ALTER ROLE dashboard_rw SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE dashboard_rw CONNECTION LIMIT 20;

GRANT CONNECT ON DATABASE recruitment TO dashboard_rw;
GRANT USAGE ON SCHEMA public TO dashboard_rw;
REVOKE CREATE ON SCHEMA public FROM dashboard_rw;

-- Read everything, including the convenience views.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_rw;

-- Write the things a dashboard legitimately owns.
GRANT INSERT, UPDATE, DELETE ON recruitment_requirements TO dashboard_rw;
GRANT INSERT, UPDATE         ON recruiter_applications   TO dashboard_rw;
GRANT INSERT, UPDATE         ON recruiter_candidates     TO dashboard_rw;
GRANT INSERT                 ON recruiter_email_events   TO dashboard_rw;

-- Ask the agent to act. INSERT so it can queue work directly as well as via
-- requested_action; UPDATE is not granted, so it cannot mark its own jobs done.
GRANT INSERT ON agent_action_queue TO dashboard_rw;

-- Sequences, or INSERT fails.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dashboard_rw;

-- The two ledgers stay read-only. This is the invariant worth protecting.
REVOKE INSERT, UPDATE, DELETE ON recruiter_sent_replies       FROM dashboard_rw;
REVOKE INSERT, UPDATE, DELETE ON recruiter_processed_messages FROM dashboard_rw;

-- Anything added later must be granted deliberately.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM dashboard_rw;

SELECT table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE grantee = 'dashboard_rw'
GROUP BY table_name
ORDER BY table_name;
