from pathlib import Path
from uuid import uuid4

from apiswitch.config import settings
from apiswitch.app import _admin_router_for_mode
from apiswitch.db.session import SessionLocal
from apiswitch.harness_runtime import HARNESS_TOKEN_NAME, ensure_harness_token


def test_plugin_mode_omits_client_and_agent_management_routes():
    original_mode = settings.plugin_mode
    settings.plugin_mode = True
    try:
        paths = {getattr(route, "path", "") for route in _admin_router_for_mode().routes}
        assert not any(path.startswith("/api/admin/tokens") for path in paths)
        assert not any(path.startswith("/api/admin/agents") for path in paths)
        assert not any(path.startswith("/api/admin/settings/startup") for path in paths)
        assert "/api/admin/provider-instances" in paths
        assert "/api/admin/runtime" in paths
    finally:
        settings.plugin_mode = original_mode


def test_managed_harness_token_automatically_sees_new_unified_models(client, tmp_path: Path):
    original_mode = settings.plugin_mode
    original_file = settings.harness_token_file
    settings.plugin_mode = True
    settings.harness_token_file = str(tmp_path / "harness.token")
    try:
        with SessionLocal() as db:
            row, plain = ensure_harness_token(db)
            assert row.name == HARNESS_TOKEN_NAME
            assert Path(settings.harness_token_file).read_text(encoding="utf-8").strip() == plain

        provider = client.post(
            "/api/admin/provider-instances",
            json={
                "name": f"harness-{uuid4().hex}",
                "template_key": "openai",
                "base_url": "mock://harness",
                "api_key": "unit-only-not-a-real-key",
            },
        ).json()
        upstream = client.post(
            f"/api/admin/provider-instances/{provider['id']}/upstream-models",
            json={
                "model_id": "deepseek-harness-test",
                "input_capabilities_json": ["text"],
                "output_capabilities_json": ["text"],
            },
        ).json()
        unified = client.post(
            "/api/admin/unified-models",
            json={"name": "deepseek-v4-pro", "enabled_protocols": ["openai_chat"]},
        ).json()
        client.post(
            f"/api/admin/unified-models/{unified['id']}/candidates",
            json={"upstream_model_id": upstream["id"]},
        )

        response = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["data"]] == ["deepseek-v4-pro"]
    finally:
        settings.plugin_mode = original_mode
        settings.harness_token_file = original_file
