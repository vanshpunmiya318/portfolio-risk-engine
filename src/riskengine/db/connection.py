import sqlite3
from pathlib import Path


def get_read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    return get_connection(db_path, read_only=True)


def get_connection(
    db_path: str | Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    if read_only:
        database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
    else:
        connection = sqlite3.connect(db_path)

    connection.execute("PRAGMA foreign_keys = ON")
    return connection
