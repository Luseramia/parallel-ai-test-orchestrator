from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.domain.test_jobs import NotificationStatus
from app.repositories.models import NotificationRow, TestJobRow
from app.repositories.test_job_repository import NotFoundError


class NotificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        *,
        job_id: str,
        event_type: str,
        destination: str,
        payload: dict[str, Any],
    ) -> tuple[NotificationRow, bool]:
        existing = self._session.scalar(
            select(NotificationRow).where(
                NotificationRow.job_id == job_id,
                NotificationRow.event_type == event_type,
            )
        )
        if existing is not None:
            return existing, False
        if self._session.get(TestJobRow, job_id) is None:
            raise NotFoundError(f"test job {job_id!r} was not found")
        row = NotificationRow(
            id=str(uuid.uuid4()),
            job_id=job_id,
            event_type=event_type,
            destination=destination,
            payload=payload,
            status=NotificationStatus.PENDING.value,
            attempts=0,
        )
        self._session.add(row)
        self._session.flush()
        return row, True

    def get(self, notification_id: str) -> NotificationRow:
        row = self._session.get(NotificationRow, notification_id)
        if row is None:
            raise NotFoundError(f"notification {notification_id!r} was not found")
        return row

    def pending(self, now: datetime, limit: int = 100) -> list[NotificationRow]:
        return list(
            self._session.scalars(
                select(NotificationRow)
                .where(
                    NotificationRow.status == NotificationStatus.PENDING.value,
                    or_(
                        NotificationRow.next_attempt_at.is_(None),
                        NotificationRow.next_attempt_at <= now,
                    ),
                )
                .order_by(NotificationRow.created_at)
                .limit(limit)
            )
        )

    def record_failure(
        self,
        row: NotificationRow,
        *,
        error: str,
        next_attempt_at: datetime | None,
        dead: bool,
    ) -> None:
        row.attempts += 1
        row.status = (
            NotificationStatus.DEAD.value if dead else NotificationStatus.PENDING.value
        )
        row.last_error = error[:500]
        row.next_attempt_at = next_attempt_at
        row.updated_at = datetime.now(UTC)
        self._session.flush()

    def record_delivered(self, row: NotificationRow) -> None:
        now = datetime.now(UTC)
        row.attempts += 1
        row.status = NotificationStatus.DELIVERED.value
        row.last_error = None
        row.next_attempt_at = None
        row.updated_at = now
        row.delivered_at = now
        self._session.flush()
