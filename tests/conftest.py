import pytest

from riskengine.db.connection import get_connection


@pytest.fixture
def db_connection():
    connections = []

    def _create_connection(db_path):
        connection = get_connection(db_path)
        connections.append(connection)
        return connection

    yield _create_connection

    for connection in connections:
        connection.close()



from pathlib import Path
import ast


def pytest_sessionstart(session):
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))