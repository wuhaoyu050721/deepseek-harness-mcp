"""准备阶段入口；已知凭证仅经私有 stdin 内存传入，绝不回显或落盘。"""

import json
from pathlib import Path
import sys

from .config import ProjectConfig
from .errors import error_payload
from .workspace import create_snapshot


def main() -> None:
    stage = "snapshot_prepare"
    try:
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("request too large")
        request = json.loads(raw)
        if request.get("preflight_only", False):
            stage = "preflight"
        fields = request["project"]
        project = ProjectConfig(fields["id"], Path(fields["path"]), tuple(fields["allowed_paths"]), tuple(fields["exclude_patterns"]), tuple(fields["allow_untracked"]))
        snapshot = create_snapshot(project, request["relative_paths"], Path(request["destination"]), max_file_bytes=request["max_file_bytes"], max_total_bytes=request["max_snapshot_bytes"], scan_sources=request.get("scan_sources", True), known_secrets=request.get("known_secrets", ()), preflight_only=request.get("preflight_only", False))
        if request.get("preflight_only", False):
            result = {"ok": True, "preflight": snapshot}
        elif request.get("mode", "review") == "edit":
            if not fields.get("allow_edits", False):
                raise ValueError("edit permission absent")
            from .edit_workspace import prepare_edit_workspace
            prepare_edit_workspace(snapshot, Path(request["workspace_destination"]))
            result = {"ok":True,"snapshot":snapshot.to_dict()}
        else:
            result = {"ok":True,"snapshot":snapshot.to_dict()}
    except Exception as error:
        result = {"ok":False,"error":error_payload(error, stage=stage)}
    sys.stdout.buffer.write((json.dumps(result,ensure_ascii=False)+"\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

