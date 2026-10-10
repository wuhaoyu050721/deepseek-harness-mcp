"""只公开不含配置内容的版本指纹；旧连接可观察磁盘配置漂移。"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .config import load_config


def config_version(config) -> str:
    # 配置只含凭证引用；不读取实际凭证，也不公开路径或模型路由。
    raw = json.dumps(asdict(config), default=str, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def configuration_state(config, config_path: Path | None = None) -> dict:
    loaded = config_version(config)
    state = {"config_version": loaded, "disk_config_version": None,
             "config_state": "unknown", "restart_required": None}
    if config_path is None:
        return state
    try:
        disk = config_version(load_config(config_path))
    except Exception:
        return {**state, "config_state": "invalid"}
    changed = loaded != disk
    return {**state, "disk_config_version": disk,
            "config_state": "changed" if changed else "current", "restart_required": changed}


def poll_interval(job: dict, now: float) -> int:
    if job["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
        return 0
    if job["status"] == "cancelling" or job.get("phase") in {"collecting_changes", "finishing"}:
        return 2
    age = max(0, now - (job.get("started_at") or job.get("created_at") or now))
    return 2 if age < 10 else 5 if age < 30 else 10 if age < 120 else 20
