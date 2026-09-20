from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

TEST_RUNNER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TEST_RUNNER_ROOT.parent
sys.path.insert(0, str(TEST_RUNNER_ROOT))

from runner import (
    RunnerConfiguration,
    RunnerError,
    execute_commands,
    parse_coverage,
    parse_junit,
    run,
    sanitized_test_environment,
)


class TestRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", str(self.source), cwd=self.root)
        self._git("-C", str(self.source), "config", "user.email", "tests@example.test")
        self._git("-C", str(self.source), "config", "user.name", "Runner Tests")
        (self.source / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        self._git("-C", str(self.source), "add", ".")
        self._git("-C", str(self.source), "commit", "-m", "implementation")
        self.code_sha = self._git(
            "-C", str(self.source), "rev-parse", "HEAD", capture=True
        ).strip()
        self.patch_path = self.root / "tests.patch"
        self.artifact_root = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_happy_path_runs_with_sanitized_environment_and_valid_result(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py",
            "import os\nimport unittest\n\n"
            "class ValueTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(1, __import__('value').VALUE)\n"
            "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n",
        )
        os.environ["OPENAI_API_KEY"] = "must-not-reach-tests"
        try:
            result = run(self._configuration(patch))
        finally:
            del os.environ["OPENAI_API_KEY"]

        self.assertEqual("PASSED", result["status"])
        self.assertEqual(0, result["commands"][0]["exit_code"])
        validator = Draft202012Validator(
            json.loads(
                (REPOSITORY_ROOT / "contracts" / "test-result.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
            format_checker=FormatChecker(),
        )
        self.assertEqual([], list(validator.iter_errors(result)))

    def test_failed_assertion_is_classified(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py",
            "import unittest\n\n"
            "class ValueTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(2, __import__('value').VALUE)\n",
        )
        result = run(self._configuration(patch))
        self.assertEqual("FAILED", result["status"])
        self.assertEqual("TEST_ASSERTION_FAILED", result["failures"][0]["class"])

    def test_command_timeout_is_classified(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py", "def test_placeholder():\n    pass\n"
        )
        configuration = self._configuration(patch)
        configuration.command_argv["python-unittest-all"] = [
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ]
        configuration.command_timeouts["unit"] = 1
        result = run(configuration)
        self.assertEqual("TIMEOUT", result["status"])
        self.assertEqual("COMMAND_TIMEOUT", result["failures"][0]["class"])

    def test_install_failure_preserves_exit_code_and_classification(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py", "def test_placeholder():\n    pass\n"
        )
        configuration = self._configuration(patch)
        configuration.command_slots.clear()
        configuration.command_slots.update(
            {"install": "failing-install", "unit": "python-unittest-all"}
        )
        configuration.command_argv["failing-install"] = [
            sys.executable,
            "-c",
            "raise SystemExit(17)",
        ]
        result = run(configuration)
        self.assertEqual("FAILED", result["status"])
        self.assertEqual(17, result["commands"][0]["exit_code"])
        self.assertEqual(
            "DEPENDENCY_INSTALL_FAILED", result["failures"][0]["class"]
        )

    def test_build_failure_is_distinct_from_test_failure(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py", "def test_placeholder():\n    pass\n"
        )
        configuration = self._configuration(patch)
        configuration.command_slots.clear()
        configuration.command_slots["build"] = "failing-build"
        configuration.command_argv["failing-build"] = [
            sys.executable,
            "-c",
            "raise SystemExit(2)",
        ]
        result = run(configuration)
        self.assertEqual("BUILD_FAILED", result["failures"][0]["class"])
        self.assertEqual(2, result["commands"][0]["exit_code"])

    def test_compile_failure_is_classified_from_test_output(self) -> None:
        patch = self._create_patch(
            "tests/test_value.py", "this is not valid python\n"
        )
        result = run(self._configuration(patch))
        self.assertEqual("TEST_COMPILE_FAILED", result["failures"][0]["class"])

    def test_shell_string_command_is_rejected(self) -> None:
        configuration = self._configuration(b"unused")
        configuration.command_argv["python-unittest-all"] = "echo unsafe"  # type: ignore[assignment]
        with self.assertRaises(RunnerError) as raised:
            execute_commands(configuration, self.source)
        self.assertEqual("POLICY_VIOLATION", raised.exception.failure_class)

    def test_sanitized_environment_removes_credentials(self) -> None:
        clean = sanitized_test_environment(
            {
                "PATH": "safe-path",
                "OPENAI_API_KEY": "secret",
                "RUNNER_TOKEN": "secret",
                "GITHUB_TOKEN": "secret",
            }
        )
        self.assertEqual("safe-path", clean["PATH"])
        self.assertNotIn("OPENAI_API_KEY", clean)
        self.assertNotIn("RUNNER_TOKEN", clean)
        self.assertNotIn("GITHUB_TOKEN", clean)

    def test_junit_parser_counts_pass_fail_and_skip(self) -> None:
        report = self.root / "reports" / "junit.xml"
        report.parent.mkdir(parents=True)
        report.write_text(
            '<testsuite tests="5" failures="1" errors="1" skipped="1"></testsuite>',
            encoding="utf-8",
        )
        self.assertEqual(
            {"passed": 2, "failed": 2, "skipped": 1},
            parse_junit(self.root, ["reports/*.xml"]),
        )

    def test_coverage_parser_supports_istanbul_summary(self) -> None:
        report = self.root / "reports" / "coverage-summary.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "total": {
                        "lines": {"pct": 82.1},
                        "branches": {"pct": 74.3},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            {"lines": 82.1, "branches": 74.3},
            parse_coverage(self.root, ["reports/coverage-summary.json"]),
        )

    def _configuration(self, patch: bytes) -> RunnerConfiguration:
        self.patch_path.write_bytes(patch)
        return RunnerConfiguration(
            job_id="atj_" + "1" * 32,
            task_id="TASK-123",
            clone_url=str(self.source),
            base_sha=self.code_sha,
            code_sha=self.code_sha,
            patch_path=self.patch_path,
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            artifact_root=self.artifact_root,
            gateway_url="https://gateway.example.test",
            runner_token="runner-token",
            attempt=1,
            command_slots={"unit": "python-unittest-all"},
            command_argv={
                "python-unittest-all": [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ]
            },
            command_timeouts={"unit": 10},
            allowed_paths=["tests/**/*.py"],
            denied_paths=["app/**", ".github/**"],
            junit_globs=[],
            coverage_globs=[],
            allow_skipped_required_tests=False,
            max_log_bytes=1_000_000,
        )

    def _create_patch(self, relative_path: str, content: str) -> bytes:
        path = self.source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git("-C", str(self.source), "add", "-N", "--", relative_path)
        patch = subprocess.run(
            ["git", "-C", str(self.source), "diff", "--full-index", "--binary"],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self._git("-C", str(self.source), "reset", "--", relative_path)
        path.unlink()
        return patch

    @staticmethod
    def _git(*arguments: str, cwd: Path | None = None, capture: bool = False) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout if capture else ""


if __name__ == "__main__":
    unittest.main()
