-- FULL database access for an external dashboard developer.
--
-- Everything: SELECT, INSERT, UPDATE, DELETE on every table, plus CREATE on the
-- schema so he can add his own (sessions, settings, whatever his app needs).
-- Not a superuser: he cannot drop the database, create roles, or read other
-- databases on the server.
--
-- Note for the record: this includes recruiter_sent_replies and
-- recruiter_processed_messages, which are how the agent avoids emailing a
-- candidate the same thing twice and avoids answering one email twice. They are
-- writable under this grant. Nothing needs to touch them; leaving them alone
-- keeps that protection intact.
--
--   psql "$DATABASE_URL" -v app_password="'strong-password'" \
--        -f scripts/create_dashboard_full_role.sql

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_dev') THEN
        CREATE ROLE dashboard_dev LOGIN;
    END IF;
END
$$;

ALTER ROLE dashboard_dev WITH PASSWORD :app_password;
-- Login role, but not an administrator of the server.
ALTER ROLE dashboard_dev WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
-- Keeps one runaway query from pinning the database for the agent.
ALTER ROLE dashboard_dev SET statement_timeout = '60s';
ALTER ROLE dashboard_dev SET idle_in_transaction_session_timeout = '120s';
ALTER ROLE dashboard_dev CONNECTION LIMIT 30;

GRANT CONNECT ON DATABASE recruitment TO dashboard_dev;
GRANT USAGE, CREATE ON SCHEMA public TO dashboard_dev;

-- Everything that exists now.
GRANT ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public TO dashboard_dev;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO dashboard_dev;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO dashboard_dev;

-- And anything the agent adds later, so a migration does not lock him out.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO dashboard_dev;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO dashboard_dev;

SELECT 'dashboard_dev can now read and write every table in public' AS result;
