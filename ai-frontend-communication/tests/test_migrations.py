import re
from pathlib import Path


def test_every_alembic_revision_has_transaction_free_sql_pair() -> None:
    root = Path(__file__).parents[1] / "migrations"
    revisions = {path.name.split("_", 1)[0] for path in (root / "versions").glob("[!_]*.py")}
    up = {path.name.removesuffix(".up.sql") for path in (root / "sql").glob("*.up.sql")}
    down = {path.name.removesuffix(".down.sql") for path in (root / "sql").glob("*.down.sql")}

    assert revisions == up == down
    for path in (root / "sql").glob("*.sql"):
        sql = path.read_text().strip()
        assert sql
        assert "alembic_version" not in sql.lower()
        assert re.search(r"(?im)^\s*(begin|commit)\s*;\s*$", sql) is None
