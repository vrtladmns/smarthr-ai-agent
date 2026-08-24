#!/usr/bin/env bash
# One-shot: create a read-only database user for an outside developer and print
# everything you need to send them.
#
#   ./scripts/setup_webdev_access.sh
#   ./scripts/setup_webdev_access.sh 'your-own-password'
#
# Works whether Postgres runs natively or in Docker. Changes nothing else:
# no restart, no config edit, no firewall change.

set -euo pipefail
cd "$(dirname "$0")/.."

SQL_FILE="scripts/create_webdev_role.sql"
[ -f "$SQL_FILE" ] || { echo "missing $SQL_FILE"; exit 1; }

PASSWORD="${1:-$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)}"

# Pick up DATABASE_URL from .env without sourcing the whole file.
DB_URL="$(grep -E '^DATABASE_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
DB_NAME="${DB_URL##*/}"; DB_NAME="${DB_NAME%%\?*}"; DB_NAME="${DB_NAME:-recruitment}"

# There may be several Postgres containers on the box. Pick the one that
# actually holds this database, by asking each of them.
CONTAINER=""
for candidate in $(docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | grep -i postgres | cut -f1); do
  if docker exec -i "$candidate" psql -U recruiter -d "$DB_NAME" -tAc 'select 1' >/dev/null 2>&1; then
    CONTAINER="$candidate"
    break
  fi
done

echo "database : $DB_NAME"
if [ -n "$CONTAINER" ]; then
  echo "postgres : docker container '$CONTAINER'"
  docker exec -i "$CONTAINER" psql -U recruiter -d "$DB_NAME" \
    -v webdev_password="'$PASSWORD'" -q < "$SQL_FILE"
else
  echo "postgres : native service"
  if ! psql "$DB_URL" -tAc 'select 1' >/dev/null 2>&1; then
    echo
    echo "Could not reach the database as the recruiter user."
    echo "  DATABASE_URL in .env : ${DB_URL:-(not set)}"
    echo "  Check it is running  : sudo ss -ltnp | grep 5432"
    exit 1
  fi
  psql "$DB_URL" -v webdev_password="'$PASSWORD'" -q -f "$SQL_FILE"
fi

SERVER_USER="${SUDO_USER:-$(whoami)}"
SERVER_HOST="$(curl -s --max-time 3 https://checkip.amazonaws.com 2>/dev/null || echo '<your-server>')"

cat <<MSG

──────────────────────────────────────────────────────────────────
 Done. Send the developer this, and the password separately.
──────────────────────────────────────────────────────────────────

 1. Ask him for his SSH public key, then on this server run:
      echo "<his-public-key>" >> ~/.ssh/authorized_keys

 2. He opens a tunnel (leave it running):
      ssh -N -L 5432:127.0.0.1:5432 ${SERVER_USER}@${SERVER_HOST}

 3. He connects with:
      postgresql://webdev:${PASSWORD}@127.0.0.1:5432/${DB_NAME}

 He gets read-only access to recruitment_requirements,
 webdev_candidates and webdev_applications. Raw CV files, full CV
 text and the reply ledger are not visible, and he cannot write
 anything.

 To revoke:
   DROP OWNED BY webdev; DROP ROLE webdev;

 Password (send out-of-band, not in the same message as the host):
   ${PASSWORD}
──────────────────────────────────────────────────────────────────
MSG
