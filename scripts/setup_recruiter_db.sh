#!/usr/bin/env bash
set -euo pipefail

docker compose up -d recruiter-postgres

echo "Waiting for PostgreSQL to become healthy..."
until docker inspect --format='{{json .State.Health.Status}}' company-policy-recruiter-postgres | grep -q '"healthy"'; do
  sleep 2
done

./venv/bin/python recruiter_agent.py --init-db
./venv/bin/python scripts/seed_recruitment_requirements.py

echo "Recruiter PostgreSQL database, tables, and sample requirements are ready."
