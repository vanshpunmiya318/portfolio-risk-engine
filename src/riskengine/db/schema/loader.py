from contextlib import closing
from pathlib import Path

from riskengine.db.connection import get_connection


def create_schema(db_path: str | Path) -> None:
    schema_dir = Path(__file__).parent

    with closing(get_connection(db_path)) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )