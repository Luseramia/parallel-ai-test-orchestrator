from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app.domain.test_jobs import EventPhase, JobStatus
from app.repositories.models import Base, TestJobEventRow, TestJobRow
from app.repositories.test_job_repository import (
    ConcurrentUpdateError,
    TestJobRepository,
)

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
GATEWAY_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(DATABASE_URL, "TEST_DATABASE_URL is not configured")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config = Config(str(GATEWAY_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(config, "head")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.sessions = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_migration_matches_metadata_on_postgresql(self) -> None:
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            self.assertEqual([], compare_metadata(context, Base.metadata))

    def test_concurrent_duplicate_prepare_creates_one_job(self) -> None:
        task_id = "PG-CONCURRENT-PREPARE"

        def create() -> tuple[str, bool]:
            with self.sessions() as session:
                job, created = TestJobRepository(session).create_or_get_prepare(
                    task_id=task_id,
                    repository="github.com/Luseramia/ai-orchestrator",
                    branch="main",
                    base_sha="1" * 40,
                    plan_digest="2" * 64,
                    callback_url="https://n8n.example.test/webhook/completed",
                )
                session.commit()
                return job.id, created

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: create(), range(2)))

        self.assertEqual(1, len({job_id for job_id, _created in results}))
        self.assertEqual([False, True], sorted(created for _job_id, created in results))

    def test_optimistic_lock_rejects_stale_postgresql_writer(self) -> None:
        job_id = self._create_job("PG-OPTIMISTIC")
        first_session = self.sessions()
        stale_session = self.sessions()
        try:
            first_session.get(TestJobRow, job_id)
            stale_session.get(TestJobRow, job_id)
            TestJobRepository(first_session).record_event(
                event_id="evt_pg_first_writer",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=datetime.now(UTC),
            )
            first_session.commit()
            with self.assertRaises(ConcurrentUpdateError):
                TestJobRepository(stale_session).record_event(
                    event_id="evt_pg_stale_writer",
                    job_id=job_id,
                    expected_version=0,
                    attempt=1,
                    phase=EventPhase.SYSTEM,
                    to_status=JobStatus.TIMEOUT,
                    occurred_at=datetime.now(UTC),
                )
            stale_session.rollback()
        finally:
            first_session.close()
            stale_session.close()

    def test_event_table_rejects_updates_and_deletes(self) -> None:
        job_id = self._create_job("PG-APPEND-ONLY")
        with self.sessions() as session:
            TestJobRepository(session).record_event(
                event_id="evt_pg_append_only",
                job_id=job_id,
                expected_version=0,
                attempt=1,
                phase=EventPhase.PREPARE,
                to_status=JobStatus.PREPARING,
                occurred_at=datetime.now(UTC),
            )
            session.commit()

        for statement in (
            "UPDATE ai_test_job_events SET payload = '{}' WHERE event_id = 'evt_pg_append_only'",
            "DELETE FROM ai_test_job_events WHERE event_id = 'evt_pg_append_only'",
        ):
            with self.engine.connect() as connection:
                with self.assertRaises(DatabaseError):
                    connection.execute(text(statement))
                    connection.commit()
                connection.rollback()
        with self.sessions() as session:
            self.assertIsNotNone(session.get(TestJobEventRow, "evt_pg_append_only"))

    def _create_job(self, task_id: str) -> str:
        with self.sessions() as session:
            job, _created = TestJobRepository(session).create_or_get_prepare(
                task_id=task_id,
                repository="github.com/Luseramia/ai-orchestrator",
                branch="main",
                base_sha="3" * 40,
                plan_digest="4" * 64,
                callback_url="https://n8n.example.test/webhook/completed",
            )
            session.commit()
            return job.id


if __name__ == "__main__":
    unittest.main()
