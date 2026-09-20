from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.test_jobs import EventPhase, JobStatus
from app.repositories.models import Base, TestJobEventRow, TestJobRow, VerifyAttemptRow
from app.repositories.test_job_repository import (
    ConcurrentUpdateError,
    IdempotencyConflictError,
    TestJobRepository,
)

BASE_SHA = "a" * 40
CODE_SHA = "b" * 40
PLAN_DIGEST = "c" * 64
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        database_path = (
            Path(self._temporary_directory.name) / "repository.db"
        ).as_posix()
        self.engine = create_engine(f"sqlite+pysqlite:///{database_path}")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(
            dbapi_connection: object, _connection_record: object
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temporary_directory.cleanup()

    def test_duplicate_prepare_returns_the_original_job(self) -> None:
        with self.sessions() as first_session:
            first, created = self._create_prepare(first_session)
            first_session.commit()
        with self.sessions() as duplicate_session:
            duplicate, duplicate_created = self._create_prepare(duplicate_session)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.id, duplicate.id)
        with self.sessions() as session:
            self.assertEqual(1, session.query(TestJobRow).count())

    def test_prepare_identity_rejects_different_routing_data(self) -> None:
        with self.sessions() as session:
            self._create_prepare(session)
            session.commit()
        with self.sessions() as session, self.assertRaises(IdempotencyConflictError):
            self._create_prepare(session, branch="feature/not-the-original")

    def test_duplicate_verify_by_sha_or_delivery_does_not_create_an_attempt(
        self,
    ) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            repository = TestJobRepository(session)
            first, created = repository.claim_verify_attempt(
                job_id=job_id, code_sha=CODE_SHA, delivery_id="delivery-1"
            )
            session.commit()

        with self.sessions() as session:
            repository = TestJobRepository(session)
            by_delivery, delivery_created = repository.claim_verify_attempt(
                job_id=job_id, code_sha=CODE_SHA, delivery_id="delivery-1"
            )
            by_sha, sha_created = repository.claim_verify_attempt(
                job_id=job_id, code_sha=CODE_SHA, delivery_id="delivery-2"
            )

        self.assertTrue(created)
        self.assertFalse(delivery_created)
        self.assertFalse(sha_created)
        self.assertEqual(first.id, by_delivery.id)
        self.assertEqual(first.id, by_sha.id)
        with self.sessions() as session:
            self.assertEqual(1, session.query(VerifyAttemptRow).count())

    def test_reused_delivery_for_different_verify_is_rejected(self) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            repository = TestJobRepository(session)
            repository.claim_verify_attempt(
                job_id=job_id, code_sha=CODE_SHA, delivery_id="delivery-1"
            )
            session.commit()
        with self.sessions() as session:
            repository = TestJobRepository(session)
            with self.assertRaises(IdempotencyConflictError):
                repository.claim_verify_attempt(
                    job_id=job_id, code_sha="d" * 40, delivery_id="delivery-1"
                )

    def test_duplicate_event_is_acknowledged_without_a_second_transition(self) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            repository = TestJobRepository(session)
            transitioned, created = repository.record_event(
                event_id="evt_prepare_started",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )
            session.commit()
        with self.sessions() as session:
            duplicate, duplicate_created = TestJobRepository(session).record_event(
                event_id="evt_prepare_started",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(JobStatus.PREPARING, transitioned.status)
        self.assertEqual(1, transitioned.version)
        self.assertEqual(transitioned.version, duplicate.version)
        with self.sessions() as session:
            self.assertEqual(1, session.query(TestJobEventRow).count())

    def test_reused_event_id_with_different_data_is_rejected(self) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            TestJobRepository(session).record_event(
                event_id="evt_prepare_started",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )
            session.commit()
        with self.sessions() as session, self.assertRaises(IdempotencyConflictError):
            TestJobRepository(session).record_event(
                event_id="evt_prepare_started",
                job_id=job_id,
                expected_version=1,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.WAITING_FOR_CODE,
                occurred_at=NOW,
            )

    def test_reused_event_id_with_different_payload_is_rejected(self) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            TestJobRepository(session).record_event(
                event_id="evt_payload_conflict",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
                payload={"summary": "original"},
            )
            session.commit()
        with self.sessions() as session, self.assertRaises(IdempotencyConflictError):
            TestJobRepository(session).record_event(
                event_id="evt_payload_conflict",
                job_id=job_id,
                expected_version=1,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
                payload={"summary": "changed"},
            )

    def test_stale_concurrent_writer_loses_optimistic_update(self) -> None:
        job_id = self._persist_job()
        first_session = self.sessions()
        stale_session = self.sessions()
        try:
            first_session.get(TestJobRow, job_id)
            stale_session.get(TestJobRow, job_id)
            TestJobRepository(first_session).record_event(
                event_id="evt_first_writer",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )
            first_session.commit()

            with self.assertRaises(ConcurrentUpdateError):
                TestJobRepository(stale_session).record_event(
                    event_id="evt_stale_writer",
                    job_id=job_id,
                    expected_version=0,
                    attempt=1,
                    phase=EventPhase.SYSTEM,
                    to_status=JobStatus.TIMEOUT,
                    occurred_at=NOW,
                )
            stale_session.rollback()
        finally:
            first_session.close()
            stale_session.close()

        with self.sessions() as session:
            job = session.get(TestJobRow, job_id)
            event_ids = set(session.scalars(select(TestJobEventRow.event_id)))
        self.assertIsNotNone(job)
        self.assertEqual(JobStatus.PREPARING.value, job.status)
        self.assertEqual(1, job.version)
        self.assertEqual({"evt_first_writer"}, event_ids)

    def test_k8s_job_bookkeeping_does_not_invalidate_runner_callback(self) -> None:
        job_id = self._persist_job()
        callback_session = self.sessions()
        try:
            callback_repository = TestJobRepository(callback_session)
            callback_job = callback_repository.get(job_id)

            with self.sessions() as dispatcher_session:
                recorded = TestJobRepository(
                    dispatcher_session
                ).set_latest_k8s_job(
                    job_id=job_id,
                    k8s_job_name="codex-prepare-race-regression",
                    expected_statuses={JobStatus.PREPARE_QUEUED},
                    expected_prepare_attempt=1,
                )
                dispatcher_session.commit()

            transitioned, created = callback_repository.record_event(
                event_id="evt_callback_after_dispatch_metadata",
                job_id=job_id,
                expected_version=callback_job.version,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )
            callback_session.commit()
        finally:
            callback_session.close()

        self.assertTrue(recorded)
        self.assertTrue(created)
        self.assertEqual(JobStatus.PREPARING, transitioned.status)
        self.assertEqual(1, transitioned.version)
        self.assertEqual(
            "codex-prepare-race-regression", transitioned.latest_k8s_job_name
        )

    def test_stale_dispatch_metadata_cannot_overwrite_a_later_phase(self) -> None:
        job_id = self._persist_job()
        with self.sessions() as session:
            repository = TestJobRepository(session)
            preparing, _ = repository.record_event(
                event_id="evt_prepare_started_before_stale_dispatch",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=NOW,
            )
            repository.record_event(
                event_id="evt_prepare_finished_before_stale_dispatch",
                job_id=job_id,
                expected_version=preparing.version,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.WAITING_FOR_CODE,
                occurred_at=NOW,
            )
            session.commit()

        with self.sessions() as session:
            recorded = TestJobRepository(session).set_latest_k8s_job(
                job_id=job_id,
                k8s_job_name="stale-prepare-job",
                expected_statuses={JobStatus.PREPARE_QUEUED, JobStatus.PREPARING},
                expected_prepare_attempt=1,
            )
            session.commit()

        with self.sessions() as session:
            job = TestJobRepository(session).get(job_id)
        self.assertFalse(recorded)
        self.assertIsNone(job.latest_k8s_job_name)
        self.assertEqual(2, job.version)

    def test_generic_idempotency_key_detects_payload_conflict(self) -> None:
        with self.sessions() as session:
            repository = TestJobRepository(session)
            claimed, created = repository.claim_idempotency_key(
                scope="prepare",
                key="TASK-123:key",
                request_sha256="1" * 64,
                resource_id="atj_" + "1" * 32,
            )
            duplicate, duplicate_created = repository.claim_idempotency_key(
                scope="prepare",
                key="TASK-123:key",
                request_sha256="1" * 64,
                resource_id="atj_" + "1" * 32,
            )
            with self.assertRaises(IdempotencyConflictError):
                repository.claim_idempotency_key(
                    scope="prepare",
                    key="TASK-123:key",
                    request_sha256="2" * 64,
                    resource_id="atj_" + "2" * 32,
                )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(claimed.resource_id, duplicate.resource_id)

    def _persist_job(self) -> str:
        with self.sessions() as session:
            job, _ = self._create_prepare(session)
            session.commit()
            return job.id

    @staticmethod
    def _create_prepare(session: Session, branch: str = "main"):
        return TestJobRepository(session).create_or_get_prepare(
            task_id="TASK-123",
            repository="github.com/Luseramia/ai-orchestrator",
            branch=branch,
            base_sha=BASE_SHA,
            plan_digest=PLAN_DIGEST,
            callback_url="https://n8n.example.test/webhook/completed",
        )


if __name__ == "__main__":
    unittest.main()
