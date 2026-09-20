"""Create Milestone 1 durable job storage.

Revision ID: 0001_milestone_1
Revises: None
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_milestone_1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "PREPARE_QUEUED",
    "PREPARING",
    "WAITING_FOR_CODE",
    "VERIFY_QUEUED",
    "GENERATING_TESTS",
    "TEST_QUEUED",
    "TESTING",
    "PASSED",
    "FAILED",
    "BLOCKED",
    "ERROR",
    "TIMEOUT",
    "SUPERSEDED",
    "CANCELLED",
)
FAILURE_CLASSES = (
    "TEST_FAILURE",
    "CONTRACT",
    "POLICY",
    "AGENT",
    "INFRASTRUCTURE",
    "TIMEOUT",
    "CANCELLED",
)
PHASES = ("PREPARE", "VERIFY", "GENERATE", "TEST", "SYSTEM")
ARTIFACT_TYPES = (
    "PLAN",
    "DRAFT",
    "PATCH",
    "CODEX_JSONL",
    "RAW_LOG",
    "JUNIT",
    "COVERAGE",
    "RESULT",
)


def _values(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "ai_test_jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=200), nullable=False),
        sa.Column("repository", sa.String(length=300), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=True),
        sa.Column("tested_sha", sa.String(length=40), nullable=True),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("callback_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("prepare_attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verify_attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latest_k8s_job_name", sa.String(length=253), nullable=True),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            f"status IN ({_values(STATUSES)})", name="ck_ai_test_jobs_status"
        ),
        sa.CheckConstraint(
            f"failure_class IS NULL OR failure_class IN ({_values(FAILURE_CLASSES)})",
            name="ck_ai_test_jobs_failure_class",
        ),
        sa.CheckConstraint(
            "prepare_attempt >= 0", name="ck_ai_test_jobs_prepare_attempt"
        ),
        sa.CheckConstraint(
            "verify_attempt >= 0", name="ck_ai_test_jobs_verify_attempt"
        ),
        sa.CheckConstraint("version >= 0", name="ck_ai_test_jobs_version"),
        sa.CheckConstraint("length(base_sha) = 40", name="ck_ai_test_jobs_base_sha"),
        sa.CheckConstraint(
            "code_sha IS NULL OR length(code_sha) = 40",
            name="ck_ai_test_jobs_code_sha",
        ),
        sa.CheckConstraint(
            "tested_sha IS NULL OR length(tested_sha) = 40",
            name="ck_ai_test_jobs_tested_sha",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64", name="ck_ai_test_jobs_plan_digest"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "base_sha", "plan_digest", name="uq_ai_test_jobs_prepare"
        ),
    )
    op.create_index("ix_ai_test_jobs_status", "ai_test_jobs", ["status"])
    op.create_index(
        "ix_ai_test_jobs_task_updated", "ai_test_jobs", ["task_id", "updated_at"]
    )

    op.create_table(
        "ai_test_verify_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt > 0", name="ck_ai_test_verify_attempts_number"),
        sa.CheckConstraint(
            "length(code_sha) = 40", name="ck_ai_test_verify_attempts_code_sha"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ai_test_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_ai_test_verify_attempts_delivery"),
        sa.UniqueConstraint(
            "job_id", "attempt", name="uq_ai_test_verify_attempts_number"
        ),
        sa.UniqueConstraint(
            "job_id", "code_sha", name="uq_ai_test_verify_attempts_job_sha"
        ),
    )

    op.create_table(
        "ai_test_job_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt > 0", name="ck_ai_test_job_events_attempt"),
        sa.CheckConstraint(
            "code_sha IS NULL OR length(code_sha) = 40",
            name="ck_ai_test_job_events_code_sha",
        ),
        sa.CheckConstraint(
            f"phase IN ({_values(PHASES)})", name="ck_ai_test_job_events_phase"
        ),
        sa.CheckConstraint(
            f"from_status IN ({_values(STATUSES)})",
            name="ck_ai_test_job_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_values(STATUSES)})",
            name="ck_ai_test_job_events_to_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ai_test_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_ai_test_job_events_job_created",
        "ai_test_job_events",
        ["job_id", "created_at"],
    )

    op.create_table(
        "ai_test_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt > 0", name="ck_ai_test_artifacts_attempt"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_ai_test_artifacts_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_ai_test_artifacts_sha256"),
        sa.CheckConstraint(
            f"type IN ({_values(ARTIFACT_TYPES)})", name="ck_ai_test_artifacts_type"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ai_test_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_ai_test_artifacts_object_key"),
    )
    op.create_index(
        "ix_ai_test_artifacts_job_attempt", "ai_test_artifacts", ["job_id", "attempt"]
    )

    op.create_table(
        "ai_test_idempotency_keys",
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("key", sa.String(length=500), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(request_sha256) = 64", name="ck_ai_test_idempotency_keys_digest"
        ),
        sa.PrimaryKeyConstraint("scope", "key"),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_ai_test_job_event_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'ai_test_job_events is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER ai_test_job_events_append_only
            BEFORE UPDATE OR DELETE ON ai_test_job_events
            FOR EACH ROW EXECUTE FUNCTION reject_ai_test_job_event_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS ai_test_job_events_append_only ON ai_test_job_events"
        )
        op.execute("DROP FUNCTION IF EXISTS reject_ai_test_job_event_mutation()")
    op.drop_table("ai_test_idempotency_keys")
    op.drop_index("ix_ai_test_artifacts_job_attempt", table_name="ai_test_artifacts")
    op.drop_table("ai_test_artifacts")
    op.drop_index("ix_ai_test_job_events_job_created", table_name="ai_test_job_events")
    op.drop_table("ai_test_job_events")
    op.drop_table("ai_test_verify_attempts")
    op.drop_index("ix_ai_test_jobs_task_updated", table_name="ai_test_jobs")
    op.drop_index("ix_ai_test_jobs_status", table_name="ai_test_jobs")
    op.drop_table("ai_test_jobs")
