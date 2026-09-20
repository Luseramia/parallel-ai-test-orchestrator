from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine

from app.db import create_session_factory
from app.domain.test_jobs import NotificationStatus
from app.repositories.models import Base
from app.repositories.notification_repository import NotificationRepository
from app.repositories.test_job_repository import TestJobRepository
from app.services.notification_service import (
    FakeNotificationSender,
    NotificationDispatcher,
)


class NotificationDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = (
            Path(self.temporary_directory.name) / "notifications.db"
        ).as_posix()
        self.engine = create_engine(f"sqlite+pysqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.session_factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_transient_failures_are_retried_until_delivered(self) -> None:
        notification_id = self._enqueue()
        sender = FakeNotificationSender(failures_before_success=2)
        dispatcher = NotificationDispatcher(
            self.session_factory,
            sender,
            max_attempts=4,
            initial_delay_seconds=0.001,
        )
        try:
            dispatcher.submit(notification_id)
            self.assertTrue(sender.wait_for_calls(3))
        finally:
            dispatcher.close()

        with self.session_factory() as session:
            row = NotificationRepository(session).get(notification_id)
            self.assertEqual(NotificationStatus.DELIVERED.value, row.status)
            self.assertEqual(3, row.attempts)
            self.assertIsNotNone(row.delivered_at)

    def test_exhausted_retries_are_dead_lettered(self) -> None:
        notification_id = self._enqueue()
        sender = FakeNotificationSender(failures_before_success=10)
        dispatcher = NotificationDispatcher(
            self.session_factory,
            sender,
            max_attempts=3,
            initial_delay_seconds=0.001,
        )
        try:
            dispatcher.submit(notification_id)
            self.assertTrue(sender.wait_for_calls(3))
        finally:
            dispatcher.close()

        with self.session_factory() as session:
            row = NotificationRepository(session).get(notification_id)
            self.assertEqual(NotificationStatus.DEAD.value, row.status)
            self.assertEqual(3, row.attempts)
            self.assertIsNone(row.delivered_at)
            self.assertIn("fake transient failure", row.last_error)

    def test_reconcile_only_submits_due_pending_notifications(self) -> None:
        notification_id = self._enqueue()
        sender = FakeNotificationSender()
        dispatcher = NotificationDispatcher(
            self.session_factory, sender, initial_delay_seconds=0.001
        )
        try:
            dispatcher.reconcile_pending()
            self.assertTrue(sender.wait_for_calls(1))
        finally:
            dispatcher.close()

        with self.session_factory() as session:
            row = NotificationRepository(session).get(notification_id)
            self.assertEqual(NotificationStatus.DELIVERED.value, row.status)

    def _enqueue(self) -> str:
        with self.session_factory() as session:
            job, _ = TestJobRepository(session).create_or_get_prepare(
                task_id="TASK-NOTIFY",
                repository="github.com/example/repository",
                branch="main",
                base_sha="a" * 40,
                plan_digest="b" * 64,
                callback_url="https://n8n.example.test/webhook/completed",
            )
            row, created = NotificationRepository(session).enqueue(
                job_id=job.id,
                event_type="terminal:1:PASSED",
                destination=job.callback_url,
                payload={"job_id": job.id, "status": "PASSED"},
            )
            self.assertTrue(created)
            session.commit()
            return row.id


if __name__ == "__main__":
    unittest.main()
