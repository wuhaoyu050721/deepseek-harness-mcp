"""诊断只连接已经存在的认证后台；绝不启动后台或模型。"""

import asyncio

from .shared_bootstrap import _validated_registry, configuration_digest, read_private_json
from .shared_client import open_authenticated, read_frame, write_frame


async def inspect_existing_service(config) -> dict:
    local = configuration_digest(config)
    result = {"state": "not_observed", "local_runtime_version": local,
              "runtime_version": None, "restart_required": None,
              "active_connections": None, "active_tasks": None, "queued_tasks": None}
    path = config.data_dir / "shared-service" / "registry.json"
    if not path.exists():
        return result
    try:
        registry = _validated_registry(read_private_json(path), config)
    except Exception:
        return {**result, "state": "invalid_registry"}
    writer = None
    try:
        reader, writer = await open_authenticated(registry)
        await write_frame(writer, {"version": 1, "model_environment": {}})
        ready = await read_frame(reader)
        if ready.get("ready") is not True:
            return {**result, "state": "attach_failed"}
        result.update(state="connected", runtime_version=registry["config_digest"],
                      restart_required=local != registry["config_digest"])
        await write_frame(writer, {"id": "doctor-service-info", "method": "service_info", "args": {}})
        reply = await read_frame(reader, max_bytes=16 * 1024)
        if reply.get("ok") is not True:
            return {**result, "state": "connected_legacy", "counts_available": False}
        info = reply.get("result")
        if not isinstance(info, dict):
            return {**result, "state": "invalid_response"}
        # 诊断自己的临时连接包含在后台实时计数中，明确记录并扣除。
        for name in ("active_connections", "active_tasks", "queued_tasks"):
            value = info.get(name)
            if type(value) is int and value >= 0:
                result[name] = max(0, value - 1) if name == "active_connections" else value
        result["counts_available"] = all(result[name] is not None for name in ("active_connections", "active_tasks", "queued_tasks"))
        result["diagnostic_connection_excluded"] = True
        result["config_state"] = info.get("config_state") if info.get("config_state") in {"current", "changed", "invalid", "unknown"} else "unknown"
        return result
    except Exception:
        return {**result, "state": "unavailable"}
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def service_diagnostics(config) -> dict:
    async def bounded():
        try:
            return await asyncio.wait_for(inspect_existing_service(config), 5)
        except asyncio.TimeoutError:
            return {"state": "timeout", "restart_required": None,
                    "active_connections": None, "active_tasks": None, "queued_tasks": None}
    return asyncio.run(bounded())
