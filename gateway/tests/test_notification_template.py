from __future__ import annotations

import unittest

from app.services.notification_template import render_completion_message


class NotificationTemplateTests(unittest.TestCase):
    def test_result_message_always_identifies_tested_sha_and_counts(self) -> None:
        message = render_completion_message(
            {
                "job_id": "atj_example",
                "status": "FAILED",
                "tested_sha": "a" * 40,
                "attempt": 2,
                "summary": {
                    "passed": 4,
                    "failed": 1,
                    "skipped": 0,
                    "duration_seconds": 1.25,
                },
                "status_url": "/api/v1/test-jobs/atj_example",
            }
        )
        self.assertIn(f"Tested SHA: `{'a' * 40}`", message)
        self.assertIn("4 passed, 1 failed, 0 skipped", message)
        self.assertIn("1.250s", message)

    def test_non_test_terminal_message_is_explicitly_not_executed(self) -> None:
        message = render_completion_message(
            {
                "job_id": "atj_blocked",
                "status": "BLOCKED",
                "tested_sha": None,
                "attempt": 1,
                "summary": {},
                "status_url": "/api/v1/test-jobs/atj_blocked",
            }
        )
        self.assertIn("Tested SHA: `not-executed`", message)


if __name__ == "__main__":
    unittest.main()
