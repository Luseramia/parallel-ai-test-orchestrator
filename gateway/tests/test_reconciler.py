from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from app.config import Settings
from app.db import create_session_factory
from app.domain.test_jobs import ArtifactType, JobStatus, NotificationStatus
from app.repositories.models import (
    Base,
    NotificationRow,
    TestArtifactRow,
    TestJobRow,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.test_job_repository import TestJobRepository
from app.services.artifact_service import FilesystemArtifactStore
from app.services.job_launcher import FakeKubernetesJobLauncher
from app.services.notification_service import (
    FakeNotificationSender,
    NotificationDispatcher,
)
from app.services.reconciler import JobReconciler

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
BASE_SHA = "a" * 40
PLAN_DIGEST = "c" * 64


class ReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        database_path = (self.root / "reconciler.db").as_posix()
        self.engine = create_engine(f"sqlite+pysqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.session_factory = create_session_factory(self.engine)
        self.launcher = FakeKubernetesJobLauncher()
        self.sender = FakeNotificationSender()
        self.dispatcher = NotificationDispatcher(
            self.session_factory, self.sender, initial_delay_seconds=0.001
        )
        self.store = FilesystemArtifactStore(self.root / "artifacts")
        self.settings = Settings(
            database_url=f"sqlite+pysqlite:///{database_path}",
            api_token="api-token-for-tests",
            runner_token="runner-token-for-tests",
            artifact_signing_key="artifact-signing-key-for-tests",
            artifact_root=self.root / "artifacts",
            repository_policy_path=self.root / "policy.yaml",
            runner_active_deadline_seconds=1800,
            reconcile_grace_seconds=300,
            waiting_for_code_ttl_seconds=86_400,
            artifact_retention_days=30,
        )
        self._sequence = 0
        self.reconciler = JobReconciler(
            self.session_factory,
            self.launcher,
            self.store,
            self.dispatcher,
            self.settings,
        )

    def tearDown(self) -> None:
        self.dispatcher.close()
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_abandoned_runner_phase_times_out_once_and_notifies_once(self) -> None:
        job_id = self._job(
            JobStatus.TESTING,
            updated_at=NOW - timedelta(seconds=2_400),
            k8s_job_name="test-runner-abandoned-1",
        )

        report = self.reconciler.run(NOW)
        repeat = self.reconciler.run(NOW)

        self.assertEqual([job_id], list(report.timed_out))
        self.assertEqual([], list(repeat.timed_out))
        job = self._read(job_id)
        self.assertEqual(JobStatus.TIMEOUT, job.status)
        self.assertEqual("TIMEOUT", job.failure_class.value)
        self.assertIsNotNone(job.completed_at)
        self.assertTrue(self.sender.wait_for_calls(1))
        self.assertEqual(1, len(self.sender.calls))
        _, payload = self.sender.calls[0]
        self.assertEqual("TIMEOUT", payload["status"])
        self.assertEqual("runner_deadline_exceeded", payload["summary"]["reason"])
        self.assertEqual("TESTING", payload["summary"]["last_status"])
        self.assertIn("TIMEOUT", payload["message"])

    def test_timeout_deletes_only_the_kubernetes_job_of_that_test_job(self) -> None:
        stale = self._job(
            JobStatus.GENERATING_TESTS,
            updated_at=NOW - timedelta(seconds=2_400),
            k8s_job_name="codex-generate-stale-1",
        )
        self._job(
            JobStatus.GENERATING_TESTS,
            updated_at=NOW,
            k8s_job_name="codex-generate-healthy-1",
            task_id="TASK-HEALTHY",
        )

        self.reconciler.run(NOW)

        self.assertEqual(
            [("codex-generate-stale-1", stale)], self.launcher.deleted_jobs
        )

    def test_waiting_for_code_uses_its_own_much_longer_deadline(self) -> None:
        waiting = self._job(
            JobStatus.WAITING_FOR_CODE, updated_at=NOW - timedelta(hours=6)
        )

        self.assertEqual([], list(self.reconciler.run(NOW).timed_out))
        self.assertEqual(JobStatus.WAITING_FOR_CODE, self._read(waiting).status)

        expired = self.reconciler.run(NOW + timedelta(hours=20))
        self.assertEqual([waiting], list(expired.timed_out))
        self.assertTrue(self.sender.wait_for_calls(1))
        self.assertEqual(
            "waiting_for_code_expired", self.sender.calls[0][1]["summary"]["reason"]
        )

    def test_finished_jobs_do_not_time_out_and_release_their_kubernetes_job(
        self,
    ) -> None:
        job_id = self._job(
            JobStatus.PASSED,
            updated_at=NOW - timedelta(days=2),
            k8s_job_name="test-runner-finished-1",
            completed_at=NOW - timedelta(days=2),
        )

        report = self.reconciler.run(NOW)
        repeat = self.reconciler.run(NOW)

        self.assertEqual([], list(report.timed_out))
        self.assertEqual([job_id], list(report.released))
        self.assertEqual(
            [("test-runner-finished-1", job_id)], self.launcher.deleted_jobs
        )
        self.assertIsNone(self._read(job_id).latest_k8s_job_name)
        self.assertEqual([], list(repeat.released))
        self.assertEqual(1, len(self.launcher.deleted_jobs))

    def test_a_failed_deletion_keeps_the_job_name_for_the_next_sweep(self) -> None:
        job_id = self._job(
            JobStatus.FAILED,
            updated_at=NOW - timedelta(days=2),
            k8s_job_name="test-runner-unreachable-1",
            completed_at=NOW - timedelta(days=2),
        )
        reconciler = JobReconciler(
            self.session_factory,
            RefusingLauncher(),
            self.store,
            self.dispatcher,
            self.settings,
        )

        report = reconciler.run(NOW)

        self.assertEqual([], list(report.released))
        self.assertEqual(
            "test-runner-unreachable-1", self._read(job_id).latest_k8s_job_name
        )

    def test_retention_purges_bytes_but_keeps_the_audit_row(self) -> None:
        job_id = self._job(
            JobStatus.PASSED,
            updated_at=NOW - timedelta(days=40),
            completed_at=NOW - timedelta(days=40),
        )
        stored = self.store.put_bytes(f"jobs/{job_id}/attempts/1/raw.log", b"log bytes")
        with self.session_factory() as session:
            TestJobRepository(session).add_artifact(
                job_id=job_id,
                attempt=1,
                artifact_type=ArtifactType.RAW_LOG,
                object_key=stored.object_key,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_type="text/plain",
            )
            session.commit()

        report = self.reconciler.run(NOW)
        repeat = self.reconciler.run(NOW)

        self.assertEqual(1, report.purged_artifacts)
        self.assertEqual(len(b"log bytes"), report.purged_bytes)
        self.assertEqual(0, repeat.purged_artifacts)
        with self.session_factory() as session:
            row = session.scalar(select(TestArtifactRow))
            self.assertIsNotNone(row.purged_at)
            self.assertEqual(stored.sha256, row.sha256)
            self.assertEqual(stored.size_bytes, row.size_bytes)
        self.assertFalse((self.root / "artifacts" / stored.object_key).exists())

    def test_retention_keeps_artifacts_of_recent_and_unfinished_jobs(self) -> None:
        recent = self._job(
            JobStatus.PASSED,
            updated_at=NOW - timedelta(days=2),
            completed_at=NOW - timedelta(days=2),
        )
        running = self._job(JobStatus.TESTING, updated_at=NOW, task_id="TASK-RUNNING")
        for job_id in (recent, running):
            stored = self.store.put_bytes(f"jobs/{job_id}/attempts/1/raw.log", b"keep")
            with self.session_factory() as session:
                TestJobRepository(session).add_artifact(
                    job_id=job_id,
                    attempt=1,
                    artifact_type=ArtifactType.RAW_LOG,
                    object_key=stored.object_key,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    media_type="text/plain",
                )
                session.commit()

        report = self.reconciler.run(NOW)

        self.assertEqual(0, report.purged_artifacts)
        with self.session_factory() as session:
            purged = [row.purged_at for row in session.scalars(select(TestArtifactRow))]
            self.assertEqual([None, None], purged)

    def test_pending_notifications_are_retried(self) -> None:
        job_id = self._job(JobStatus.PASSED, updated_at=NOW, completed_at=NOW)
        with self.session_factory() as session:
            NotificationRepository(session).enqueue(
                job_id=job_id,
                event_type="terminal:1:PASSED",
                destination="https://n8n.example.test/webhook/completed",
                payload={"job_id": job_id, "status": "PASSED"},
            )
            session.commit()

        report = self.reconciler.run(NOW)

        self.assertEqual(1, report.notifications_retried)
        self.assertTrue(self.sender.wait_for_calls(1))
        with self.session_factory() as session:
            repository = NotificationRepository(session)
            self.assertEqual([], repository.pending(NOW + timedelta(days=1)))
            delivered = session.scalar(select(NotificationRow))
            self.assertEqual(NotificationStatus.DELIVERED.value, delivered.status)

    def _job(
        self,
        status: JobStatus,
        *,
        updated_at: datetime,
        k8s_job_name: str | None = None,
        completed_at: datetime | None = None,
        task_id: str = "TASK-123",
    ) -> str:
        self._sequence += 1
        job_id = f"atj_{self._sequence:032d}"
        with self.session_factory() as session:
            session.add(
                TestJobRow(
                    id=job_id,
                    task_id=task_id,
                    repository="github.com/Luseramia/ai-orchestrator",
                    branch="main",
                    base_sha=BASE_SHA,
                    plan_digest=PLAN_DIGEST,
                    callback_url="https://n8n.example.test/webhook/completed",
                    status=status.value,
                    prepare_attempt=1,
                    verify_attempt=0,
                    latest_k8s_job_name=k8s_job_name,
                    created_at=updated_at,
                    updated_at=updated_at,
                    completed_at=completed_at,
                    version=1,
                )
            )
            session.commit()
        return job_id

    def _read(self, job_id: str):
        with self.session_factory() as session:
            return TestJobRepository(session).get(job_id)


class RefusingLauncher:
    def delete_job(self, job_name: str, job_id: str) -> None:
        raise RuntimeError("Kubernetes API is unavailable")


if __name__ == "__main__":
    unittest.main()
