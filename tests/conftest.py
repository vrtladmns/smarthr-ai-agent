"""Safety net: tests must never be able to send real email.

The interview API calls notify_post_interview_outcome_async() when a report is
written, which mails HR at RECRUITER_HR_ESCALATION_EMAIL. Running the suite
therefore delivered real messages to the live career mailbox. Nothing in here is
optional - it is applied to every test session before any test imports run.
"""

import os
import sys
from pathlib import Path

# Belt: the mailers themselves refuse to send when this is false.
os.environ["RECRUITER_REPLY_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import recruiter_agent as ra


@pytest.fixture(autouse=True, scope="session")
def _never_send_email():
    """Braces: neuter every outbound path, whatever the config says."""

    def blocked(*args, **kwargs):
        raise AssertionError(
            "A test tried to send email. Outbound mail is blocked in tests; "
            "stub the call site instead."
        )

    ra.RecruiterMailer.send_direct_email = blocked
    ra.RecruiterMailer.send_reply = blocked
    ra.MicrosoftGraphProvider.send_direct_email = blocked
    ra.MicrosoftGraphProvider.send_reply = blocked
    ra.send_hr_notification = lambda *a, **k: None
    ra.notify_post_interview_outcome = lambda *a, **k: None

    import recruiter_dashboard as rd

    rd.notify_post_interview_outcome_async = lambda *a, **k: None
    yield
