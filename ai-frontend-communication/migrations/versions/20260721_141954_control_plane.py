"""project agent control plane and managed credentials

Revision ID: 20260721_141954_control_plane
Revises: 20260721_101215_admin_access
Create Date: 2026-07-21 14:19:54+00:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260721_141954_control_plane"
down_revision: str | Sequence[str] | None = "20260721_101215_admin_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql(direction: str) -> str:
    return (Path(__file__).parents[1] / "sql" / f"{revision}.{direction}.sql").read_text()


def upgrade() -> None:
    op.execute(_sql("up"))


def downgrade() -> None:
    op.execute(_sql("down"))
