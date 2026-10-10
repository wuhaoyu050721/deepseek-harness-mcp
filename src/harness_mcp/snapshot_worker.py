"""准备阶段独立进程入口；输入只包含冻结的授权项目配置，不包含凭证。"""

import json
from pathlib import Path
import sys

from .config import ProjectConfig
from .errors import error_payload
from .workspace import create_snapshot


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("request too large")
        request = json.loads(raw)
        fields = request["project"]
        project = ProjectConfig(fields["id"], Path(fields["path"]), tuple(fields["allowed_paths"]), tuple(fields["exclude_patterns"]), tuple(fields["allow_untracked"]))
        snapshot = create_snapshot(project, request["relative_paths"], Path(request["destination"]), max_file_bytes=request["max_file_bytes"], max_total_bytes=request["max_snapshot_bytes"])
        if request.get("mode", "review") == "edit":
            if not fields.get("allow_edits", False):
                raise ValueError("edit permission absent")
            from .edit_workspace import prepare_edit_workspace
            prepare_edit_workspace(snapshot, Path(request["workspace_destination"]))
        result = {"ok":True,"snapshot":snapshot.to_dict()}
    except Exception as error:
        result = {"ok":False,"error":error_payload(error)}
    sys.stdout.buffer.write((json.dumps(result,ensure_ascii=False)+"\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

