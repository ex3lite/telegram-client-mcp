"""member profiles

Revision ID: 91d7cfe41a2b
Revises: 744685d9ddd2
Create Date: 2026-07-21 19:30:00.000000
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "91d7cfe41a2b"
down_revision: str | Sequence[str] | None = "744685d9ddd2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
