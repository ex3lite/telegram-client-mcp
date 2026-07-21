import re
from datetime import datetime
from pathlib import Path

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
