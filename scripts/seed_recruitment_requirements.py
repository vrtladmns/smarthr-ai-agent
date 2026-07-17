import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recruiter_agent import RecruiterDatabase


REQUIREMENTS = [
    {
        "position_title": "Python Developer",
        "experience_min_years": 2,
        "experience_max_years": 5,
        "budget_min": 600000,
        "budget_max": 1200000,
        "currency": "INR",
        "job_description": """
We are hiring a Python Developer to build backend services, APIs, automation workflows, and integrations.
The candidate should have strong Python fundamentals, experience with FastAPI or Django, PostgreSQL,
REST APIs, Git, debugging, and clean code practices. Experience with Docker, cloud deployment,
LangChain, AI workflows, or data pipelines is a plus.
""".strip(),
        "urgently_required": True,
        "needed_within_days": 15,
        "status": "open",
    },
    {
        "position_title": "Accountant",
        "experience_min_years": 1,
        "experience_max_years": 4,
        "budget_min": 300000,
        "budget_max": 600000,
        "currency": "INR",
        "job_description": """
We are hiring an Accountant to manage daily accounting, bookkeeping, invoices, GST/TDS records,
bank reconciliation, vendor payments, expense tracking, and monthly financial reports.
The candidate should understand accounting principles, Excel, Tally or similar accounting software,
statutory compliance, and accurate documentation.
""".strip(),
        "urgently_required": False,
        "needed_within_days": 30,
        "status": "open",
    },
]


UPSERT_SQL = """
INSERT INTO recruitment_requirements
(
    position_title, experience_min_years, experience_max_years,
    budget_min, budget_max, currency, job_description,
    urgently_required, needed_within_days, status
)
VALUES
(
    %(position_title)s, %(experience_min_years)s, %(experience_max_years)s,
    %(budget_min)s, %(budget_max)s, %(currency)s, %(job_description)s,
    %(urgently_required)s, %(needed_within_days)s, %(status)s
)
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
    updated_at = NOW();
"""


def main():
    try:
        db = RecruiterDatabase()
    except Exception as exc:
        raise SystemExit(
            "Could not connect to PostgreSQL. Start the local DB first with:\n"
            "  sudo docker compose up -d recruiter-postgres\n"
            "Then run:\n"
            "  ./venv/bin/python scripts/seed_recruitment_requirements.py\n\n"
            f"Original error: {exc}"
        ) from exc

    try:
        db.init_schema()
        if db.is_mssql():
            for requirement in REQUIREMENTS:
                existing = db.one(
                    "SELECT id FROM recruitment_requirements WHERE LOWER(position_title) = LOWER(%s) LIMIT 1",
                    (requirement["position_title"],),
                )
                if existing:
                    db.execute(
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
                        (
                            requirement["experience_min_years"],
                            requirement["experience_max_years"],
                            requirement["budget_min"],
                            requirement["budget_max"],
                            requirement["currency"],
                            requirement["job_description"],
                            requirement["urgently_required"],
                            requirement["needed_within_days"],
                            requirement["status"],
                            existing["id"],
                        ),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO recruitment_requirements
                        (
                            position_title, experience_min_years, experience_max_years,
                            budget_min, budget_max, currency, job_description,
                            urgently_required, needed_within_days, status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            requirement["position_title"],
                            requirement["experience_min_years"],
                            requirement["experience_max_years"],
                            requirement["budget_min"],
                            requirement["budget_max"],
                            requirement["currency"],
                            requirement["job_description"],
                            requirement["urgently_required"],
                            requirement["needed_within_days"],
                            requirement["status"],
                        ),
                    )
        else:
            with db.conn.cursor() as cursor:
                for requirement in REQUIREMENTS:
                    cursor.execute(UPSERT_SQL, requirement)
            db.conn.commit()
        print(f"Seeded {len(REQUIREMENTS)} recruitment requirements.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
