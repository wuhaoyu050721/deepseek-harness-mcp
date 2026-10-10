"""官方 Harness SDK 适配器；桌面进程树/容器生命周期由外层执行器负责。

此模块不读取用户 Harness home，不接受动态插件/patch/命令，不保留 reasoning
或原始通知。SDK 自身请求超时只限制回执，不能代替外层整个容器的任务期限。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import time
from typing import Callable

from .errors import HarnessError, error_payload


SDK_VERSION = "0.1.5rc1"
PROFILE = "sdk-minimal"
PRIVACY_PATCH = """# 固定调用层：关闭额外会话/插件清单上传和自动标题调用。
- id: session-log-deepseek
  disabled: true
  config:
    enabled: false
- id: plugin-package-inventory-deepseek
  disabled: true
  config:
    enabled: false
- id: session-title
  disabled: true
"""


class AdapterProtocolError(RuntimeError):
    """通知违反本适配器依赖的官方活动边界。"""


@dataclass
class ActivityCollector:
    """只保留根会话最后一条 assistant 文本；子任务不能覆盖最终回复。"""

    session_id: str
    max_output_bytes: int
    received: bool = False
    final_text: str = ""
    finish_reason: str | None = None
    output_limited: bool = False

    def accept(self, notification: object, message_id: str) -> bool:
        method = getattr(notification, "method", None)
        payload = getattr(notification, "payload", None)
        if not isinstance(payload, dict) or payload.get("sessionId") != self.session_id:
            return False
        event = payload.get("event")
        if not self.received:
            if method != "session.event" or not isinstance(event, dict):
                return False
            data = event.get("data")
            inserted = data.get("inserted") if isinstance(data, dict) else None
            self.received = event.get("type") == "agent/inbox/spliced" and isinstance(inserted, list) and any(
                isinstance(item, dict) and item.get("id") == message_id for item in inserted
            )
            if not self.received:
                return False
        if method == "session.event" and isinstance(event, dict):
            data = event.get("data")
            if event.get("type") == "assistant/message" and isinstance(data, dict):
                message = data.get("message")
                owner = message if isinstance(message, dict) else data
                content = owner.get("content")
                if isinstance(content, list):
                    # 只筛选 text 块；reasoning、tool-call 及其他 provider 数据立即丢弃。
                    parts: list[str] = []
                    remaining = self.max_output_bytes
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        text = block.get("text")
                        if not isinstance(text, str):
                            continue
                        # 每次只编码最多 cap+1 个字符，避免对超大文本额外复制整份字节。
                        raw = text[: remaining + 1].encode("utf-8")
                        if len(raw) > remaining or len(text) > remaining + 1:
                            self.output_limited = True
                        bounded = raw[:remaining].decode("utf-8", errors="ignore")
                        parts.append(bounded)
                        remaining -= len(bounded.encode("utf-8"))
                    self.final_text = "".join(parts)
            elif event.get("type") == "turn/end":
                reason = data.get("reason") if isinstance(data, dict) else None
                kind = reason.get("kind") if isinstance(reason, dict) else None
                if not isinstance(kind, str) or not kind:
                    raise AdapterProtocolError("turn/end requires a non-empty reason kind")
                self.finish_reason = kind
        return method == "session.status" and payload.get("status") == "idle"


def write_privacy_patch(home_dir: Path) -> Path:
    """仅在本作业专用 home 写入固定补丁，不复制个人配置。"""
    home_dir.mkdir(parents=True, exist_ok=True)
    patch_path = home_dir / "mcp-privacy.patch.yml"
    patch_path.write_text(PRIVACY_PATCH, encoding="utf-8")
    return patch_path


def _outcome_error(collector: ActivityCollector) -> dict | None:
    if collector.output_limited:
        return {"code": "OUTPUT_LIMIT", "message": "模型回复超过配置的报告大小上限。"}
    if collector.finish_reason == "max-tokens":
        return {"code": "MAX_TOKENS", "message": "模型达到输出 token 上限，审查结果不完整。"}
    if collector.finish_reason == "error":
        return {"code": "MODEL_ERROR", "message": "Harness 以 error 原因结束审查。"}
    if collector.finish_reason != "completed":
        return {"code": "INCOMPLETE_RUN", "message": "Harness 未确认本次审查完整完成。"}
    if not collector.final_text.strip():
        return {"code": "EMPTY_OUTPUT", "message": "Harness 已结束，但没有最终审查文本。"}
    return None


def _safe_error(error: Exception) -> dict:
    # 官方异常可能带 stderr、HTTP 文本或环境变量。只给固定消息，不序列化 str(error)。
    payload = error_payload(error, stage="model_execute")
    if isinstance(error, TimeoutError):
        payload.update(code="SDK_REQUEST_TIMEOUT", message="Harness SDK 回执超时。")
    elif isinstance(error, (ImportError, ModuleNotFoundError)):
        payload.update(code="SDK_UNAVAILABLE", message="Worker 未安装锁定的 Harness SDK。")
    elif isinstance(error, AdapterProtocolError):
        payload.update(code="SDK_PROTOCOL_ERROR", message="Harness 通知未满足活动边界协议。")
    else:
        from .desktop_launcher import DesktopLaunchError
        if not isinstance(error, (HarnessError, DesktopLaunchError)):
            payload.update(code="HARNESS_RUNTIME_ERROR", message="Harness 运行失败；原始诊断未进入报告。")
    return payload


def run_harness(
    request: dict,
    snapshot_dir: Path,
    home_dir: Path,
    on_activity: Callable[[str], None] | None = None,
) -> dict:
    """运行一个审查或修改活动；输入来自宿主冻结的配置，不直接暴露给调用端。

    外层必须先以允许列表建立 Worker 环境并监管整个进程树/容器。关闭 SDK 只
    负责其直接 runtime，执行器仍须确认整个执行器停止后才能发布最终状态。
    """
    started = time.monotonic()
    collector: ActivityCollector | None = None
    client = None
    result = {"raw_text": "", "finish_reason": None, "usage": None, "error": None, "timing": {}}
    try:
        mode = request.get("mode", "review")
        if not isinstance(mode, str) or mode not in {"review", "edit"}:
            raise HarnessError("INVALID_MODE", "执行模式必须为 review 或 edit。")
        execution = request.get("executor", {})
        backend = execution.get("backend", "docker")
        if mode == "edit" and backend != "desktop":
            raise HarnessError("EXECUTOR_MODE_UNSUPPORTED", "当前执行器不支持修改模式。")
        model = request["model"]
        provider, model_name = model["provider"], model["model"]
        prompt, job_id = request["prompt"], request["job_id"]
        if not all(isinstance(item, str) and item.strip() for item in (provider, model_name, prompt, job_id)):
            raise ValueError("missing execution configuration")
        credential_env = model.get("credential_env", "DEEPSEEK_API_KEY")
        if not isinstance(credential_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", credential_env):
            raise ValueError("invalid credential reference")
        api_key = os.environ.get(credential_env)
        if not api_key:
            result["error"] = {"code": "CREDENTIAL_UNAVAILABLE", "message": "Worker 环境缺少所配置的模型凭证。"}
            return result
        max_output_bytes = int(request.get("limits", {}).get("max_output_bytes", 2 * 1024 * 1024))
        if not 1 <= max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("invalid output limit")
        snapshot_dir, home_dir = snapshot_dir.resolve(), home_dir.resolve()
        workspace_dir = snapshot_dir
        if mode == "edit":
            workspace_value = request.get("workspace_dir")
            if not isinstance(workspace_value, str) or not Path(workspace_value).is_absolute():
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改模式需要独立工作副本的绝对路径。")
            workspace_dir = Path(workspace_value).resolve()
            if workspace_dir == snapshot_dir or workspace_dir.is_relative_to(snapshot_dir) or snapshot_dir.is_relative_to(workspace_dir):
                raise HarnessError("INVALID_EXECUTOR_PATH", "修改工作副本不能与基线快照重叠。")
        if not snapshot_dir.is_dir() or not workspace_dir.is_dir():
            raise HarnessError("INVALID_EXECUTOR_PATH", "执行代码目录不存在。")
        if any(home_dir == directory or home_dir.is_relative_to(directory) or directory.is_relative_to(home_dir) for directory in (snapshot_dir, workspace_dir)):
            raise HarnessError("INVALID_EXECUTOR_PATH", "专用 home 必须与代码目录分开。")
        collector = ActivityCollector(job_id, max_output_bytes)
        patch_path = write_privacy_patch(home_dir)
        # Worker 的依赖与 MCP 宿主分开安装；宿主导入本模块不会启动/安装 runtime。
        from deepseek_harness import HarnessClient, HarnessConfig

        environment = {"DEEPSEEK_API_KEY": api_key}
        base_url = model.get("base_url")
        if base_url:
            environment["DEEPSEEK_BASE_URL"] = base_url
        config = HarnessConfig(
            profile=PROFILE, dsh_home=str(home_dir), cwd=str(workspace_dir),
            patches=(str(patch_path),), env=environment,
            initialize_timeout_seconds=30.0, request_timeout_seconds=30.0,
            shutdown_timeout_seconds=1.0,
        )
        if backend == "desktop":
            from .desktop_launcher import create_desktop_client
            client = create_desktop_client(config, execution.get("desktop_root"), HarnessClient)
        elif backend == "docker":
            client = HarnessClient(config)
        else:
            from .desktop_launcher import DesktopLaunchError
            raise DesktopLaunchError("EXECUTOR_BACKEND_INVALID", "执行器类型未获本机配置授权。")
        if on_activity:
            on_activity("initializing")
        client.start()
        client.initialize(cwd=str(workspace_dir), provider=provider, model=model_name)
        result["timing"]["initialization_seconds"] = round(time.monotonic() - started, 3)
        with client.subscribe_session_notifications(job_id) as subscription:
            message_id = client.session_prompt(
                job_id, [{"type": "text", "text": prompt}], notification_subscription=subscription,
            )
            activity = "editing" if mode == "edit" else "reviewing"
            if on_activity:
                on_activity(activity)
            while True:
                notification = subscription.next()
                if on_activity:
                    on_activity(activity)
                if collector.accept(notification, message_id):
                    break
        result.update(raw_text=collector.final_text, finish_reason=collector.finish_reason, error=_outcome_error(collector))
    except Exception as error:
        result["error"] = _safe_error(error)
        if collector:
            result.update(raw_text=collector.final_text, finish_reason=collector.finish_reason)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                result["error"] = {"code": "SDK_CLOSE_FAILED", "message": "SDK 关闭失败，需由执行器确认整个执行器停止。"}
        result["timing"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # SDK 未公开经验证的 usage 汇总字段。保留 None，不以本地估计冒充 provider 计费。
    return result
