"""initial schema

Revision ID: 744685d9ddd2
Revises:
Create Date: 2026-07-21 16:08:14.871892
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "744685d9ddd2"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
