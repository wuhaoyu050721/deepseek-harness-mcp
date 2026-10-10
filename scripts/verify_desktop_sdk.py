"""生产桌面入口的无模型 SDK 握手：专用 home、固定隐私补丁、整树停止。

仅 initialize/shutdown，从不提交 prompt，不读个人凭证或会话。可选 wheel
须与官方 PyPI SHA256 一致；默认使用正式安装并锁定的宿主 SDK。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


SDK_WHEEL_SHA256 = "8d9897e97e8a95fd738cb2c3e2c563951d824e43847ec6f3b0fb172c0729b4d8"


def _worker(args: argparse.Namespace) -> dict:
    if args.sdk_wheel:
        sys.path.insert(0, str(args.sdk_wheel))
    from deepseek_harness import HarnessClient, HarnessConfig
    from harness_mcp.desktop_launcher import create_desktop_client, prepare_desktop_launch
    from harness_mcp.harness_adapter import write_privacy_patch
    import yaml

    class InspectionLoader(yaml.SafeLoader):
        pass

    InspectionLoader.add_constructor("tag:yaml.org,2002:js", lambda loader, node: loader.construct_scalar(node))
    home = args.probe_dir / "home"
    workspace = args.probe_dir / "workspace"
    workspace.mkdir()
    patch = write_privacy_patch(home)
    config = HarnessConfig(
        dsh_home=str(home), cwd=str(workspace), profile="sdk-minimal", patches=(str(patch),),
        env={"DEEPSEEK_API_KEY": "probe-not-a-real-api-key", "DEEPSEEK_BASE_URL": "http://127.0.0.1:9/anthropic"},
        initialize_timeout_seconds=20, request_timeout_seconds=5, shutdown_timeout_seconds=1,
    )
    prepared, direct_argv = prepare_desktop_launch(config, str(args.install_root))
    mode = "public_dsh_bin" if direct_argv is None else "fixed_argv_compatibility"
    launch = direct_argv or (prepared.dsh_bin, "--profile", prepared.profile,
                            *(argument for value in prepared.patches for argument in ("--patch", value)))
    report = {"sdk_handshake_verified": False, "privacy_patch_composed": False, "runtime_launch_api": mode}
    # 原始组合配置仅在内存解析，不保存/打印；此执行环境只有假凭证。
    dumped = subprocess.run((*launch, "--dump-config"), cwd=workspace,
                            env={**os.environ, **prepared.env}, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=15)
    if dumped.returncode:
        return {**report, "error_type": "ConfigDumpFailed"}
    document = yaml.load(dumped.stdout, Loader=InspectionLoader)
    rows = {}

    def collect(node):
        if isinstance(node, list):
            for child in node:
                collect(child)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str):
                rows[node["id"]] = node
            for child in node.values():
                if isinstance(child, (dict, list)):
                    collect(child)

    collect(document)
    wanted = ("session-log-deepseek", "plugin-package-inventory-deepseek", "session-title")
    report["privacy_patch_composed"] = all(rows.get(name, {}).get("disabled") is True for name in wanted)
    report["extra_uploads_disabled"] = all(
        rows.get(name, {}).get("config", {}).get("enabled") is False for name in wanted[:2])
    if not (report["privacy_patch_composed"] and report["extra_uploads_disabled"]):
        return {**report, "error_type": "PrivacyPatchNotComposed"}
    client = create_desktop_client(config, str(args.install_root), HarnessClient)
    try:
        client.start()
        client.initialize(cwd=str(workspace), provider="deepseek-official", model="deepseek-flash")
        report["sdk_handshake_verified"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        client.close()
    return report


async def _supervise(args: argparse.Namespace, report: dict) -> None:
    from harness_mcp.process_control import spawn_owned, stop_process_tree

    argv = [sys.executable, str(Path(__file__).resolve()), "--worker",
            "--install-root", str(args.install_root.resolve()), "--probe-dir", str(args.probe_dir.resolve())]
    if args.sdk_wheel:
        argv += ["--sdk-wheel", str(args.sdk_wheel.resolve())]
    environment = {name: os.environ[name] for name in (
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ) if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    process = spawn_owned(argv, environment, max_output=1024 * 1024)
    report["process_tree_owned_before_execution"] = True
    communication = asyncio.create_task(asyncio.to_thread(process.communicate))
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=40)
        report["probe_exit_code"] = process.returncode
        if process.returncode == 0:
            report.update(json.loads(stdout))
        else:
            report["error_type"] = "ProbeWorkerFailed"
    except asyncio.TimeoutError:
        report["error_type"] = "ProbeOverallTimeout"
    finally:
        report["process_tree_stopped"] = await stop_process_tree(process)
        if report["process_tree_stopped"]:
            await asyncio.gather(communication, return_exceptions=True)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-wheel", type=Path)
    parser.add_argument("--install-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DeepSeek Harness")
    parser.add_argument("--probe-dir", type=Path)
    parser.add_argument("--output", type=Path, default=root / "artifacts/desktop-sdk-handshake.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(_worker(args), ensure_ascii=False))
        return
    report = {
        "scope": "production_desktop_sdk_initialize_only", "sdk_version": "0.1.5rc1", "desktop_version": "0.2.0-rc.2",
        "sdk_handshake_verified": False, "model_called": False, "runtime_prompt_submitted": False,
        "private_credentials_read": False, "private_home_writes_requested": False,
        "isolated_probe_home": True, "filesystem_sandboxed": False,
        "process_tree_owned_before_execution": False, "process_tree_stopped": False,
        "provider_request_fields_verified": False,
    }
    try:
        if os.name != "nt":
            raise RuntimeError("WindowsOnlyProbe")
        if args.sdk_wheel and hashlib.sha256(args.sdk_wheel.read_bytes()).hexdigest() != SDK_WHEEL_SHA256:
            raise RuntimeError("SdkWheelHashMismatch")
        args.probe_dir = args.probe_dir or root / "data/phase0" / ("desktop-sdk-" + uuid.uuid4().hex)
        args.probe_dir.mkdir(parents=True, exist_ok=False)
        asyncio.run(_supervise(args, report))
    except Exception as error:
        report["error_type"] = type(error).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
