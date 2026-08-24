#!/usr/bin/env bash
# Create the dashboard developer's database login and print what to send him.
#
#   ./scripts/setup_dashboard_developer.sh                 # generates a password
#   ./scripts/setup_dashboard_developer.sh 'my-password'
#
# Creating a role needs CREATEROLE, which the application's own role usually
# does not have. This finds an administrative connection by trying, in order:
#
#   ADMIN_DATABASE_URL   if you set it
#   sudo -u postgres     the normal native install
#   docker exec          if postgres runs in a container
#   DATABASE_URL         only if that role happens to have CREATEROLE
#
# It creates the role only. The network steps it prints are for you to apply
# deliberately - it opens nothing.

set -euo pipefail
cd "$(dirname "$0")/.."

SQL_FILE="scripts/create_dashboard_full_role.sql"
[ -f "$SQL_FILE" ] || { echo "missing $SQL_FILE"; exit 1; }

PASSWORD="${1:-$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"

DB_URL="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"'')"
DB_NAME="${DB_URL##*/}"; DB_NAME="${DB_NAME%%\?*}"; DB_NAME="${DB_NAME:-recruitment}"

# --- find a connection that can create a role --------------------------------
MODE=""; CONTAINER=""

can_create_role() {   # can_create_role <mode> [container]
  local probe="select 1 from pg_roles where rolname = current_user and (rolsuper or rolcreaterole)"
  local out
  case "$1" in
    admin_url) out="$(psql "$ADMIN_DATABASE_URL" -tAc "$probe" 2>/dev/null || true)" ;;
    sudo)      out="$(sudo -n -u postgres psql -d "$DB_NAME" -tAc "$probe" 2>/dev/null || true)" ;;
    docker)    out="$(docker exec -i "$2" psql -U postgres -d "$DB_NAME" -tAc "$probe" 2>/dev/null || true)" ;;
    app_url)   out="$(psql "$DB_URL" -tAc "$probe" 2>/dev/null || true)" ;;
  esac
  [ "$out" = "1" ]
}

if [ -n "${ADMIN_DATABASE_URL:-}" ] && can_create_role admin_url; then
  MODE=admin_url; ROUTE="ADMIN_DATABASE_URL"
elif command -v sudo >/dev/null && can_create_role sudo; then
  MODE=sudo; ROUTE="sudo -u postgres"
else
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -i postgres || true); do
    if can_create_role docker "$c"; then MODE=docker; CONTAINER="$c"; ROUTE="docker exec $c (postgres)"; break; fi
  done
  if [ -z "$MODE" ] && can_create_role app_url; then MODE=app_url; ROUTE="DATABASE_URL"; fi
fi

if [ -z "$MODE" ]; then
  cat <<MSG

No connection available that can create a role.

The application's own role cannot do it - that is expected and correct. Use the
postgres superuser instead. Pick whichever matches your server:

  # native install, run as a user with sudo
  sudo -u postgres ./scripts/setup_dashboard_developer.sh

  # postgres in Docker
  docker exec -i <container> psql -U postgres -d ${DB_NAME} \\
      -v app_password="'a-strong-password'" -v owner_role=recruiter \\
      < scripts/create_dashboard_full_role.sql

  # managed Postgres (RDS) - use the master user
  ADMIN_DATABASE_URL='postgresql://master:pw@host:5432/${DB_NAME}' \\
      ./scripts/setup_dashboard_developer.sh

Or grant the application role the right once, then re-run this script:

  sudo -u postgres psql -d ${DB_NAME} -c 'ALTER ROLE recruiter CREATEROLE'

MSG
  exit 1
fi

run_admin() {   # run_admin <psql args...>
  case "$MODE" in
    admin_url) psql "$ADMIN_DATABASE_URL" "$@" ;;
    sudo)      sudo -n -u postgres psql -d "$DB_NAME" "$@" ;;
    docker)    docker exec -i "$CONTAINER" psql -U postgres -d "$DB_NAME" "$@" ;;
    app_url)   psql "$DB_URL" "$@" ;;
  esac
}

echo "admin route : $ROUTE"

# Default privileges must be attached to whoever owns the agent's tables.
OWNER="$(run_admin -tAc "select tableowner from pg_tables where tablename='recruiter_applications'" 2>/dev/null | tr -d '[:space:]')"
OWNER="${OWNER:-recruiter}"
echo "table owner : $OWNER"

run_admin -q -v app_password="'$PASSWORD'" -v owner_role="$OWNER" -f /dev/stdin < "$SQL_FILE"

PUBLIC_IP="$(curl -s --max-time 4 https://checkip.amazonaws.com 2>/dev/null | tr -d '\n' || true)"
PUBLIC_IP="${PUBLIC_IP:-<YOUR-ELASTIC-IP>}"
LISTENING="$(ss -ltn 2>/dev/null | awk '$4 ~ /:5432$/ {print $4}' | paste -sd' ' || true)"

cat <<MSG

══════════════════════════════════════════════════════════════════
 Role created: dashboard_dev  (full read/write on every table)
══════════════════════════════════════════════════════════════════

 SEND HIM THIS (.NET / Npgsql):

   Host=${PUBLIC_IP};Port=5432;Database=${DB_NAME};Username=dashboard_dev;Password=${PASSWORD};SSL Mode=Require;Trust Server Certificate=true

 Or as a URL (psql, DBeaver, most tools):

   postgresql://dashboard_dev:${PASSWORD}@${PUBLIC_IP}:5432/${DB_NAME}?sslmode=require

 Send the password separately from the host.

──────────────────────────────────────────────────────────────────
 STILL TO DO - the string above will not connect until you do this
──────────────────────────────────────────────────────────────────

 Listening on now: ${LISTENING:-127.0.0.1:5432 (loopback only)}

 1. postgresql.conf
       listen_addresses = '*'

 2. pg_hba.conf   (his server's IP, TLS enforced)
       hostssl  ${DB_NAME}  dashboard_dev  <HIS.SERVER.IP>/32  scram-sha-256

 3. AWS security group
       allow TCP 5432 from <HIS.SERVER.IP>/32   -- that address only

 4. Reload
       sudo systemctl reload postgresql     # or: docker restart ${CONTAINER:-<container>}

 If Postgres runs in Docker, step 1 is already done - Docker publishes on all
 interfaces and bypasses ufw. Steps 2-4 still apply.

──────────────────────────────────────────────────────────────────
 Verify from his side:
   psql "postgresql://dashboard_dev:PASSWORD@${PUBLIC_IP}:5432/${DB_NAME}?sslmode=require" -c "select count(*) from recruiter_applications"

 Rotate the password later:
   ALTER ROLE dashboard_dev WITH PASSWORD 'new-one';

 Revoke completely:
   DROP OWNED BY dashboard_dev; DROP ROLE dashboard_dev;
══════════════════════════════════════════════════════════════════

 Password: ${PASSWORD}
MSG
