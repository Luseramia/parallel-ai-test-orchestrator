"""End-to-end runs of the whole pipeline against a live gateway.

Everything here is real except the Codex phases: a live HTTP server, a real Git
repository, the production test-runner process, real patch application, real
artifact upload over the internal API and real callbacks. Codex is represented
by the artifacts and callbacks it would produce, which is exactly the Stage 1
rollout posture ("local/staging with fake Codex") from the plan.

The runner subprocess is started with the environment the Kubernetes Job
template defines, so the credential separation is exercised rather than assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from sqlalchemy import create_engine

from app.config import Settings
from app.main import create_app
from app.repositories.models import Base
from app.services.git_verifier import FakeGitVerifier
from app.services.job_launcher import FakeKubernetesJobLauncher
from app.services.notification_service import FakeNotificationSender

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_RUNNER = REPOSITORY_ROOT / "test-runner" / "runner.py"
API_TOKEN = "api-token-for-end-to-end"
CODEX_TOKEN = "codex-callback-token-for-end-to-end"
TEST_RUNNER_TOKEN = "test-callback-token-for-end-to-end"
REPOSITORY = "local/e2e-target"
BRANCH = "main"
PASSING_TEST = (
    "import os\n"
    "import unittest\n"
    "\n"
    "\n"
    "class ValueTests(unittest.TestCase):\n"
    "    def test_value(self):\n"
    "        self.assertEqual(1, __import__('value').VALUE)\n"
    "\n"
    "    def test_runner_has_no_model_credential(self):\n"
    "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n"
)
FAILING_TEST = (
    "import unittest\n"
    "\n"
    "\n"
    "class ValueTests(unittest.TestCase):\n"
    "    def test_value(self):\n"
    "        self.assertEqual(99, __import__('value').VALUE)\n"
)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "--initial-branch", BRANCH, str(self.source), cwd=self.root)
        self._git("-C", str(self.source), "config", "user.email", "e2e@example.test")
        self._git("-C", str(self.source), "config", "user.name", "End To End")
        (self.source / "value.py").write_text("VALUE = 0\n", encoding="utf-8")
        (self.source / "tests").mkdir()
        (self.source / "tests" / "__init__.py").write_text("", encoding="utf-8")
        self._git("-C", str(self.source), "add", ".")
        self._git("-C", str(self.source), "commit", "-m", "baseline")
        self.base_sha = self._git(
            "-C", str(self.source), "rev-parse", "HEAD", capture=True
        ).strip()

        self.policy_path = self.root / "repository-policies.yaml"
        self.policy_path.write_text(self._policy_document(), encoding="utf-8")

        database_path = (self.root / "end-to-end.db").as_posix()
        self.engine = create_engine(f"sqlite+pysqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.launcher = FakeKubernetesJobLauncher()
        self.git_verifier = FakeGitVerifier()
        self.notification_sender = FakeNotificationSender()
        self.settings = Settings(
            database_url=f"sqlite+pysqlite:///{database_path}",
            api_token=API_TOKEN,
            runner_token="",
            artifact_signing_key="artifact-signing-key-for-end-to-end",
            artifact_root=self.root / "artifacts",
            repository_policy_path=self.policy_path,
            allow_http_callbacks=True,
            callback_allowed_hosts=("n8n.example.test",),
            codex_runner_token=CODEX_TOKEN,
            test_runner_token=TEST_RUNNER_TOKEN,
        )
        self.app = create_app(
            self.settings,
            engine=self.engine,
            launcher=self.launcher,
            git_verifier=self.git_verifier,
            notification_sender=self.notification_sender,
        )
        self.base_url = self._serve(self.app)
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def tearDown(self) -> None:
        self.client.close()
        self.server.should_exit = True
        self.server_thread.join(timeout=10)
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_happy_path_from_plan_to_passed_notification(self) -> None:
        job_id = self._prepare()
        self._codex_prepare(job_id)
        self.assertEqual("WAITING_FOR_CODE", self._status(job_id)["status"])

        code_sha = self._implementation_commit()
        self._verify(job_id, code_sha)
        patch = self._test_patch("tests/test_value.py", PASSING_TEST)
        self._codex_generate(job_id, code_sha, patch)

        launch = self._await_test_launch()
        self.assertEqual(code_sha, launch.code_sha)
        environment = self._runner_environment(launch)
        self.assertNotIn("OPENAI_API_KEY", environment)

        completed = self._run_test_runner(environment)
        self.assertEqual(0, completed.returncode, completed.stdout)

        status = self._status(job_id)
        self.assertEqual("PASSED", status["status"])
        self.assertEqual(code_sha, status["tested_sha"])
        self.assertIsNone(status["failure_class"])
        stored_types = {artifact["type"] for artifact in status["artifacts"]}
        self.assertEqual(
            {"PLAN", "DRAFT", "PATCH", "RAW_LOG", "JUNIT", "RESULT"}, stored_types
        )

        result = self._read_artifact(status, "RESULT")
        self.assertEqual("PASSED", result["status"])
        self.assertEqual(code_sha, result["tested_sha"])
        self.assertEqual(hashlib.sha256(patch).hexdigest(), result["patch_sha256"])
        self.assertEqual(0, result["commands"][0]["exit_code"])
        self.assertEqual(2, result["summary"]["passed"])

        self.assertTrue(self.notification_sender.wait_for_calls(1))
        destination, payload = self.notification_sender.calls[0]
        self.assertEqual("http://n8n.example.test/webhook/completed", destination)
        self.assertEqual("PASSED", payload["status"])
        self.assertEqual(code_sha, payload["tested_sha"])
        self.assertIn(code_sha, payload["message"])

        # A late delivery that reuses the runner's event ID with different
        # content is refused outright rather than overwriting the result.
        replayed = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._terminal_event_of(job_id, code_sha, "PASSED"),
            headers={"Authorization": f"Bearer {TEST_RUNNER_TOKEN}"},
        )
        self.assertEqual(409, replayed.status_code, replayed.text)
        time.sleep(0.1)
        self.assertEqual("PASSED", self._status(job_id)["status"])
        self.assertEqual(1, len(self.notification_sender.calls))

    def test_failing_assertion_reports_failed_with_evidence(self) -> None:
        job_id = self._prepare(task_id="TASK-FAIL")
        self._codex_prepare(job_id)
        code_sha = self._implementation_commit()
        self._verify(job_id, code_sha)
        patch = self._test_patch("tests/test_value.py", FAILING_TEST)
        self._codex_generate(job_id, code_sha, patch)

        completed = self._run_test_runner(
            self._runner_environment(self._await_test_launch())
        )
        self.assertEqual(1, completed.returncode, completed.stdout)

        status = self._status(job_id)
        self.assertEqual("FAILED", status["status"])
        self.assertEqual(code_sha, status["tested_sha"])
        self.assertEqual("TEST_FAILURE", status["failure_class"])

        result = self._read_artifact(status, "RESULT")
        self.assertEqual("FAILED", result["status"])
        self.assertEqual("TEST_ASSERTION_FAILED", result["failures"][0]["class"])
        self.assertNotEqual(0, result["commands"][0]["exit_code"])
        self.assertEqual(1, result["summary"]["failed"])
        log = self._read_artifact(status, "RAW_LOG", as_json=False).decode()
        self.assertIn("test_value", log)

        self.assertTrue(self.notification_sender.wait_for_calls(1))
        _, payload = self.notification_sender.calls[0]
        self.assertEqual("FAILED", payload["status"])
        self.assertEqual(code_sha, payload["tested_sha"])

    def test_callbacks_accept_either_workload_token_and_nothing_else(self) -> None:
        job_id = self._prepare(task_id="TASK-TOKENS")
        event = self._event(f"evt_{job_id[4:]}_preparing", "PREPARE", "PREPARING")

        for token in ("not-a-token", API_TOKEN):
            rejected = self.client.post(
                f"/internal/v1/test-jobs/{job_id}/events",
                json=event,
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(401, rejected.status_code, rejected.text)

        # Either workload credential is accepted on the callback endpoint, which
        # is what lets one be rotated without stopping the other.
        for token in (CODEX_TOKEN, TEST_RUNNER_TOKEN):
            accepted = self.client.post(
                f"/internal/v1/test-jobs/{job_id}/events",
                json=event,
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual("PREPARING", self._status(job_id)["status"])

    def test_patch_policy_violation_blocks_before_any_test_runs(self) -> None:
        job_id = self._prepare(task_id="TASK-BLOCKED")
        self._codex_prepare(job_id)
        code_sha = self._implementation_commit()
        self._verify(job_id, code_sha)

        self._runner_event(
            job_id,
            self._event(
                f"evt_{job_id[4:]}_generating",
                "GENERATE",
                "GENERATING_TESTS",
                code_sha=code_sha,
            ),
            token=CODEX_TOKEN,
        )
        blocked_event = self._event(
            f"evt_{job_id[4:]}_blocked",
            "GENERATE",
            "BLOCKED",
            code_sha=code_sha,
            summary={
                "reason_code": "PATCH_POLICY_VIOLATION",
                "denied_paths": ["value.py"],
            },
        )
        self._runner_event(job_id, blocked_event, token=CODEX_TOKEN)

        status = self._status(job_id)
        self.assertEqual("BLOCKED", status["status"])
        self.assertEqual("POLICY", status["failure_class"])
        self.assertEqual([], self.launcher.test_launches)
        self.assertTrue(self.notification_sender.wait_for_calls(1))
        self.assertEqual("BLOCKED", self.notification_sender.calls[0][1]["status"])

        # The same delivery arriving twice is a no-op, not a second comment.
        duplicate = self._runner_event(job_id, blocked_event, token=CODEX_TOKEN)
        self.assertTrue(duplicate["duplicate"])
        time.sleep(0.1)
        self.assertEqual("BLOCKED", self._status(job_id)["status"])
        self.assertEqual(1, len(self.notification_sender.calls))

    def test_newer_commit_supersedes_a_result_that_is_still_running(self) -> None:
        job_id = self._prepare(task_id="TASK-SUPERSEDE")
        self._codex_prepare(job_id)
        first_sha = self._implementation_commit()
        self._verify(job_id, first_sha)
        patch = self._test_patch("tests/test_value.py", PASSING_TEST)
        self._codex_generate(job_id, first_sha, patch)
        self._await_test_launch()
        self._runner_event(
            job_id,
            self._event(
                f"evt_{job_id[4:]}_testing", "TEST", "TESTING", code_sha=first_sha
            ),
        )

        second_sha = self._implementation_commit(value=2)
        self._verify(job_id, second_sha, delivery="github-delivery-2", attempt=2)

        self.assertTrue(self.notification_sender.wait_for_calls(1))
        _, payload = self.notification_sender.calls[0]
        self.assertEqual("SUPERSEDED", payload["status"])
        self.assertEqual(first_sha, payload["code_sha"])
        self.assertEqual("VERIFY_QUEUED", self._status(job_id)["status"])

        # The runner of the old SHA reports back late; its result is refused.
        stale = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._event(
                f"evt_{job_id[4:]}_stale_passed",
                "TEST",
                "PASSED",
                code_sha=first_sha,
            ),
            headers={"Authorization": f"Bearer {TEST_RUNNER_TOKEN}"},
        )
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual("VERIFY_QUEUED", self._status(job_id)["status"])

    # ---- pipeline steps ------------------------------------------------

    def _prepare(self, task_id: str = "TASK-E2E") -> str:
        plan = self._plan(task_id)
        canonical = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        response = self.client.post(
            "/api/v1/test-jobs/prepare",
            json={
                "task_id": task_id,
                "repository": REPOSITORY,
                "branch": BRANCH,
                "base_sha": self.base_sha,
                "plan": plan,
                "callback_url": "http://n8n.example.test/webhook/completed",
            },
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Idempotency-Key": f"{task_id}:{self.base_sha}:{digest}",
            },
        )
        self.assertEqual(202, response.status_code, response.text)
        self.assertTrue(self.launcher.wait_for_launches(1))
        return response.json()["job_id"]

    def _codex_prepare(self, job_id: str) -> None:
        self._runner_event(
            job_id,
            self._event(f"evt_{job_id[4:]}_preparing", "PREPARE", "PREPARING"),
            token=CODEX_TOKEN,
        )
        draft = json.dumps({"schema_version": "1.0", "test_cases": []}).encode()
        artifact = self._upload(
            job_id, f"jobs/{job_id}/attempts/1/test-draft.json", draft
        )
        self._runner_event(
            job_id,
            self._event(
                f"evt_{job_id[4:]}_prepared",
                "PREPARE",
                "WAITING_FOR_CODE",
                artifacts=[
                    {**artifact, "type": "DRAFT", "media_type": "application/json"}
                ],
            ),
            token=CODEX_TOKEN,
        )

    def _verify(
        self,
        job_id: str,
        code_sha: str,
        *,
        delivery: str = "github-delivery-1",
        attempt: int = 1,
    ) -> None:
        self.git_verifier.allow(REPOSITORY, BRANCH, code_sha)
        response = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json={
                "repository": REPOSITORY,
                "branch": BRANCH,
                "code_sha": code_sha,
                "push_event_id": delivery,
            },
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual(attempt, response.json()["attempt"])
        self.assertTrue(self.launcher.wait_for_generate_launches(attempt))

    def _codex_generate(self, job_id: str, code_sha: str, patch: bytes) -> None:
        self._runner_event(
            job_id,
            self._event(
                f"evt_{job_id[4:]}_generating",
                "GENERATE",
                "GENERATING_TESTS",
                code_sha=code_sha,
            ),
            token=CODEX_TOKEN,
        )
        patch_artifact = self._upload(
            job_id, f"jobs/{job_id}/attempts/1/tests.patch", patch
        )
        self._runner_event(
            job_id,
            self._event(
                f"evt_{job_id[4:]}_test_queued",
                "GENERATE",
                "TEST_QUEUED",
                code_sha=code_sha,
                artifacts=[
                    {**patch_artifact, "type": "PATCH", "media_type": "text/x-diff"}
                ],
            ),
            token=CODEX_TOKEN,
        )

    def _await_test_launch(self):
        self.assertTrue(self.launcher.wait_for_test_launches(1))
        return self.launcher.test_launches[-1]

    def _runner_environment(self, launch) -> dict[str, str]:
        """Mirror the environment of k8s/templates/test-runner-job.yaml.

        The only secret is the test callback token; the model credential has no
        entry to inherit, which is the property the job template encodes.
        """

        artifact_root = self.root / "runner-artifacts"
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME")
            if key in os.environ
        }
        environment.update(
            {
                "JOB_ID": launch.job_id,
                "TASK_ID": launch.task_id,
                "GIT_CLONE_URL": launch.clone_url,
                "BASE_SHA": launch.base_sha,
                "CODE_SHA": launch.code_sha,
                "ATTEMPT": str(launch.attempt),
                "ARTIFACT_ROOT": str(artifact_root),
                "PATCH_PATH": str(artifact_root / "input" / "tests.patch"),
                "PATCH_OBJECT_KEY": launch.patch_object_key,
                "PATCH_SHA256": launch.patch_sha256,
                "GATEWAY_URL": self.base_url,
                "ARTIFACT_GATEWAY_URL": (
                    f"{self.base_url}/internal/v1/test-jobs/{launch.job_id}/artifacts"
                ),
                "RUNNER_TOKEN": TEST_RUNNER_TOKEN,
                "RUNNER_POLICY_JSON": json.dumps(
                    launch.runner_policy, sort_keys=True, separators=(",", ":")
                ),
            }
        )
        return environment

    def _run_test_runner(
        self, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TEST_RUNNER)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    # ---- helpers -------------------------------------------------------

    def _serve(self, app) -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, log_level="warning")
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None
        self.server_thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self.server_thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise AssertionError("gateway did not start")
            time.sleep(0.05)
        return f"http://127.0.0.1:{port}"

    def _implementation_commit(self, value: int = 1) -> str:
        (self.source / "value.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        self._git("-C", str(self.source), "add", ".")
        self._git("-C", str(self.source), "commit", "-m", f"set value to {value}")
        return self._git(
            "-C", str(self.source), "rev-parse", "HEAD", capture=True
        ).strip()

    def _test_patch(self, relative_path: str, content: str) -> bytes:
        path = self.source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git("-C", str(self.source), "add", "-N", "--", relative_path)
        patch = subprocess.run(
            ["git", "-C", str(self.source), "diff", "--full-index"],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self._git("-C", str(self.source), "reset", "--", relative_path)
        path.unlink()
        return patch

    def _upload(self, job_id: str, object_key: str, content: bytes) -> dict[str, Any]:
        response = self.client.put(
            f"/internal/v1/test-jobs/{job_id}/artifacts/{object_key}",
            content=content,
            headers={"Authorization": f"Bearer {CODEX_TOKEN}"},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _runner_event(
        self, job_id: str, event: dict[str, Any], token: str = TEST_RUNNER_TOKEN
    ) -> dict[str, Any]:
        response = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=event,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _status(self, job_id: str) -> dict[str, Any]:
        response = self.client.get(
            f"/api/v1/test-jobs/{job_id}",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _read_artifact(
        self, status: dict[str, Any], artifact_type: str, as_json: bool = True
    ) -> Any:
        artifact = next(
            item for item in status["artifacts"] if item["type"] == artifact_type
        )
        response = self.client.get(artifact["url"])
        self.assertEqual(200, response.status_code, response.text)
        return response.json() if as_json else response.content

    def _terminal_event_of(
        self, job_id: str, code_sha: str, status: str
    ) -> dict[str, Any]:
        """Rebuild the event ID the test runner uses for its terminal callback."""

        return self._event(
            f"evt_{job_id[4:]}_test_1_{status.lower()}",
            "TEST",
            status,
            code_sha=code_sha,
            summary={"result": "replayed with different content"},
        )

    @staticmethod
    def _event(
        event_id: str,
        phase: str,
        status: str,
        *,
        code_sha: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        summary: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "event_id": event_id,
            "attempt": attempt,
            "phase": phase,
            "status": status,
            "code_sha": code_sha,
            "artifacts": artifacts or [],
            "summary": summary or {},
            "occurred_at": "2026-09-19T12:00:00Z",
        }

    def _plan(self, task_id: str) -> dict[str, Any]:
        plan = yaml.safe_load(
            (REPOSITORY_ROOT / "docs" / "test-plans" / "example.yaml").read_text(
                encoding="utf-8"
            )
        )
        plan.update(
            task_id=task_id,
            repository=REPOSITORY,
            branch=BRANCH,
            base_sha=self.base_sha,
        )
        plan["scope"]["production_paths"] = ["value.py"]
        plan["scope"]["allowed_test_paths"] = ["tests/**/*.py"]
        plan["commands"] = {"unit": "python-unittest-all"}
        return plan

    def _policy_document(self) -> str:
        return yaml.safe_dump(
            {
                "version": 1,
                "repositories": [
                    {
                        "id": REPOSITORY,
                        "provider": "github",
                        "clone_url": str(self.source),
                        "allowed_branches": [BRANCH],
                        "patch": {
                            "allowed_paths": ["tests/**/*.py"],
                            "denied_paths": ["value.py", ".github/**"],
                            "allow_new_files": True,
                            "allow_deletes": False,
                            "allow_renames": False,
                            "allow_binary": False,
                            "allow_symlinks": False,
                            "allow_submodules": False,
                            "allow_mode_changes": False,
                            "max_files": 10,
                            "max_bytes": 65536,
                        },
                        "commands": {"unit": "python-unittest-all"},
                        "command_argv": {
                            # A server-owned argv array, exactly as a real
                            # policy stores it. It writes JUnit XML so the
                            # run exercises report parsing and upload too.
                            "python-unittest-all": [
                                sys.executable,
                                "-m",
                                "pytest",
                                "tests",
                                "-q",
                                "--junitxml=reports/junit/results.xml",
                            ]
                        },
                        "limits": {
                            "unit_timeout_seconds": 120,
                            "total_timeout_seconds": 300,
                            "max_log_bytes": 1_000_000,
                        },
                        "artifacts": {
                            "junit_globs": ["reports/junit/*.xml"],
                            "coverage_globs": [],
                        },
                    }
                ],
            },
            sort_keys=False,
        )

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
