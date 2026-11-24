from contextlib import closing
from pathlib import Path

from riskengine.db.connection import get_connection


def schema_sql() -> str:
    schema_dir = Path(__file__).parent
    return "\n".join(
        (schema_dir / filename).read_text(encoding="utf-8")
        for filename in ("instruments.sql", "portfolios.sql", "prices.sql")
    )


def create_schema(db_path: str | Path) -> None:
    with closing(get_connection(db_path)) as connection, connection:
        connection.executescript("BEGIN;\n" + schema_sql())
