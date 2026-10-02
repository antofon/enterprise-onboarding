"""runs carry a heartbeat so a run whose process stopped can be found

Nullable, no backfill: a finished run never needs one, and a run still `running` without one is
judged by its start time instead.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 17:22:55.188683
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "migration_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        schema="onboarding",
    )


def downgrade() -> None:
    op.drop_column("migration_runs", "heartbeat_at", schema="onboarding")
