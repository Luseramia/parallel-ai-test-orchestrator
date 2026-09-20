"""Create durable completion notification outbox.

Revision ID: 0002_notifications
Revises: 0001_milestone_1
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_notifications"
down_revision: str | None = "0001_milestone_1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_test_notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("destination", sa.String(length=2048), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
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
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempts >= 0", name="ck_ai_test_notifications_attempts"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DELIVERED', 'DEAD')",
            name="ck_ai_test_notifications_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ai_test_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "event_type", name="uq_ai_test_notifications_job_event"
        ),
    )
    op.create_index(
        "ix_ai_test_notifications_pending",
        "ai_test_notifications",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_test_notifications_pending", table_name="ai_test_notifications"
    )
    op.drop_table("ai_test_notifications")

