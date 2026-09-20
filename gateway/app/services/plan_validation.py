from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.services.repository_policy import (
    PolicyValidationError,
    RepositoryPolicy,
    glob_patterns_overlap,
)


class PlanValidationError(ValueError):
    pass


class TestPlanValidator:
    def __init__(self, schema_path: Path) -> None:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(
        self,
        plan: dict[str, Any],
        *,
        task_id: str,
        repository: str,
        branch: str,
        base_sha: str,
        policy: RepositoryPolicy,
    ) -> tuple[bytes, str]:
        errors = sorted(
            self._validator.iter_errors(plan), key=lambda error: list(error.path)
        )
        if errors:
            path = ".".join(str(part) for part in errors[0].path) or "$"
            raise PlanValidationError(
                f"invalid test plan at {path}: {errors[0].message}"
            )
        for field, expected in {
            "task_id": task_id,
            "repository": repository,
            "branch": branch,
            "base_sha": base_sha,
        }.items():
            if plan[field] != expected:
                raise PlanValidationError(
                    f"request {field} does not match the test plan"
                )
        if not policy.allows_branch(branch):
            raise PlanValidationError("branch is not allowlisted for this repository")

        commands = plan["commands"]
        for slot, command_id in commands.items():
            if policy.command_slots.get(slot) != command_id:
                raise PlanValidationError(
                    f"command ID for {slot!r} is not allowlisted by repository policy"
                )

        allowed_paths = plan["scope"]["allowed_test_paths"]
        unsupported = set(allowed_paths) - set(policy.patch.allowed_paths)
        if unsupported:
            raise PlanValidationError(
                "test plan requests paths outside repository policy: "
                + ", ".join(sorted(unsupported))
            )
        for test_pattern in allowed_paths:
            for production_pattern in plan["scope"]["production_paths"]:
                if glob_patterns_overlap(test_pattern, production_pattern):
                    raise PlanValidationError(
                        "allowed test paths overlap protected production paths"
                    )

        acceptance_ids = [item["id"] for item in plan["acceptance_criteria"]]
        if len(acceptance_ids) != len(set(acceptance_ids)):
            raise PlanValidationError("acceptance criteria IDs must be unique")
        acceptance_set = set(acceptance_ids)
        test_case_ids: set[str] = set()
        for test_case in plan["test_cases"]:
            if test_case["id"] in test_case_ids:
                raise PlanValidationError("test case IDs must be unique")
            test_case_ids.add(test_case["id"])
            unknown = set(test_case["acceptance_criteria"]) - acceptance_set
            if unknown:
                raise PlanValidationError(
                    "test case references unknown acceptance criteria: "
                    + ", ".join(sorted(unknown))
                )

        canonical = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return canonical, hashlib.sha256(canonical).hexdigest()


def expected_prepare_idempotency_key(task_id: str, base_sha: str, digest: str) -> str:
    return f"{task_id}:{base_sha}:{digest}"


def validate_repository_policy(policy: RepositoryPolicy) -> None:
    if not policy.allowed_branches:
        raise PolicyValidationError("repository must allow at least one branch")
