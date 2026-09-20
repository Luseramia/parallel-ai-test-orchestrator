from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.test_jobs import (
    TERMINAL_STATUSES,
    ArtifactType,
    EventPhase,
    FailureClass,
    JobStatus,
    TestJob,
    validate_transition,
)
from app.repositories.models import (
    IdempotencyKeyRow,
    TestArtifactRow,
    TestJobEventRow,
    TestJobRow,
    VerifyAttemptRow,
)


class NotFoundError(LookupError):
    pass


class ConcurrentUpdateError(RuntimeError):
    pass


class IdempotencyConflictError(ValueError):
    pass


def _job_id() -> str:
    return f"atj_{uuid.uuid4().hex}"


def _uuid() -> str:
    return str(uuid.uuid4())


class TestJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, job_id: str) -> TestJob:
        row = self._session.get(TestJobRow, job_id)
        if row is None:
            raise NotFoundError(f"test job {job_id!r} was not found")
        return self._to_domain(row)

    def create_or_get_prepare(
        self,
        *,
        task_id: str,
        repository: str,
        branch: str,
        base_sha: str,
        plan_digest: str,
        callback_url: str,
    ) -> tuple[TestJob, bool]:
        existing = self._find_prepare(task_id, base_sha, plan_digest)
        if existing is not None:
            self._assert_prepare_matches(existing, repository, branch, callback_url)
            return self._to_domain(existing), False

        row = TestJobRow(
            id=_job_id(),
            task_id=task_id,
            repository=repository,
            branch=branch,
            base_sha=base_sha,
            plan_digest=plan_digest,
            callback_url=callback_url,
            status=JobStatus.PREPARE_QUEUED.value,
            prepare_attempt=1,
            verify_attempt=0,
            version=0,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            existing = self._find_prepare(task_id, base_sha, plan_digest)
            if existing is None:
                raise
            self._assert_prepare_matches(existing, repository, branch, callback_url)
            return self._to_domain(existing), False
        return self._to_domain(row), True

    def claim_verify_attempt(
        self, *, job_id: str, code_sha: str, delivery_id: str
    ) -> tuple[VerifyAttemptRow, bool]:
        job = self._session.scalar(
            select(TestJobRow).where(TestJobRow.id == job_id).with_for_update()
        )
        if job is None:
            raise NotFoundError(f"test job {job_id!r} was not found")

        # Recheck both identities after taking the per-job lock. This makes
        # attempt numbering deterministic when verify deliveries race.
        by_delivery = self._session.scalar(
            select(VerifyAttemptRow).where(VerifyAttemptRow.delivery_id == delivery_id)
        )
        if by_delivery is not None:
            if by_delivery.job_id != job_id or by_delivery.code_sha != code_sha:
                raise IdempotencyConflictError(
                    "delivery ID was already used for a different verify request"
                )
            return by_delivery, False

        by_sha = self._session.scalar(
            select(VerifyAttemptRow).where(
                VerifyAttemptRow.job_id == job_id,
                VerifyAttemptRow.code_sha == code_sha,
            )
        )
        if by_sha is not None:
            return by_sha, False

        latest_attempt = self._session.scalar(
            select(func.max(VerifyAttemptRow.attempt)).where(
                VerifyAttemptRow.job_id == job_id
            )
        )
        row = VerifyAttemptRow(
            id=_uuid(),
            job_id=job_id,
            code_sha=code_sha,
            delivery_id=delivery_id,
            attempt=(latest_attempt or 0) + 1,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            by_delivery = self._session.scalar(
                select(VerifyAttemptRow).where(
                    VerifyAttemptRow.delivery_id == delivery_id
                )
            )
            if by_delivery is not None:
                if by_delivery.job_id != job_id or by_delivery.code_sha != code_sha:
                    raise IdempotencyConflictError(
                        "delivery ID was already used for a different verify request"
                    )
                return by_delivery, False
            by_sha = self._session.scalar(
                select(VerifyAttemptRow).where(
                    VerifyAttemptRow.job_id == job_id,
                    VerifyAttemptRow.code_sha == code_sha,
                )
            )
            if by_sha is None:
                raise
            return by_sha, False
        return row, True

    def add_artifact(
        self,
        *,
        job_id: str,
        attempt: int,
        artifact_type: ArtifactType,
        object_key: str,
        sha256: str,
        size_bytes: int,
        media_type: str | None = None,
    ) -> tuple[TestArtifactRow, bool]:
        existing = self._session.scalar(
            select(TestArtifactRow).where(TestArtifactRow.object_key == object_key)
        )
        if existing is not None:
            if (
                existing.job_id != job_id
                or existing.attempt != attempt
                or existing.type != artifact_type.value
                or existing.sha256 != sha256
                or existing.size_bytes != size_bytes
            ):
                raise IdempotencyConflictError(
                    "artifact object key was reused with different metadata"
                )
            return existing, False
        if self._session.get(TestJobRow, job_id) is None:
            raise NotFoundError(f"test job {job_id!r} was not found")
        row = TestArtifactRow(
            id=_uuid(),
            job_id=job_id,
            attempt=attempt,
            type=artifact_type.value,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
        )
        self._session.add(row)
        self._session.flush()
        return row, True

    def list_artifacts(self, job_id: str) -> list[TestArtifactRow]:
        return list(
            self._session.scalars(
                select(TestArtifactRow)
                .where(TestArtifactRow.job_id == job_id)
                .order_by(TestArtifactRow.created_at, TestArtifactRow.id)
            )
        )

    def get_artifact(self, job_id: str, artifact_id: str) -> TestArtifactRow:
        row = self._session.scalar(
            select(TestArtifactRow).where(
                TestArtifactRow.job_id == job_id,
                TestArtifactRow.id == artifact_id,
            )
        )
        if row is None:
            raise NotFoundError(f"artifact {artifact_id!r} was not found")
        return row

    def list_stale_active(
        self,
        *,
        runner_cutoff: datetime,
        waiting_cutoff: datetime,
        limit: int = 100,
    ) -> list[TestJob]:
        """Non-terminal jobs that have outlived the deadline for their phase.

        WAITING_FOR_CODE is idle by design - it waits for a human or agent to
        push - so it gets its own, much longer cutoff instead of the runner
        deadline.
        """

        rows = self._session.scalars(
            select(TestJobRow)
            .where(
                TestJobRow.status.not_in(
                    [status.value for status in TERMINAL_STATUSES]
                ),
                or_(
                    and_(
                        TestJobRow.status == JobStatus.WAITING_FOR_CODE.value,
                        TestJobRow.updated_at < waiting_cutoff,
                    ),
                    and_(
                        TestJobRow.status != JobStatus.WAITING_FOR_CODE.value,
                        TestJobRow.updated_at < runner_cutoff,
                    ),
                ),
            )
            .order_by(TestJobRow.updated_at)
            .limit(limit)
        )
        return [self._to_domain(row) for row in rows]

    def list_finished_with_k8s_job(self, *, limit: int = 100) -> list[TestJob]:
        """Terminal jobs that still name a Kubernetes Job to clean up."""

        rows = self._session.scalars(
            select(TestJobRow)
            .where(
                TestJobRow.status.in_([status.value for status in TERMINAL_STATUSES]),
                TestJobRow.latest_k8s_job_name.is_not(None),
            )
            .order_by(TestJobRow.updated_at)
            .limit(limit)
        )
        return [self._to_domain(row) for row in rows]

    def clear_k8s_job(self, *, job_id: str, k8s_job_name: str) -> bool:
        """Forget a deleted Kubernetes Job without touching job state.

        This is bookkeeping, not a state transition, so it deliberately leaves
        `version` alone: bumping it would make an in-flight dispatcher lose a
        compare-and-set it should have won.
        """

        result = self._session.execute(
            update(TestJobRow)
            .where(
                TestJobRow.id == job_id,
                TestJobRow.latest_k8s_job_name == k8s_job_name,
            )
            .values(latest_k8s_job_name=None)
        )
        self._session.expire_all()
        return result.rowcount == 1

    def list_purgeable_artifacts(
        self, *, cutoff: datetime, limit: int = 500
    ) -> list[TestArtifactRow]:
        """Stored artifacts of jobs that finished before the retention cutoff."""

        return list(
            self._session.scalars(
                select(TestArtifactRow)
                .join(TestJobRow, TestJobRow.id == TestArtifactRow.job_id)
                .where(
                    TestArtifactRow.purged_at.is_(None),
                    TestJobRow.completed_at.is_not(None),
                    TestJobRow.completed_at < cutoff,
                )
                .order_by(TestArtifactRow.created_at, TestArtifactRow.id)
                .limit(limit)
            )
        )

    def mark_artifact_purged(self, artifact_id: str, purged_at: datetime) -> None:
        self._session.execute(
            update(TestArtifactRow)
            .where(TestArtifactRow.id == artifact_id)
            .values(purged_at=purged_at)
        )

    def set_latest_k8s_job(
        self,
        *,
        job_id: str,
        k8s_job_name: str,
        expected_statuses: Collection[JobStatus],
        expected_prepare_attempt: int | None = None,
        expected_verify_attempt: int | None = None,
        expected_code_sha: str | None = None,
    ) -> bool:
        """Record dispatch metadata only while the launch is still current.

        The Kubernetes name is bookkeeping rather than a state transition. It
        must not advance `version` or `updated_at`: doing so can invalidate a
        runner callback that already read the current state-machine version,
        and can also extend the reconciliation timeout for the active phase.
        """

        conditions = [
            TestJobRow.id == job_id,
            TestJobRow.status.in_([status.value for status in expected_statuses]),
        ]
        if expected_prepare_attempt is not None:
            conditions.append(
                TestJobRow.prepare_attempt == expected_prepare_attempt
            )
        if expected_verify_attempt is not None:
            conditions.append(TestJobRow.verify_attempt == expected_verify_attempt)
        if expected_code_sha is not None:
            conditions.append(TestJobRow.code_sha == expected_code_sha)

        result = self._session.execute(
            update(TestJobRow)
            .where(*conditions)
            .values(latest_k8s_job_name=k8s_job_name)
        )
        self._session.expire_all()
        return result.rowcount == 1

    def claim_idempotency_key(
        self,
        *,
        scope: str,
        key: str,
        request_sha256: str,
        resource_id: str,
        expires_at: datetime | None = None,
    ) -> tuple[IdempotencyKeyRow, bool]:
        existing = self._session.get(IdempotencyKeyRow, (scope, key))
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise IdempotencyConflictError(
                    "idempotency key was reused with a different request"
                )
            return existing, False

        row = IdempotencyKeyRow(
            scope=scope,
            key=key,
            request_sha256=request_sha256,
            resource_id=resource_id,
            expires_at=expires_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            existing = self._session.get(IdempotencyKeyRow, (scope, key))
            if existing is None:
                raise
            if existing.request_sha256 != request_sha256:
                raise IdempotencyConflictError(
                    "idempotency key was reused with a different request"
                )
            return existing, False
        return row, True

    def record_event(
        self,
        *,
        event_id: str,
        job_id: str,
        expected_version: int,
        attempt: int,
        phase: EventPhase,
        to_status: JobStatus,
        occurred_at: datetime,
        code_sha: str | None = None,
        payload: dict[str, Any] | None = None,
        tested_sha: str | None = None,
        failure_class: FailureClass | None = None,
        failure_message: str | None = None,
        verify_attempt: int | None = None,
        clear_result: bool = False,
    ) -> tuple[TestJob, bool]:
        existing_event = self._session.get(TestJobEventRow, event_id)
        if existing_event is not None:
            if (
                existing_event.job_id != job_id
                or existing_event.attempt != attempt
                or existing_event.phase != phase.value
                or existing_event.to_status != to_status.value
                or existing_event.code_sha != code_sha
                or existing_event.payload != (payload or {})
            ):
                raise IdempotencyConflictError(
                    "event ID was reused with different event data"
                )
            return self.get(job_id), False

        row = self._session.get(TestJobRow, job_id)
        if row is None:
            raise NotFoundError(f"test job {job_id!r} was not found")
        from_status = JobStatus(row.status)
        validate_transition(from_status, to_status)

        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "status": to_status.value,
            "version": expected_version + 1,
            "updated_at": now,
        }
        if code_sha is not None:
            values["code_sha"] = code_sha
        if tested_sha is not None:
            values["tested_sha"] = tested_sha
        if failure_class is not None:
            values["failure_class"] = failure_class.value
        if failure_message is not None:
            values["failure_message"] = failure_message
        if verify_attempt is not None:
            values["verify_attempt"] = verify_attempt
        if clear_result:
            values.update(
                tested_sha=None,
                failure_class=None,
                failure_message=None,
                completed_at=None,
            )
        if to_status.is_terminal:
            values["completed_at"] = now

        result = self._session.execute(
            update(TestJobRow)
            .where(TestJobRow.id == job_id, TestJobRow.version == expected_version)
            .values(**values)
        )
        if result.rowcount != 1:
            raise ConcurrentUpdateError(
                f"test job {job_id!r} is no longer at version {expected_version}"
            )

        self._session.add(
            TestJobEventRow(
                event_id=event_id,
                job_id=job_id,
                attempt=attempt,
                phase=phase.value,
                from_status=from_status.value,
                to_status=to_status.value,
                code_sha=code_sha,
                payload=payload or {},
                occurred_at=occurred_at,
            )
        )
        self._session.flush()
        self._session.expire(row)
        return self.get(job_id), True

    def _find_prepare(
        self, task_id: str, base_sha: str, plan_digest: str
    ) -> TestJobRow | None:
        return self._session.scalar(
            select(TestJobRow).where(
                TestJobRow.task_id == task_id,
                TestJobRow.base_sha == base_sha,
                TestJobRow.plan_digest == plan_digest,
            )
        )

    @staticmethod
    def _assert_prepare_matches(
        row: TestJobRow, repository: str, branch: str, callback_url: str
    ) -> None:
        if (
            row.repository != repository
            or row.branch != branch
            or row.callback_url != callback_url
        ):
            raise IdempotencyConflictError(
                "prepare identity matched a job with different routing data"
            )

    @staticmethod
    def _to_domain(row: TestJobRow) -> TestJob:
        return TestJob(
            id=row.id,
            task_id=row.task_id,
            repository=row.repository,
            branch=row.branch,
            base_sha=row.base_sha,
            plan_digest=row.plan_digest,
            callback_url=row.callback_url,
            status=JobStatus(row.status),
            prepare_attempt=row.prepare_attempt,
            verify_attempt=row.verify_attempt,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            code_sha=row.code_sha,
            tested_sha=row.tested_sha,
            latest_k8s_job_name=row.latest_k8s_job_name,
            failure_class=FailureClass(row.failure_class)
            if row.failure_class
            else None,
            failure_message=row.failure_message,
            completed_at=row.completed_at,
        )
