from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import Settings
from app.main import create_app
from app.repositories.models import Base
from app.repositories.notification_repository import NotificationRepository
from app.services.git_verifier import FakeGitVerifier
from app.services.job_launcher import FakeKubernetesJobLauncher, PrepareLaunch
from app.services.notification_service import FakeNotificationSender

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
API_TOKEN = "api-token-for-tests"
RUNNER_TOKEN = "runner-token-for-tests"


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        database_path = (root / "api.db").as_posix()
        self.engine = create_engine(f"sqlite+pysqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.launcher = FakeKubernetesJobLauncher()
        self.git_verifier = FakeGitVerifier()
        self.notification_sender = FakeNotificationSender()
        self.settings = Settings(
            database_url=f"sqlite+pysqlite:///{database_path}",
            api_token=API_TOKEN,
            runner_token=RUNNER_TOKEN,
            artifact_signing_key="artifact-signing-key-for-tests",
            artifact_root=root / "artifacts",
            repository_policy_path=REPOSITORY_ROOT
            / "config"
            / "repository-policies.example.yaml",
            max_request_bytes=100_000,
            callback_allowed_hosts=("n8n.example.test",),
        )
        self.client_context = TestClient(
            create_app(
                self.settings,
                engine=self.engine,
                launcher=self.launcher,
                git_verifier=self.git_verifier,
                notification_sender=self.notification_sender,
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_prepare_returns_202_dispatches_once_and_duplicate_returns_same_job(
        self,
    ) -> None:
        request = self._prepare_request()
        headers = self._prepare_headers(request["plan"])

        started = time.monotonic()
        first = self.client.post(
            "/api/v1/test-jobs/prepare", json=request, headers=headers
        )
        elapsed = time.monotonic() - started
        duplicate = self.client.post(
            "/api/v1/test-jobs/prepare", json=request, headers=headers
        )

        self.assertEqual(202, first.status_code, first.text)
        self.assertLess(elapsed, 5)
        self.assertEqual(first.json()["job_id"], duplicate.json()["job_id"])
        self.assertTrue(self.launcher.wait_for_launches(1))
        time.sleep(0.05)
        self.assertEqual(1, len(self.launcher.launches))

        status_response = self.client.get(
            first.json()["status_url"], headers=self._api_headers()
        )
        self.assertEqual(200, status_response.status_code, status_response.text)
        status = status_response.json()
        self.assertEqual("PREPARE_QUEUED", status["status"])
        self.assertEqual(1, len(status["artifacts"]))
        self.assertEqual("PLAN", status["artifacts"][0]["type"])

        artifact_response = self.client.get(status["artifacts"][0]["url"])
        self.assertEqual(200, artifact_response.status_code)
        self.assertEqual(request["plan"], artifact_response.json())

    def test_prepare_does_not_wait_for_launcher_work(self) -> None:
        slow_launcher = SlowLauncher(delay_seconds=0.5)
        app = create_app(
            self.settings,
            engine=self.engine,
            launcher=slow_launcher,
            git_verifier=self.git_verifier,
        )
        request = self._prepare_request(task_id="TASK-SLOW")
        with TestClient(app) as client:
            started = time.monotonic()
            response = client.post(
                "/api/v1/test-jobs/prepare",
                json=request,
                headers=self._prepare_headers(request["plan"]),
            )
            elapsed = time.monotonic() - started
            self.assertEqual(202, response.status_code, response.text)
            self.assertLess(elapsed, 0.4)

    def test_authentication_and_token_separation(self) -> None:
        request = self._prepare_request()
        key = self._idempotency_key(request["plan"])
        missing = self.client.post(
            "/api/v1/test-jobs/prepare",
            json=request,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(401, missing.status_code)

        prepared = self._post_prepare(request)
        event = self._event("evt_token_separation", "PREPARING")
        wrong_identity = self.client.post(
            f"/internal/v1/test-jobs/{prepared['job_id']}/events",
            json=event,
            headers=self._api_headers(),
        )
        self.assertEqual(401, wrong_identity.status_code)

    def test_invalid_plan_and_idempotency_key_are_rejected(self) -> None:
        invalid_command = self._prepare_request()
        invalid_command["plan"]["commands"]["unit"] = "not-allowlisted"
        response = self.client.post(
            "/api/v1/test-jobs/prepare",
            json=invalid_command,
            headers={
                **self._api_headers(),
                "Idempotency-Key": self._idempotency_key(invalid_command["plan"]),
            },
        )
        self.assertEqual(422, response.status_code, response.text)

        valid = self._prepare_request(task_id="TASK-WRONG-KEY")
        wrong_key = self.client.post(
            "/api/v1/test-jobs/prepare",
            json=valid,
            headers={**self._api_headers(), "Idempotency-Key": "wrong"},
        )
        self.assertEqual(422, wrong_key.status_code)

    def test_request_size_limit_rejects_before_validation(self) -> None:
        response = self.client.post(
            "/api/v1/test-jobs/prepare",
            content=b"x" * 100_001,
            headers={**self._api_headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(413, response.status_code, response.text)

    def test_runner_event_transitions_and_duplicate_is_idempotent(self) -> None:
        prepared = self._post_prepare(self._prepare_request())
        job_id = prepared["job_id"]
        event = self._event("evt_prepare_started", "PREPARING")

        first = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=event,
            headers=self._runner_headers(),
        )
        duplicate = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=event,
            headers=self._runner_headers(),
        )

        self.assertEqual(200, first.status_code, first.text)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual("PREPARING", first.json()["status"])
        self.assertEqual(200, duplicate.status_code, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])

        completed = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._event("evt_prepare_completed", "WAITING_FOR_CODE"),
            headers=self._runner_headers(),
        )
        self.assertEqual(200, completed.status_code, completed.text)
        self.assertEqual("WAITING_FOR_CODE", completed.json()["status"])

    def test_readiness_checks_database(self) -> None:
        response = self.client.get("/health/ready")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ready"}, response.json())

    def test_cancel_deletes_only_the_recorded_job_and_is_idempotent(self) -> None:
        prepared = self._post_prepare(self._prepare_request(task_id="TASK-CANCEL"))
        job_id = prepared["job_id"]
        self.assertTrue(self.launcher.wait_for_launches(1))
        time.sleep(0.05)

        first = self.client.post(
            f"/api/v1/test-jobs/{job_id}/cancel", headers=self._api_headers()
        )
        duplicate = self.client.post(
            f"/api/v1/test-jobs/{job_id}/cancel", headers=self._api_headers()
        )

        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("CANCELLED", first.json()["status"])
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(200, duplicate.status_code, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(1, len(self.launcher.deleted_jobs))
        deleted_name, deleted_job_id = self.launcher.deleted_jobs[0]
        self.assertEqual(job_id, deleted_job_id)
        self.assertEqual(self.launcher.launches[0].job_id, deleted_job_id)
        self.assertIn(job_id[-20:], deleted_name)
        self.assertTrue(self.notification_sender.wait_for_calls(1))
        self.assertEqual("CANCELLED", self.notification_sender.calls[0][1]["status"])

    def test_verify_checks_reachability_and_is_idempotent(self) -> None:
        prepared = self._post_prepare(self._prepare_request())
        job_id = prepared["job_id"]
        self._advance_to_waiting(job_id)
        code_sha = "d" * 40
        self.git_verifier.allow(
            "github.com/Luseramia/ai-orchestrator", "main", code_sha
        )
        request = {
            "repository": "github.com/Luseramia/ai-orchestrator",
            "branch": "main",
            "code_sha": code_sha,
            "push_event_id": "github-delivery-1",
        }
        first = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json=request,
            headers=self._api_headers(),
        )
        duplicate = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json=request,
            headers=self._api_headers(),
        )

        self.assertEqual(202, first.status_code, first.text)
        self.assertEqual("VERIFY_QUEUED", first.json()["status"])
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(202, duplicate.status_code, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertTrue(self.launcher.wait_for_generate_launches(1))
        time.sleep(0.05)
        self.assertEqual(1, len(self.launcher.generate_launches))
        self.assertEqual(code_sha, self.launcher.generate_launches[0].code_sha)
        self.assertEqual(1, len(self.git_verifier.calls))

    def test_verify_rejects_unreachable_commit(self) -> None:
        request = self._prepare_request(task_id="TASK-UNREACHABLE")
        prepared = self._post_prepare(request)
        self._advance_to_waiting(prepared["job_id"])
        response = self.client.post(
            f"/api/v1/test-jobs/{prepared['job_id']}/verify",
            json={
                "repository": request["repository"],
                "branch": request["branch"],
                "code_sha": "e" * 40,
                "push_event_id": "github-delivery-unreachable",
            },
            headers=self._api_headers(),
        )
        self.assertEqual(422, response.status_code, response.text)

    def test_newer_sha_supersedes_active_attempt_and_rejects_stale_callback(
        self,
    ) -> None:
        request = self._prepare_request(task_id="TASK-SUPERSEDE")
        prepared = self._post_prepare(request)
        job_id = prepared["job_id"]
        self._advance_to_waiting(job_id)
        first_sha = "1" * 40
        second_sha = "2" * 40
        self.git_verifier.allow(request["repository"], request["branch"], first_sha)
        first = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json={
                "repository": request["repository"],
                "branch": request["branch"],
                "code_sha": first_sha,
                "push_event_id": "github-supersede-first",
            },
            headers=self._api_headers(),
        )
        self.assertEqual(202, first.status_code, first.text)
        self.assertTrue(self.launcher.wait_for_generate_launches(1))
        time.sleep(0.05)

        self.git_verifier.allow(request["repository"], request["branch"], second_sha)
        second = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json={
                "repository": request["repository"],
                "branch": request["branch"],
                "code_sha": second_sha,
                "push_event_id": "github-supersede-second",
            },
            headers=self._api_headers(),
        )
        self.assertEqual(202, second.status_code, second.text)
        self.assertEqual(2, second.json()["attempt"])
        self.assertEqual("VERIFY_QUEUED", second.json()["status"])
        self.assertTrue(self.launcher.wait_for_generate_launches(2))
        # Two dispatch workers append in whatever order they finish, so assert
        # on the set of launched SHAs rather than on arrival order.
        self.assertEqual(
            {first_sha, second_sha},
            {launch.code_sha for launch in self.launcher.generate_launches},
        )
        self.assertTrue(self.notification_sender.wait_for_calls(1))
        self.assertEqual("SUPERSEDED", self.notification_sender.calls[0][1]["status"])
        self.assertEqual(first_sha, self.notification_sender.calls[0][1]["code_sha"])

        stale = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                "evt_stale_superseded_callback",
                "GENERATE",
                "GENERATING_TESTS",
                first_sha,
                attempt=1,
            ),
            headers=self._runner_headers(),
        )
        current = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                "evt_current_supersede_callback",
                "GENERATE",
                "GENERATING_TESTS",
                second_sha,
                attempt=2,
            ),
            headers=self._runner_headers(),
        )
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual(200, current.status_code, current.text)

    def test_generate_callback_dispatches_credential_free_test_job_once(self) -> None:
        request = self._prepare_request(task_id="TASK-TEST-DISPATCH")
        prepared = self._post_prepare(request)
        job_id = prepared["job_id"]
        self._advance_to_waiting(job_id)
        code_sha = "f" * 40
        self.git_verifier.allow(request["repository"], request["branch"], code_sha)
        verified = self.client.post(
            f"/api/v1/test-jobs/{job_id}/verify",
            json={
                "repository": request["repository"],
                "branch": request["branch"],
                "code_sha": code_sha,
                "push_event_id": "github-test-dispatch",
            },
            headers=self._api_headers(),
        )
        self.assertEqual(202, verified.status_code, verified.text)
        self.assertTrue(self.launcher.wait_for_generate_launches(1))

        generating = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                f"evt_{job_id[4:]}_generating",
                "GENERATE",
                "GENERATING_TESTS",
                code_sha,
            ),
            headers=self._runner_headers(),
        )
        self.assertEqual(200, generating.status_code, generating.text)

        artifacts = []
        for artifact_type, name, content, media_type in (
            ("PATCH", "tests.patch", b"diff --git a/x b/x\n", "text/x-diff"),
            ("RESULT", "generation-result.json", b"{}", "application/json"),
        ):
            object_key = f"jobs/{job_id}/attempts/1/{name}"
            stored = self.client.app.state.artifact_store.put_bytes(object_key, content)
            artifacts.append(
                {
                    "type": artifact_type,
                    "object_key": stored.object_key,
                    "sha256": stored.sha256,
                    "size_bytes": stored.size_bytes,
                    "media_type": media_type,
                }
            )
        queued_event = self._phase_event(
            f"evt_{job_id[4:]}_test_queued",
            "GENERATE",
            "TEST_QUEUED",
            code_sha,
            artifacts,
        )
        queued = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=queued_event,
            headers=self._runner_headers(),
        )
        duplicate = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=queued_event,
            headers=self._runner_headers(),
        )
        self.assertEqual(200, queued.status_code, queued.text)
        self.assertEqual(200, duplicate.status_code, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertTrue(self.launcher.wait_for_test_launches(1))
        time.sleep(0.05)
        self.assertEqual(1, len(self.launcher.test_launches))
        launch = self.launcher.test_launches[0]
        self.assertEqual(code_sha, launch.code_sha)
        self.assertIsInstance(
            launch.runner_policy["command_argv"]["python-unittest-all"], list
        )

        testing = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                f"evt_{job_id[4:]}_testing", "TEST", "TESTING", code_sha
            ),
            headers=self._runner_headers(),
        )
        passed = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                f"evt_{job_id[4:]}_passed", "TEST", "PASSED", code_sha
            ),
            headers=self._runner_headers(),
        )
        self.assertEqual(200, testing.status_code, testing.text)
        self.assertEqual(200, passed.status_code, passed.text)
        status = self.client.get(
            f"/api/v1/test-jobs/{job_id}", headers=self._api_headers()
        ).json()
        self.assertEqual("PASSED", status["status"])
        self.assertEqual(code_sha, status["tested_sha"])
        self.assertTrue(self.notification_sender.wait_for_calls(1))
        time.sleep(0.05)
        self.assertEqual(1, len(self.notification_sender.calls))
        destination, payload = self.notification_sender.calls[0]
        self.assertEqual(request["callback_url"], destination)
        self.assertEqual("PASSED", payload["status"])
        self.assertEqual(job_id, payload["job_id"])

        duplicate_terminal = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._phase_event(
                f"evt_{job_id[4:]}_passed", "TEST", "PASSED", code_sha
            ),
            headers=self._runner_headers(),
        )
        self.assertEqual(200, duplicate_terminal.status_code)
        self.assertTrue(duplicate_terminal.json()["duplicate"])
        time.sleep(0.05)
        self.assertEqual(1, len(self.notification_sender.calls))

        with self.client.app.state.session_factory() as session:
            notifications = NotificationRepository(session).pending(
                datetime.max.replace(tzinfo=UTC)
            )
            self.assertEqual([], notifications)

    def _post_prepare(self, request: dict) -> dict:
        response = self.client.post(
            "/api/v1/test-jobs/prepare",
            json=request,
            headers=self._prepare_headers(request["plan"]),
        )
        self.assertEqual(202, response.status_code, response.text)
        self.launcher.wait_for_launches(1)
        return response.json()

    def _advance_to_waiting(self, job_id: str) -> None:
        started = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=self._event(f"evt_{job_id[4:]}_started", "PREPARING"),
            headers=self._runner_headers(),
        )
        self.assertEqual(200, started.status_code, started.text)
        object_key = f"jobs/{job_id}/attempts/1/test-draft.json"
        stored = self.client.app.state.artifact_store.put_bytes(object_key, b"{}")
        completed_event = self._event(f"evt_{job_id[4:]}_completed", "WAITING_FOR_CODE")
        completed_event["artifacts"] = [
            {
                "type": "DRAFT",
                "object_key": stored.object_key,
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "media_type": "application/json",
            }
        ]
        completed = self.client.post(
            f"/internal/v1/test-jobs/{job_id}/events",
            json=completed_event,
            headers=self._runner_headers(),
        )
        self.assertEqual(200, completed.status_code, completed.text)

    @staticmethod
    def _event(event_id: str, status: str) -> dict:
        return {
            "schema_version": "1.0",
            "event_id": event_id,
            "attempt": 1,
            "phase": "PREPARE",
            "status": status,
            "code_sha": None,
            "artifacts": [],
            "summary": {},
            "occurred_at": "2026-09-19T12:00:00Z",
        }

    @staticmethod
    def _phase_event(
        event_id: str,
        phase: str,
        status: str,
        code_sha: str,
        artifacts: list[dict] | None = None,
        attempt: int = 1,
    ) -> dict:
        return {
            "schema_version": "1.0",
            "event_id": event_id,
            "attempt": attempt,
            "phase": phase,
            "status": status,
            "code_sha": code_sha,
            "artifacts": artifacts or [],
            "summary": {},
            "occurred_at": "2026-09-19T12:00:00Z",
        }

    @staticmethod
    def _prepare_request(task_id: str = "TASK-123") -> dict:
        plan = yaml.safe_load(
            (REPOSITORY_ROOT / "docs" / "test-plans" / "example.yaml").read_text(
                encoding="utf-8"
            )
        )
        plan["task_id"] = task_id
        return {
            "task_id": task_id,
            "repository": plan["repository"],
            "branch": plan["branch"],
            "base_sha": plan["base_sha"],
            "plan": plan,
            "callback_url": "https://n8n.example.test/webhook/completed",
        }

    @staticmethod
    def _idempotency_key(plan: dict) -> str:
        canonical = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return f"{plan['task_id']}:{plan['base_sha']}:{digest}"

    def _prepare_headers(self, plan: dict) -> dict[str, str]:
        return {
            **self._api_headers(),
            "Idempotency-Key": self._idempotency_key(plan),
        }

    @staticmethod
    def _api_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {API_TOKEN}"}

    @staticmethod
    def _runner_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {RUNNER_TOKEN}"}


class SlowLauncher:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def launch_prepare(self, request: PrepareLaunch) -> str:
        time.sleep(self._delay_seconds)
        return f"slow-{request.job_id[-20:]}"


if __name__ == "__main__":
    unittest.main()
