"""admin access principals, keys and sessions

Revision ID: 20260721_101215_admin_access
Revises: 91d7cfe41a2b
Create Date: 2026-07-21 10:12:15+00:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260721_101215_admin_access"
down_revision: str | Sequence[str] | None = "91d7cfe41a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
