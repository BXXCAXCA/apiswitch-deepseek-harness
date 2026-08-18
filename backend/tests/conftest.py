import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB_PATH = Path(__file__).parent / "test_apiswitch.db"
if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()
os.environ["APISWITCH_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["APISWITCH_MASTER_KEY"] = "cU-mD4vAJMmm-gR87R3acjRkCMpSWxESn4S6-sQGD7o="
os.environ["APISWITCH_PLUGIN_MODE"] = "false"

from apiswitch.app import app  # noqa: E402
from apiswitch.db.base import Base  # noqa: E402
from apiswitch.db.bootstrap import init_database  # noqa: E402
from apiswitch.db.session import engine  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    init_database()
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.fixture()
def gateway_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/admin/tokens",
        json={"name": "pytest-gateway", "scopes": ["gateway:invoke"]},
    )
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}
