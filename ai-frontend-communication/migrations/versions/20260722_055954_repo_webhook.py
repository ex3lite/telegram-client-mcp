"""repository webhook auto sync

Revision ID: 20260722_055954_repo_webhook
Revises: 20260722_043616_agent_memory
Create Date: 2026-07-22 05:59:54
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260722_055954_repo_webhook"
down_revision: str | Sequence[str] | None = "20260722_043616_agent_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
