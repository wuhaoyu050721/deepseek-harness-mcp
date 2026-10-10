"""现有后台诊断不启动服务、模型或读取实际凭证。"""
import asyncio
from unittest.mock import AsyncMock

from harness_mcp.config import AppConfig
from harness_mcp import service_diagnostics as diagnostics


class Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def setup(tmp_path, monkeypatch):
    config = AppConfig(tmp_path / "state")
    path = config.data_dir / "shared-service/registry.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    registry = {"config_digest": "a" * 64}
    monkeypatch.setattr(diagnostics, "configuration_digest", lambda config: "b" * 64)
    monkeypatch.setattr(diagnostics, "read_private_json", lambda path: registry)
    monkeypatch.setattr(diagnostics, "_validated_registry", lambda value, config: value)
    writer = Writer()
    monkeypatch.setattr(diagnostics, "open_authenticated", AsyncMock(return_value=(object(), writer)))
    sent = AsyncMock()
    monkeypatch.setattr(diagnostics, "write_frame", sent)
    return config, writer, sent


def test_existing_backend_counts_exclude_temporary_diagnostic_connection(tmp_path, monkeypatch):
    config, writer, sent = setup(tmp_path, monkeypatch)
    read = AsyncMock(side_effect=[{"ready":True}, {"ok":True,"result": {
        "active_connections":3,"active_tasks":2,"queued_tasks":1,"config_state":"changed",
        "unknown": "private-server-field"}}])
    monkeypatch.setattr(diagnostics, "read_frame", read)
    result = asyncio.run(diagnostics.inspect_existing_service(config))
    assert result["active_connections"] == 2 and result["active_tasks"] == 2 and result["queued_tasks"] == 1
    assert result["restart_required"] is True and result["counts_available"] is True
    assert result["config_state"] == "changed" and writer.closed
    assert "unknown" not in result
    frames = [call.args[1] for call in sent.await_args_list]
    assert frames == [{"version":1,"model_environment":{}}, {"id":"doctor-service-info","method":"service_info","args":{}}]


def test_legacy_backend_reports_unknown_counts(tmp_path, monkeypatch):
    config, writer, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnostics, "read_frame", AsyncMock(side_effect=[{"ready":True}, {"ok":False,"error":{}}]))
    result = asyncio.run(diagnostics.inspect_existing_service(config))
    assert result["state"] == "connected_legacy" and result["restart_required"] is True
    assert result["active_connections"] is None and result["active_tasks"] is None
    assert result["counts_available"] is False and writer.closed


def test_missing_registry_does_not_connect(tmp_path, monkeypatch):
    config = AppConfig(tmp_path / "missing")
    monkeypatch.setattr(diagnostics, "configuration_digest", lambda config: "b" * 64)
    opened = AsyncMock()
    monkeypatch.setattr(diagnostics, "open_authenticated", opened)
    result = asyncio.run(diagnostics.inspect_existing_service(config))
    assert result["state"] == "not_observed" and result["restart_required"] is None
    opened.assert_not_called()


def test_unavailable_existing_backend_does_not_claim_config_change(tmp_path, monkeypatch):
    config, _, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnostics, "open_authenticated", AsyncMock(side_effect=OSError("private-value")))
    result = asyncio.run(diagnostics.inspect_existing_service(config))
    assert result["state"] == "unavailable" and result["restart_required"] is None
    assert "private-value" not in str(result)
