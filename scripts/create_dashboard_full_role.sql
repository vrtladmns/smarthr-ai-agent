-- FULL database access for an external dashboard developer.
--
-- Everything: SELECT, INSERT, UPDATE, DELETE on every table, plus CREATE on the
-- schema so he can add his own (sessions, settings, whatever his app needs).
-- Not a superuser: he cannot drop the database, create roles, or read other
-- databases on the server.
--
-- Must be run by a role with CREATEROLE - usually the postgres superuser, not
-- the application's own role. setup_dashboard_developer.sh finds one for you.
--
--   :app_password  the new login's password
--   :owner_role    the role that owns the agent's tables (usually recruiter).
--                  Default privileges have to be attached to the owner, not to
--                  whoever runs this script, or tables created by a later
--                  migration are unreachable for the developer.
--
-- Note for the record: this grant includes recruiter_sent_replies and
-- recruiter_processed_messages, which are how the agent avoids emailing a
-- candidate the same thing twice and avoids answering one email twice. Nothing
-- needs to touch them; leaving them alone keeps that protection intact.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_dev') THEN
        CREATE ROLE dashboard_dev LOGIN;
        RAISE NOTICE 'created role dashboard_dev';
    ELSE
        RAISE NOTICE 'role dashboard_dev already exists - updating it';
    END IF;
END
$$;

ALTER ROLE dashboard_dev WITH PASSWORD :app_password;
-- A login role, but not an administrator of the server.
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

-- And anything the agent creates later, so a migration cannot lock him out.
-- Attached to the table owner: default privileges only apply to objects made by
-- the role named here.
ALTER DEFAULT PRIVILEGES FOR ROLE :owner_role IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO dashboard_dev;
ALTER DEFAULT PRIVILEGES FOR ROLE :owner_role IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO dashboard_dev;

SELECT 'dashboard_dev can read and write every table in public; future tables by '
       || :'owner_role' || ' included' AS result;
