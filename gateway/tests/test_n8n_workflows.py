from __future__ import annotations

import json
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / "n8n" / "workflows"


class N8nWorkflowExportTests(unittest.TestCase):
    def test_expected_workflows_are_valid_inactive_exports(self) -> None:
        expected = {
            "test-plan-received.json": "test-plan-received",
            "git-push-received.json": "git-push-received",
            "test-job-completed.json": "test-job-completed",
        }
        for filename, name in expected.items():
            with self.subTest(filename=filename):
                workflow = json.loads((WORKFLOW_ROOT / filename).read_text("utf-8"))
                self.assertEqual(name, workflow["name"])
                self.assertFalse(workflow["active"])
                self.assertGreaterEqual(len(workflow["nodes"]), 4)
                self.assertIn("connections", workflow)
                self.assertNotIn("credentials", json.dumps(workflow).lower())

    def test_completion_export_carries_tested_sha_and_idempotency_key(self) -> None:
        raw = (WORKFLOW_ROOT / "test-job-completed.json").read_text("utf-8")
        self.assertIn("tested_sha", raw)
        self.assertIn("Idempotency-Key", raw)
        self.assertIn("Fetch Authoritative Result", raw)

    def test_inbound_exports_check_signatures(self) -> None:
        for filename in (
            "test-plan-received.json",
            "git-push-received.json",
            "test-job-completed.json",
        ):
            with self.subTest(filename=filename):
                raw = (WORKFLOW_ROOT / filename).read_text("utf-8")
                self.assertIn("createHmac", raw)
                self.assertIn("timingSafeEqual", raw)


if __name__ == "__main__":
    unittest.main()
