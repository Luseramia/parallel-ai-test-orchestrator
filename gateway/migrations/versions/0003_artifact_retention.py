"""Record when artifact bytes are removed by retention.

Revision ID: 0003_retention
Revises: 0002_notifications
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_retention"
down_revision: str | None = "0002_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_test_artifacts",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The reconciler scans for unpurged artifacts of long-finished jobs.
    op.create_index(
        "ix_ai_test_artifacts_purged",
        "ai_test_artifacts",
        ["purged_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_test_artifacts_purged", table_name="ai_test_artifacts")
    op.drop_column("ai_test_artifacts", "purged_at")
