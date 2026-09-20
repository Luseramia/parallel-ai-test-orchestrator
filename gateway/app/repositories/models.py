from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.test_jobs import (
    ArtifactType,
    EventPhase,
    FailureClass,
    JobStatus,
    NotificationStatus,
)


def _sql_values(
    values: type[
        JobStatus | EventPhase | FailureClass | ArtifactType | NotificationStatus
    ],
) -> str:
    return ", ".join(f"'{value.value}'" for value in values)


class Base(DeclarativeBase):
    pass


class TestJobRow(Base):
    __tablename__ = "ai_test_jobs"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "base_sha", "plan_digest", name="uq_ai_test_jobs_prepare"
        ),
        CheckConstraint(
            f"status IN ({_sql_values(JobStatus)})", name="ck_ai_test_jobs_status"
        ),
        CheckConstraint(
            f"failure_class IS NULL OR failure_class IN ({_sql_values(FailureClass)})",
            name="ck_ai_test_jobs_failure_class",
        ),
        CheckConstraint("prepare_attempt >= 0", name="ck_ai_test_jobs_prepare_attempt"),
        CheckConstraint("verify_attempt >= 0", name="ck_ai_test_jobs_verify_attempt"),
        CheckConstraint("version >= 0", name="ck_ai_test_jobs_version"),
        CheckConstraint("length(base_sha) = 40", name="ck_ai_test_jobs_base_sha"),
        CheckConstraint(
            "code_sha IS NULL OR length(code_sha) = 40",
            name="ck_ai_test_jobs_code_sha",
        ),
        CheckConstraint(
            "tested_sha IS NULL OR length(tested_sha) = 40",
            name="ck_ai_test_jobs_tested_sha",
        ),
        CheckConstraint("length(plan_digest) = 64", name="ck_ai_test_jobs_plan_digest"),
        Index("ix_ai_test_jobs_task_updated", "task_id", "updated_at"),
        Index("ix_ai_test_jobs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    repository: Mapped[str] = mapped_column(String(300), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    code_sha: Mapped[str | None] = mapped_column(String(40))
    tested_sha: Mapped[str | None] = mapped_column(String(40))
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    callback_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    prepare_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    verify_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latest_k8s_job_name: Mapped[str | None] = mapped_column(String(253))
    failure_class: Mapped[str | None] = mapped_column(String(32))
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class VerifyAttemptRow(Base):
    __tablename__ = "ai_test_verify_attempts"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "code_sha", name="uq_ai_test_verify_attempts_job_sha"
        ),
        UniqueConstraint("delivery_id", name="uq_ai_test_verify_attempts_delivery"),
        UniqueConstraint("job_id", "attempt", name="uq_ai_test_verify_attempts_number"),
        CheckConstraint("attempt > 0", name="ck_ai_test_verify_attempts_number"),
        CheckConstraint(
            "length(code_sha) = 40", name="ck_ai_test_verify_attempts_code_sha"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ai_test_jobs.id", ondelete="CASCADE"), nullable=False
    )
    code_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TestJobEventRow(Base):
    __tablename__ = "ai_test_job_events"
    __table_args__ = (
        CheckConstraint(
            f"phase IN ({_sql_values(EventPhase)})", name="ck_ai_test_job_events_phase"
        ),
        CheckConstraint(
            f"from_status IN ({_sql_values(JobStatus)})",
            name="ck_ai_test_job_events_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_sql_values(JobStatus)})",
            name="ck_ai_test_job_events_to_status",
        ),
        CheckConstraint("attempt > 0", name="ck_ai_test_job_events_attempt"),
        CheckConstraint(
            "code_sha IS NULL OR length(code_sha) = 40",
            name="ck_ai_test_job_events_code_sha",
        ),
        Index("ix_ai_test_job_events_job_created", "job_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ai_test_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    code_sha: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TestArtifactRow(Base):
    __tablename__ = "ai_test_artifacts"
    __table_args__ = (
        CheckConstraint(
            f"type IN ({_sql_values(ArtifactType)})", name="ck_ai_test_artifacts_type"
        ),
        CheckConstraint("attempt > 0", name="ck_ai_test_artifacts_attempt"),
        CheckConstraint("size_bytes >= 0", name="ck_ai_test_artifacts_size"),
        CheckConstraint("length(sha256) = 64", name="ck_ai_test_artifacts_sha256"),
        UniqueConstraint("object_key", name="uq_ai_test_artifacts_object_key"),
        Index("ix_ai_test_artifacts_job_attempt", "job_id", "attempt"),
        Index("ix_ai_test_artifacts_purged", "purged_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ai_test_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when retention removes the stored bytes. The row itself is kept so
    # the digest of what was tested stays auditable after the blob is gone.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKeyRow(Base):
    __tablename__ = "ai_test_idempotency_keys"
    __table_args__ = (
        CheckConstraint(
            "length(request_sha256) = 64", name="ck_ai_test_idempotency_keys_digest"
        ),
    )

    scope: Mapped[str] = mapped_column(String(50), primary_key=True)
    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationRow(Base):
    __tablename__ = "ai_test_notifications"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_values(NotificationStatus)})",
            name="ck_ai_test_notifications_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_ai_test_notifications_attempts"),
        UniqueConstraint(
            "job_id", "event_type", name="uq_ai_test_notifications_job_event"
        ),
        Index("ix_ai_test_notifications_pending", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("ai_test_jobs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    destination: Mapped[str] = mapped_column(String(2048), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
