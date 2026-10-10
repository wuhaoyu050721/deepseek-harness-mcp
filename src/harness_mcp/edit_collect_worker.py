"""停止模型后独立收集副本差异；受宿主进程树、取消和总期限监管。

固定凭证匹配值只通过匿名 stdin 临时传入，用于拒绝含秘密的改动；不持久化、
不作为命令行参数。stdout 只含有界差异或固定错误，原始异常不输出。
"""

import json
from pathlib import Path
import sys

from .errors import error_payload
from .workspace import Snapshot


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("request too large")
        request = json.loads(raw)
        from .edit_workspace import collect_edit_changes
        changes = collect_edit_changes(
            Snapshot.from_dict(request["snapshot"]), Path(request["workspace_dir"]),
            allowed_paths=tuple(request["allowed_paths"]), relative_paths=request["relative_paths"],
            exclude_patterns=tuple(request["exclude_patterns"]),
            max_file_bytes=request["max_file_bytes"], max_snapshot_bytes=request["max_snapshot_bytes"],
            max_output_bytes=request["max_output_bytes"], secrets=tuple(request.get("secrets", [])),
        )
        result = {"ok": True, "changes": changes}
    except Exception as error:
        result = {"ok": False, "error": error_payload(error, stage="changes_collect")}
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
