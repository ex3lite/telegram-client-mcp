import re
from datetime import datetime
from pathlib import Path

from dca.db import ConversationThread, ProjectMembership

LEGACY_REVISIONS = {"744685d9ddd2", "91d7cfe41a2b"}
TIMESTAMPED_REVISION = re.compile(r"^(?P<timestamp>[0-9]{8}_[0-9]{6})_[a-z0-9][a-z0-9_-]*$")


def test_every_alembic_revision_has_transaction_free_sql_pair() -> None:
    root = Path(__file__).parents[1] / "migrations"
    revisions = set()
    timestamps: set[str] = set()
    for path in (root / "versions").glob("[!_]*.py"):
        match = re.search(r'^revision: str = "([^"]+)"$', path.read_text(), re.MULTILINE)
        assert match is not None
        revision = match.group(1)
        assert len(revision) <= 32
        assert path.name.startswith(revision)
        if revision not in LEGACY_REVISIONS:
            timestamped = TIMESTAMPED_REVISION.fullmatch(revision)
            assert timestamped is not None
            timestamp = timestamped.group("timestamp")
            datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
            assert timestamp not in timestamps
            assert path.name == f"{revision}.py"
            timestamps.add(timestamp)
        revisions.add(revision)
    up = {path.name.removesuffix(".up.sql") for path in (root / "sql").glob("*.up.sql")}
    down = {path.name.removesuffix(".down.sql") for path in (root / "sql").glob("*.down.sql")}

    assert revisions == up == down
    for path in (root / "sql").glob("*.sql"):
        sql = path.read_text().strip()
        assert sql
        assert "alembic_version" not in sql.lower()
        assert re.search(r"(?im)^\s*(begin|commit)\s*;\s*$", sql) is None


def test_claude_context_migration_defines_policy_and_session_state() -> None:
    root = Path(__file__).parents[1] / "migrations" / "sql"
    up = (root / "20260722_163207_02_context.up.sql").read_text()
    down = (root / "20260722_163207_02_context.down.sql").read_text()

    for column in (
        "knowledge_scope",
        "can_create_requests",
        "claude_session_id",
        "claude_repository_id",
        "claude_commit_sha",
        "claude_policy_hash",
        "claude_compaction_count",
        "claude_last_compacted_at",
        "claude_context_validated_at",
    ):
        assert f"ADD COLUMN {column}" in up
        assert f"DROP COLUMN {column}" in down
    assert "lower(trim(role)) IN ('owner', 'admin', 'backend_admin')" in up
    assert "lower(trim(coalesce(department, ''))) = 'backend'" in up
    assert "ON DELETE SET NULL" in up
    assert "claude_compaction_count >= 0" in up


def test_context_policy_orm_matches_database_contract() -> None:
    membership = ProjectMembership.__table__.c
    thread = ConversationThread.__table__.c

    assert membership.knowledge_scope.nullable is False
    assert membership.can_create_requests.nullable is False
    assert thread.claude_compaction_count.nullable is False
    assert thread.claude_policy_hash.type.length == 64
    repository_fk = next(iter(thread.claude_repository_id.foreign_keys))
    assert repository_fk.target_fullname == "repositories.id"
    assert repository_fk.ondelete == "SET NULL"
