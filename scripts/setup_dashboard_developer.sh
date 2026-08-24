#!/usr/bin/env bash
# Create the developer's database login and print everything to send him.
#
#   ./scripts/setup_dashboard_developer.sh                 # generates a password
#   ./scripts/setup_dashboard_developer.sh 'my-password'
#
# Creates the role only. It does not open any port - the network steps it
# prints are for you to apply deliberately.

set -euo pipefail
cd "$(dirname "$0")/.."

SQL_FILE="scripts/create_dashboard_full_role.sql"
[ -f "$SQL_FILE" ] || { echo "missing $SQL_FILE"; exit 1; }

PASSWORD="${1:-$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"

DB_URL="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
DB_NAME="${DB_URL##*/}"; DB_NAME="${DB_NAME%%\?*}"; DB_NAME="${DB_NAME:-recruitment}"

CONTAINER=""
for candidate in $(docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | grep -i postgres | cut -f1); do
  if docker exec -i "$candidate" psql -U recruiter -d "$DB_NAME" -tAc 'select 1' >/dev/null 2>&1; then
    CONTAINER="$candidate"; break
  fi
done

if [ -n "$CONTAINER" ]; then
  echo "postgres : docker container '$CONTAINER'"
  docker exec -i "$CONTAINER" psql -U recruiter -d "$DB_NAME" \
    -v app_password="'$PASSWORD'" -q < "$SQL_FILE"
else
  echo "postgres : native service"
  psql "$DB_URL" -v app_password="'$PASSWORD'" -q -f "$SQL_FILE"
fi

PUBLIC_IP="$(curl -s --max-time 4 https://checkip.amazonaws.com 2>/dev/null | tr -d '\n' || true)"
PUBLIC_IP="${PUBLIC_IP:-<YOUR-ELASTIC-IP>}"
LISTENING="$(ss -ltn 2>/dev/null | awk '$4 ~ /:5432$/ {print $4}' | head -1 || true)"

cat <<MSG

══════════════════════════════════════════════════════════════════
 Role created: dashboard_dev  (full read/write on every table)
══════════════════════════════════════════════════════════════════

 SEND HIM THIS (.NET / Npgsql):

   Host=${PUBLIC_IP};Port=5432;Database=${DB_NAME};Username=dashboard_dev;Password=${PASSWORD};SSL Mode=Require;Trust Server Certificate=true

 Or as a URL (psql, DBeaver, most tools):

   postgresql://dashboard_dev:${PASSWORD}@${PUBLIC_IP}:5432/${DB_NAME}?sslmode=require

 Send the password in a separate message from the host.

──────────────────────────────────────────────────────────────────
 STILL TO DO - the string above will not connect until you do this
──────────────────────────────────────────────────────────────────

 Currently listening on: ${LISTENING:-127.0.0.1:5432 (loopback only)}

 1. postgresql.conf
       listen_addresses = '*'

 2. pg_hba.conf   (his server's IP, TLS enforced)
       hostssl  ${DB_NAME}  dashboard_dev  <HIS.SERVER.IP>/32  scram-sha-256

 3. AWS security group
       allow TCP 5432 from <HIS.SERVER.IP>/32   -- that address only

 4. Restart
       sudo systemctl restart postgresql        # or: docker restart ${CONTAINER:-<container>}

 If Postgres runs in Docker, step 1 is already done - Docker publishes
 on all interfaces. Steps 2-4 still apply.

──────────────────────────────────────────────────────────────────
 Verify from his side:
   psql "postgresql://dashboard_dev:PASSWORD@${PUBLIC_IP}:5432/${DB_NAME}?sslmode=require" -c "select count(*) from recruiter_applications"

 Change the password later:
   ALTER ROLE dashboard_dev WITH PASSWORD 'new-one';

 Revoke completely:
   DROP OWNED BY dashboard_dev; DROP ROLE dashboard_dev;
══════════════════════════════════════════════════════════════════

 Password: ${PASSWORD}
MSG
