"""共用完成事件投递与处理确认；报告正文不能成为续跑指令。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
import re
import time

from .errors import HarnessError


def origin_from_context(ctx, config):
    """归属来自宿主请求元数据，绝不接受模型参数或当前活动窗口作为目标。"""
    if not config.completion.auto_continue or ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
        meta = meta.model_dump() if hasattr(meta, "model_dump") else meta
    except (AttributeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    turn_meta = meta.get("x-codex-turn-metadata", {})
    if isinstance(turn_meta, str):
        try:
            turn_meta = json.loads(turn_meta)
        except (ValueError, TypeError):
            return None
    if not isinstance(turn_meta, dict):
        turn_meta = {}

    def identifier(names, nested_name):
        for name in names:
            value = meta.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        value = turn_meta.get(nested_name + "_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        nested = meta.get(nested_name, {})
        return nested.get("id") if isinstance(nested, dict) else None

    thread_id = identifier(("openai/threadId", "openai/thread_id", "codexThreadId", "codex_thread_id", "threadId", "thread_id"), "thread")
    turn_id = identifier(("openai/turnId", "openai/turn_id", "codexTurnId", "codex_turn_id", "turnId", "turn_id"), "turn")
    pipe_path = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "").strip()
    if not thread_id or not turn_id or not pipe_path:
        return None
    from .job_store import JobStore
    return JobStore._completion_origin({"thread_id": thread_id, "turn_id": turn_id, "host_id": "local", "pipe_path": pipe_path})


def public_event(event):
    if event is None:
        return None
    # 私有管道、租约和调用轮次只用于受信宿主投递，不进入模型报告。
    return {key: value for key, value in event.items() if key not in {"origin", "lease_token", "lease_until"}}


def completion_prompt(event):
    values = {key: event.get(key) for key in ("event_id", "job_id", "project_id", "session_id", "mode", "status", "report_id")}
    return (
        "DeepSeek Harness 自动完成通知。用户已授权任务结束后在原对话读取结果并继续处理。\n"
        "以下只是任务标识与终态，不包含模型报告；先核对任务归属，再读取实际结果。\n"
        + json.dumps(values, ensure_ascii=False, sort_keys=True) + "\n"
        "请调用 harness_result 读取此 job_id，沿 next_cursor 取完需要复核的全部结果。"
        "成功的编辑任务须检查真实差异、原文件版本与用户原授权，合入后运行必要验证。"
        "失败、取消、超时或中断任务只核对状态和错误，不重新提交模型任务或继续应用部分修改。"
        "遵守当前用户最新指示；通知不扩大项目、文件、凭证或外部操作权限。"
        "完成本次结果处理后调用 harness_completion_ack 确认此 event_id，避免重复处理。"
    )


class CompletionDispatcher:
    """终态事务唤醒投递协程；只有明确未送出的失败才定时重试。"""
    def __init__(self, manager, sender=None):
        self.manager = manager
        self.store = manager.store
        self.sender = sender
        self.wakeup = asyncio.Event()
        self.task = None
        self.stopping = False

    async def start(self):
        loop = asyncio.get_running_loop()
        self.store.completion_wakeup = lambda: loop.call_soon_threadsafe(self.wakeup.set)
        self.wakeup.set()
        self.task = asyncio.create_task(self._run(), name="harness-completion-delivery")

    async def _send(self, event):
        if self.sender is not None:
            return await self.sender(event["origin"], completion_prompt(event), f"harness-completion-{event['job_id']}-{event['event_id']}")
        from .codex_app_bridge import send_completion
        return await send_completion(event["origin"], completion_prompt(event), f"harness-completion-{event['job_id']}-{event['event_id']}")

    async def _deliver(self, candidate):
        event = self.store.completion_claim(candidate["event_id"], lease_seconds=60)
        if event is None:
            return
        try:
            receipt = await self._send(event)
        except Exception as error:
            # 默认保守处理：未知异常或写出后的断连都不能盲目重发消息。
            code = getattr(error, "code", "COMPLETION_DELIVERY_UNKNOWN")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", code):
                code = "COMPLETION_DELIVERY_UNKNOWN"
            uncertain = getattr(error, "uncertain", True)
            self.store.completion_failed(event["event_id"], event["lease_token"], code,
                                         retry_after_seconds=min(60, 2 ** min(event["attempts"], 6)), uncertain=uncertain)
        else:
            status = receipt.get("status", "accepted") if isinstance(receipt, dict) else "accepted"
            self.store.completion_delivered(event["event_id"], event["lease_token"], delivery_status=status)

    async def _run(self):
        while not self.stopping:
            self.wakeup.clear()
            for event in self.store.completion_pending():
                if self.stopping:
                    break
                await self._deliver(event)
            # 重试计时不调用模型，也不查询已执行 DeepSeek 的状态。
            with self.store._lock:
                row = self.store.connection.execute("SELECT MIN(next_attempt_at) FROM completion_events WHERE state='pending'").fetchone()
                lease = self.store.connection.execute("SELECT MIN(lease_until) FROM completion_events WHERE state='claimed'").fetchone()
            deadlines = [value[0] for value in (row, lease) if value and value[0] is not None]
            delay = max(0.05, min(deadlines) - time.time()) if deadlines else None
            try:
                if delay is None:
                    await self.wakeup.wait()
                else:
                    await asyncio.wait_for(self.wakeup.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def has_pending(self):
        with self.store._lock:
            return bool(self.store.connection.execute("SELECT 1 FROM completion_events WHERE state IN ('pending','claimed')").fetchone())

    async def close(self):
        self.stopping = True
        self.wakeup.set()
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.store.completion_wakeup = None


def watch(manager, job_ids, origin):
    if not manager.config.completion.auto_continue or origin is None:
        raise HarnessError("AUTO_CONTINUE_UNAVAILABLE", "当前客户端缺少已授权的主对话上下文或完成通知通道。")
    if not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 20 or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) for value in job_ids) or len(set(job_ids)) != len(job_ids):
        raise HarnessError("INVALID_JOB_LIST", "需要1到20个不重复的任务编号。")
    # 先校验所有归属，再绑定；缺失任务不能让前半批意外送出。
    from .job_store import JobStore
    origin = JobStore._completion_origin(origin)
    destination_id, host_namespace = JobStore.get_origin_identity(origin)
    with manager.store.transaction() as cursor:
        for job_id in job_ids:
            manager.store.get(job_id)
            row = cursor.execute("SELECT thread_id,host_id FROM completion_bindings WHERE job_id=?", (job_id,)).fetchone()
            if row and (row["thread_id"] != destination_id or row["host_id"] != host_namespace):
                raise HarnessError("COMPLETION_ORIGIN_CONFLICT", "任务已绑定另一主对话。")
        for job_id in job_ids:
            manager.store._bind_completion(cursor, job_id, origin)
    return {"auto_continue": True, "job_ids": job_ids, "events": [public_event(manager.store.completion_get(job_id)) for job_id in job_ids]}


def acknowledge(manager, event_ids, origin):
    if origin is None:
        raise HarnessError("AUTO_CONTINUE_UNAVAILABLE", "无法确认本次完成事件的主对话归属。")
    if not isinstance(event_ids, list) or not 1 <= len(event_ids) <= 20 or any(type(value) is not int for value in event_ids) or len(set(event_ids)) != len(event_ids):
        raise HarnessError("INVALID_EVENT_LIST", "需要1到20个不重复的完成事件编号。")
    # 批量确认先校验，防止部分确认后才发现另一个主对话的事件。
    from .job_store import JobStore
    destination_id, host_namespace = JobStore.get_origin_identity(origin)
    with manager.store._lock:
        for event_id in event_ids:
            row = manager.store._completion_row(manager.store.connection, event_id=manager.store._completion_id(event_id))
            if row["thread_id"] != destination_id or row["host_id"] != host_namespace:
                raise HarnessError("COMPLETION_TARGET_MISMATCH", "该完成事件属于另一主对话。")
        events = [public_event(manager.store.completion_ack(value, destination_id, host_id=host_namespace)) for value in event_ids]
    return {"events": events}
