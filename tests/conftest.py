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