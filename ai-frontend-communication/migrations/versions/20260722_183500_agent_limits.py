"""expand Claude timeout and conversation context limits

Revision ID: 20260722_183500_agent_limits
Revises: 20260722_163207_02_context
Create Date: 2026-07-22 18:35:00+08:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260722_183500_agent_limits"
down_revision: str | Sequence[str] | None = "20260722_163207_02_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
