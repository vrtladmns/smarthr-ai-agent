"""The agent was sending 49 HR notifications for 119 inbound candidate messages.

Measured over 2026-08-17..28 in the live mailbox: 20 "Possible match needs your
call" and 16 "Manual HR review needed". Every one of the 20 named Business
Development Executive or US Bookkeeper as the adjacent role, including for a
Software Test Engineer and an HR Business Partner, and the ATS scores were 78-88
against a screening bar of 60. These tests pin the causes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recruiter_agent as ra

REQUIREMENTS = [
    {"id": 1, "position_title": "Business Development Executive",
     "job_description": "Generate leads, build client relationships, drive the sales pipeline."},
    {"id": 2, "position_title": "US Bookkeeper",
     "job_description": "Bank reconciliation, QuickBooks, month end close, AP and AR for US clients."},
    {"id": 3, "position_title": "US Tax Preparer",
     "job_description": "Prepare US 1040 and 1120 returns and review workpapers."},
]

# An ordinary accounting CV. It mentions payroll, employees and clients in
# passing, the way real CVs do.
ACCOUNTANT_CV = (
    "Sunny Kumar, Senior Accountant. Eight years in accounts payable and receivable, "
    "QuickBooks, month end close, payroll processing for 200 employees, coordinated "
    "with HR on onboarding, supported business development with client reporting."
)


class StubAI:
    """Scores a CV the way the real evaluator would, without an LLM call."""

    def __init__(self, scores: dict[str, tuple[float, float]]):
        self.scores = scores
        self.calls: list[str] = []

    def evaluate_cv(self, cv_text, extracted, requirement):
        title = (requirement or {}).get("position_title", "")
        self.calls.append(title)
        ats, jd = self.scores.get(title, (10.0, 5.0))
        return {"ats_score": ats, "jd_match_score": jd, "recommendation": "x",
                "strengths": [], "risks": [], "missing_requirements": []}


# --- the field a candidate works in -------------------------------------------

def test_cv_prose_no_longer_invents_role_families():
    """Scanning cv_text[:2000] read hr and sales out of an accounting CV, so
    every accountant looked adjacent to Business Development Executive."""
    families = ra.candidate_role_families({"primary_role": "Senior Accountant"}, {}, ACCOUNTANT_CV)
    assert "accounting" in families
    assert "sales" not in families, "'business development' in prose is not a sales candidate"
    assert "hr" not in families, "'payroll' and 'onboarding' in prose is not an HR candidate"


def test_accountant_is_not_a_near_miss_for_business_development():
    titles = [r["position_title"] for r in
              ra.near_miss_requirements(REQUIREMENTS, {"primary_role": "Senior Accountant"}, {}, ACCOUNTANT_CV)]
    assert titles == ["US Bookkeeper"]


def test_the_unambiguous_match_is_now_reachable():
    """With three families the len(matches) == 1 test failed and the auto-decide
    path was skipped, which is why these reached HR at all."""
    sole = ra.single_family_requirement(REQUIREMENTS, {"primary_role": "Senior Accountant"}, {}, ACCOUNTANT_CV)
    assert sole is not None and sole["position_title"] == "US Bookkeeper"


# --- deciding on the JD score instead of on shared words ----------------------

def test_a_confident_model_match_is_not_vetoed_by_shared_words():
    """'Sr. Accountant' and 'US Bookkeeper' have no word in common."""
    assert not ra.requirement_is_compatible_with_candidate_role(
        {"current_title": "Sr. Accountant"}, {"detected_position": "Sr. Accountant"},
        REQUIREMENTS[1],
    ), "precondition: the overlap check still rejects this pair"
    assert ra.LLM_MATCH_TRUST_CONFIDENCE < 1.0
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    assert "requirement_title_veto_overridden" in source
    assert "match_confidence >= LLM_MATCH_TRUST_CONFIDENCE" in source


def test_score_resolves_the_role_that_titles_could_not():
    ai = StubAI({"US Bookkeeper": (85.0, 78.0), "Business Development Executive": (30.0, 12.0)})
    requirement, evaluation, scored = ra.best_requirement_by_score(
        ai, REQUIREMENTS, {"primary_role": "Senior Accountant"}, {"current_title": "Sr. Accountant"}, ACCOUNTANT_CV
    )
    assert requirement is not None and requirement["position_title"] == "US Bookkeeper"
    assert evaluation["ats_score"] == 85.0
    assert scored, "the attempt must be recorded for the log"


def test_an_unrelated_candidate_is_not_forced_into_a_role():
    """A QA engineer must not be resolved into Business Development Executive."""
    ai = StubAI({t["position_title"]: (25.0, 10.0) for t in REQUIREMENTS})
    requirement, _, scored = ra.best_requirement_by_score(
        ai, REQUIREMENTS, {"primary_role": "Software Test Engineer"},
        {"current_title": "Software Test Engineer"}, "Selenium, pytest, test plans, defect triage.",
    )
    assert requirement is None
    assert len(scored) >= 1


def test_scoring_is_capped_so_one_cv_cannot_fan_out():
    many = [{"id": i, "position_title": f"Role {i}", "job_description": "x"} for i in range(20)]
    ai = StubAI({})
    ra.best_requirement_by_score(ai, many, None, {"current_title": "Whatever"}, "some cv text")
    assert len(ai.calls) <= ra.REQUIREMENT_SCORING_MAX


def test_a_failing_evaluation_does_not_lose_the_other_roles():
    class Flaky(StubAI):
        def evaluate_cv(self, cv_text, extracted, requirement):
            if (requirement or {}).get("position_title") == "Business Development Executive":
                raise RuntimeError("model timeout")
            return super().evaluate_cv(cv_text, extracted, requirement)

    ai = Flaky({"US Bookkeeper": (85.0, 78.0)})
    requirement, _, _ = ra.best_requirement_by_score(
        ai, REQUIREMENTS, {"primary_role": "Senior Accountant"}, {"current_title": "Sr. Accountant"}, ACCOUNTANT_CV
    )
    assert requirement is not None and requirement["position_title"] == "US Bookkeeper"


# --- what still reaches a human -----------------------------------------------

def test_only_a_borderline_cv_is_worth_a_persons_time():
    assert ra.NEAR_MISS_AMBIGUOUS_BAND > 0
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    assert "near_miss_scored_below_band_no_handoff" in source
    assert "RECRUITER_SCREENING_JD_MIN - NEAR_MISS_AMBIGUOUS_BAND" in source


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


# --- replies that ask for nothing ---------------------------------------------

def test_a_closed_thread_courtesy_reply_reaches_nobody():
    assert not ra.message_needs_human_attention(
        "Noted, thank you for letting me know. I appreciate the update on my application."
    )
    assert not ra.message_needs_human_attention("Okay. Understood. Thanks a lot for your time.")


def test_a_real_question_on_a_closed_thread_still_reaches_a_person():
    for message in [
        "Thanks. Could you consider me for any other role?",
        "Why was my application rejected?",
        "Please reconsider my profile for the bookkeeping position.",
        "Can you share the feedback from the interview",
    ]:
        assert ra.message_needs_human_attention(message), message


# --- budget ------------------------------------------------------------------

def test_budget_gap_ratio():
    assert ra.budget_gap_ratio({"expected_salary": 1200000}, {"budget_max": 900000}) == pytest_approx(1.333)
    assert ra.budget_gap_ratio({"expected_salary": None}, {"budget_max": 900000}) is None
    assert ra.budget_gap_ratio({"expected_salary": 100}, {"budget_max": 0}) is None


def pytest_approx(value, tol=0.01):
    class _A:
        def __eq__(self, other): return abs(other - value) < tol
        def __repr__(self): return f"~{value}"
    return _A()


def test_a_hopeless_gap_is_not_worth_a_disclosure():
    """A candidate wanting well above the ceiling was sent the range, replied,
    and the thread then went to a human. Nothing was negotiable."""
    assert ra.BUDGET_GAP_MAX_RATIO > 1.0
    ratio = ra.budget_gap_ratio({"expected_salary": 2000000}, {"budget_max": 900000})
    assert ratio > ra.BUDGET_GAP_MAX_RATIO
    # a modest gap stays a conversation
    assert ra.budget_gap_ratio({"expected_salary": 1000000}, {"budget_max": 900000}) < ra.BUDGET_GAP_MAX_RATIO


def test_the_out_of_range_note_is_sent_at_most_once():
    assert "budget_out_of_range" in ra.ONCE_PER_APPLICATION_SCENARIOS


# --- 'business' is not a sales signal -----------------------------------------

def test_the_word_business_no_longer_means_sales():
    """Every one of the 20 escalations named Business Development Executive as
    the adjacent role, partly because the bare token matched any CV or title
    using the word."""
    assert ra.role_families_from_text("HR Business Partner") == {"hr"}
    assert ra.role_families_from_text("Business Analyst") == set()


def test_the_phrase_still_does():
    assert "sales" in ra.role_families_from_text("Business Development Executive")
    assert "sales" in ra.role_families_from_text("Sales Executive")
    assert "accounting" in ra.role_families_from_text("Accounts Payable Executive")


def test_an_hr_person_is_not_routed_into_a_sales_role():
    reqs = [{"id": 1, "position_title": "Business Development Executive"},
            {"id": 2, "position_title": "US Bookkeeper"}]
    assert ra.single_family_requirement(
        reqs, {"primary_role": "HR Business Partner"}, {"current_title": "HR Business Partner"}, ""
    ) is None


def test_a_real_sales_candidate_still_is():
    reqs = [{"id": 1, "position_title": "Business Development Executive"},
            {"id": 2, "position_title": "US Bookkeeper"}]
    match = ra.single_family_requirement(
        reqs, {"primary_role": "Sales Executive"}, {"current_title": "Sales Executive"}, ""
    )
    assert match is not None and match["position_title"] == "Business Development Executive"


# --- salary units (found in production data, 2026-08-28) ----------------------

def test_a_monthly_budget_is_compared_annually():
    """Business Development Executive is stored as 40000-60000, which is per
    month; every other open role is annual. A candidate expecting 660000 a year
    was escalated as over budget against 60000, and is in fact under it."""
    ok, issues = ra.screening_fit({"expected_salary": 660000}, {"budget_max": 60000})
    assert ok, issues


def test_a_monthly_answer_against_a_monthly_budget_also_fits():
    """One candidate countered at 42000 for that role, meaning per month."""
    ok, _ = ra.screening_fit({"expected_salary": 42000}, {"budget_max": 60000})
    assert ok


def test_a_genuine_overshoot_is_still_caught():
    ok, issues = ra.screening_fit({"expected_salary": 1400000}, {"budget_max": 1200000})
    assert not ok and "above budget" in issues[0]


def test_the_issue_text_keeps_the_figures_hr_typed():
    _, issues = ra.screening_fit({"expected_salary": 1400000}, {"budget_max": 1200000})
    assert "1400000.0 > 1200000.0" in issues[0], "HR must see the numbers they entered"


def test_annualised_amount():
    assert ra.annualised_amount(60000) == 720000      # monthly
    assert ra.annualised_amount(660000) == 660000     # already annual
    assert ra.annualised_amount(None) is None
    assert ra.annualised_amount(0) is None


def test_an_implausible_gap_never_closes_an_application_by_itself():
    """11x is a unit or data-entry problem, not a candidate asking too much.
    Closing those automatically would have rejected every BDE applicant."""
    assert ra.BUDGET_IMPLAUSIBLE_GAP_RATIO > ra.BUDGET_GAP_MAX_RATIO
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    assert "BUDGET_GAP_MAX_RATIO < gap_ratio <= BUDGET_IMPLAUSIBLE_GAP_RATIO" in source


# --- two crashes found in the production event log ----------------------------

def test_event_details_survive_a_decimal():
    """Every NUMERIC column comes back as Decimal. log_email_event serialised
    details with a bare json.dumps, so the budget_disclosed event - which logs
    budget_max - raised and took the disclosure down with it. There are zero
    budget_disclosed rows on the server and four 'Object of type Decimal is not
    JSON serializable' failures."""
    import json
    from decimal import Decimal
    payload = {"application_id": 1, "budget_max": Decimal("60000.00"),
               "candidate_expected_salary": Decimal("660000.00")}
    json.dumps(payload, default=str)          # must not raise
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    assert "json.dumps(details, default=str)" in source


def test_the_follow_up_path_has_its_locals_bound():
    """8 emails died on 'cannot access local variable cv_role_summary'. It is
    read on the follow-up path and only assigned in the attachment loop."""
    source = Path(__file__).resolve().parent.parent.joinpath("recruiter_agent.py").read_text()
    body = source[source.index("    def process_email(self, inbox_email: InboxEmail) -> bool:"):]
    body = body[: body.index("\n    def ", 10)]
    first_use = body.index("cv_role_summary")
    assert 'cv_role_summary: dict[str, Any] = {}' in body[:first_use + 40], \
        "cv_role_summary must be bound before any path can read it"


def test_the_role_helpers_tolerate_an_empty_summary():
    reqs = [{"id": 1, "position_title": "US Bookkeeper", "job_description": "x"}]
    assert ra.single_family_requirement(reqs, {}, {}, "") is None
    assert ra.near_miss_requirements(reqs, {}, {}, "") == []
    assert ra.candidate_role_families({}, {}, "") == set()
