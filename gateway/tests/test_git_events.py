from __future__ import annotations

import unittest

from app.services.git_events import (
    CommitFooterError,
    parse_verification_footer,
    select_head_verification_footer,
)

JOB_ID = "atj_" + "1" * 32
HEAD_SHA = "a" * 40


class CommitFooterTests(unittest.TestCase):
    def test_parses_required_trailers_from_final_block(self) -> None:
        footer = parse_verification_footer(
            f"feat: implement behavior\n\nAI-Test-Job: {JOB_ID}\nAI-Test-Phase: verify"
        )
        self.assertEqual(JOB_ID, footer.job_id)
        self.assertEqual("verify", footer.phase)

    def test_rejects_duplicate_or_non_footer_values(self) -> None:
        invalid_messages = [
            f"feat: mention AI-Test-Job: {JOB_ID} in prose",
            f"feat: x\n\nAI-Test-Job: {JOB_ID}\nAI-Test-Job: {JOB_ID}\nAI-Test-Phase: verify",
            f"feat: x\n\nAI-Test-Job: {JOB_ID}\nAI-Test-Phase: prepare",
            "feat: x\n\nAI-Test-Job: ../../etc/passwd\nAI-Test-Phase: verify",
        ]
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(CommitFooterError):
                parse_verification_footer(message)

    def test_selects_only_the_exact_head_commit(self) -> None:
        footer = select_head_verification_footer(
            [
                {
                    "id": "b" * 40,
                    "message": f"old\n\nAI-Test-Job: {JOB_ID}\nAI-Test-Phase: verify",
                },
                {
                    "id": HEAD_SHA,
                    "message": f"head\n\nAI-Test-Job: {JOB_ID}\nAI-Test-Phase: verify",
                },
            ],
            HEAD_SHA,
        )
        self.assertEqual(JOB_ID, footer.job_id)

    def test_rejects_when_head_commit_is_absent(self) -> None:
        with self.assertRaises(CommitFooterError):
            select_head_verification_footer([], HEAD_SHA)


if __name__ == "__main__":
    unittest.main()
