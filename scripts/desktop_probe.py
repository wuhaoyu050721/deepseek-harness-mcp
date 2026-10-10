"""只调用已安装桌面端的版本和配置导出入口，不启动会话或模型。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import yaml


class InspectionLoader(yaml.SafeLoader):
    """把 Harness 的 JS 标签保留为普通数据，绝不执行表达式或导入插件。"""


def _tagged_data(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


InspectionLoader.add_constructor("tag:yaml.org,2002:js", _tagged_data)


def _selected_defaults(node, output):
    if isinstance(node, list):
        for child in node:
            _selected_defaults(child, output)
    elif isinstance(node, dict):
        name = node.get("id") or node.get("name")
        config = node.get("config") or {}
        selected = (
            "agent-default-model", "llm-deepseek", "llm-retry",
            "session-log-deepseek", "plugin-package-inventory-deepseek",
        )
        if isinstance(config, dict) and name and (
            name in selected or "permission" in str(name) or "shell" in str(name)
        ):
            output.append({
                "id": name, "name": node.get("name"),
                "config": {k: v for k, v in config.items() if k in (
                    "enabled", "provider", "model", "apiKeyEnv",
                    "maxRetries", "mode", "permission", "access", "retryPolicy",
                ) and isinstance(v, (str, bool, int))},
                "config_keys": list(config),
            })
        for child in node.values():
            if isinstance(child, (dict, list)):
                _selected_defaults(child, output)


def inspect_credential_presence(home: Path) -> dict:
    """仅返回固定引用的非空布尔值；不复制凭证或打印 YAML 错误源行。"""
    try:
        path = home / ".credentials.yaml"
        if path.stat().st_size > 1024 * 1024:
            return {"error": "CREDENTIAL_STORE_TOO_LARGE"}
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        refs = document.get("refs", {}) or {}
        records = document.get("records", {}) or {}
        grant = records.get("deepseek-account-platform/default", {}) or {}
        payload = grant.get("payload", {}) or {}
        return {
            "api_key_reference_nonempty": bool(refs.get("DEEPSEEK_API_KEY")),
            "account_grant_present": grant.get("kind") == "grant" and bool(payload.get("token")),
            "credential_values_copied": False,
            "credential_validity_verified": False,
        }
    except Exception as error:
        return {"error_type": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DeepSeek Harness")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect-credential-presence", action="store_true", help="读取本机凭证文件，仅记录固定引用非空状态")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    probe_home = root / "data" / "phase0-desktop-probe"
    probe_home.mkdir(parents=True, exist_ok=True)
    # 版本与导出调用使用全新 home；普通目录本身并非文件系统沙箱。
    environment = {k: os.environ[k] for k in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if k in os.environ}
    environment.update(ELECTRON_RUN_AS_NODE="1", DSH_HOME=str(probe_home))
    executable = args.install_root / "DeepSeek Harness.exe"
    entry = args.install_root / "resources" / "app.asar" / "dsh" / "node_modules" / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js"
    report = {
        "scope": "installed_desktop_metadata_only", "install_root": str(args.install_root),
        "uses_project_probe_home": True, "filesystem_sandboxed": False,
        "runtime_session_started": False, "model_called": False,
        "private_home_writes_requested": False,
        "private_home_modification_verified": False,
        "sdk_handshake_verified": False,
    }
    try:
        # 此参数顺序直接依据本机 dsh.cmd，而不是猜测桌面程序的 SDK 接口。
        prefix = [str(executable), "--expose-internals", str(entry)]
        version = subprocess.run(prefix + ["--version"], env=environment, cwd=probe_home, capture_output=True, timeout=15)
        report["version_exit_code"] = version.returncode
        version_text = version.stdout.decode("utf-8").strip()
        report["version"] = version_text if version.returncode == 0 and re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z]+)+(?:[+_-][0-9A-Za-z._-]+)?", version_text) and len(version_text) <= 64 else None
        dump = subprocess.run(prefix + ["--profile", "sdk-minimal", "--dump-default-config"], env=environment, cwd=probe_home, capture_output=True, timeout=15)
        report["config_dump_exit_code"] = dump.returncode
        if dump.returncode == 0 and len(dump.stdout) <= 2 * 1024 * 1024:
            document = yaml.load(dump.stdout.decode("utf-8"), Loader=InspectionLoader)
            selected = []
            _selected_defaults(document, selected)
            report["selected_defaults"] = selected
            report["config_parsed"] = True
        else:
            report["config_parsed"] = False
    except Exception as error:
        report["error_type"] = type(error).__name__
    if args.inspect_credential_presence:
        report["credential_presence"] = inspect_credential_presence(Path.home() / ".dsh")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()

