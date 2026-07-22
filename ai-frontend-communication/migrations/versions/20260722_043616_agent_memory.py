"""durable scoped conversation memory

Revision ID: 20260722_043616_agent_memory
Revises: 20260721_141954_control_plane
Create Date: 2026-07-22 04:36:16+00:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260722_043616_agent_memory"
down_revision: str | Sequence[str] | None = "20260721_141954_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
