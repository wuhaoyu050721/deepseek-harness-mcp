"""生成独立人工示例仓库和两套本机配置；不改个人客户端配置，不调用模型。"""

import json
from pathlib import Path
import subprocess
import sys


def write_new(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        return
    with path.open("x",encoding="utf-8",newline="\n") as stream:
        stream.write(content)


def main() -> None:
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    sample = root / "data" / "示例 project"
    if not sample.exists():
        sample.mkdir(parents=True)
        (sample / "calculator.py").write_text('''"""人工验收样例，包含供审查定位的边界问题。"""

def average(values):
    return sum(values) / len(values)


def withdraw(balance, amount):
    return balance - amount
''',encoding="utf-8")
        (sample / "README.md").write_text("# 人工示例仓库\n\n只读审查 calculator.py，关注空列表和负数金额边界。不要运行命令或修改源文件。\n",encoding="utf-8")
        subprocess.run(["git","init","-q",str(sample)],check=True)
        subprocess.run(["git","-C",str(sample),"add","calculator.py","README.md"],check=True)
        subprocess.run(["git","-c","user.name=MCP Sample","-c","user.email=sample@localhost","-c","core.hooksPath=/dev/null","-C",str(sample),"commit","-q","-m","Artificial read-only acceptance fixture"],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    configs = {}
    mcp_scopes = ["src", "tests", "scripts", "docs", "README.md", "pyproject.toml", "worker-requirements.lock"]
    # 源码 ZIP 没有根 Git 元数据时只授权人工样例，不扩大为整个解压目录。
    # 检查根自己的 .git，避免把解压目录的父仓库误当成本项目仓库。
    has_root_git = (root / ".git").exists()
    mcp_extras = []
    if has_root_git:
        # 未跟踪文件按当前 Git 清单逐个授权；目录本身不能扩展未跟踪文件权限。
        candidates = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z", "--", *mcp_scopes], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        mcp_extras = sorted(path.decode("utf-8") for path in candidates.split(b"\x00") if path)
    for client in ("codex","claude"):
        config = root / "config" / f"{client}.local.toml"
        content = f'''# 本机可信配置。凭证只引用个人 Harness API key；不复制 key 值。
data_dir = {json.dumps((root / "data" / client).as_posix(),ensure_ascii=False)}

[model]
provider = "deepseek-official"
model = "deepseek-flash"
base_url = "https://api.deepseek.com/anthropic"
credential_source = "desktop_store"
credential_env = "DEEPSEEK_API_KEY"

[executor]
backend = "desktop"
desktop_root = ""
image = "deepseek-harness-worker:0.1.0"
cpus = 1.0
memory_mb = 1024
pids_limit = 128
lease_seconds = 15
heartbeat_seconds = 2
termination_grace_seconds = 3

[limits]
max_queue = 5
max_parallel_jobs = 3
default_timeout_seconds = 600
max_timeout_seconds = 1800
max_file_bytes = 2097152
max_snapshot_bytes = 104857600
max_output_bytes = 1048576
max_prompt_chars = 32000

[projects.sample]
path = {json.dumps(sample.as_posix(),ensure_ascii=False)}
allowed_paths = ["."]
exclude_patterns = []
allow_untracked = []
'''
        if has_root_git:
            content += f'''
[projects.mcp]
path = {json.dumps(root.as_posix(),ensure_ascii=False)}
allowed_paths = ["src", "tests", "scripts", "docs", "README.md", "pyproject.toml", "worker-requirements.lock"]
exclude_patterns = []
allow_untracked = {json.dumps(mcp_extras,ensure_ascii=False)}
'''
        write_new(config,content)
        configs[client] = config
    python = root / ".venv" / ("Scripts/python.exe" if sys.platform=="win32" else "bin/python")
    command = json.dumps(str(python),ensure_ascii=False)
    codex_args = json.dumps(["-m","harness_mcp.server","--shared","--config",str(configs["codex"])],ensure_ascii=False)
    cwd = json.dumps(str(root),ensure_ascii=False)
    write_new(root / "examples/codex.local.toml",f"[mcp_servers.deepseek_harness]\ncommand = {command}\nargs = {codex_args}\ncwd = {cwd}\nstartup_timeout_sec = 30\ntool_timeout_sec = 30\n")
    claude = {"mcpServers":{"deepseek_harness":{"type":"stdio","command":str(python),"args":["-m","harness_mcp.server","--shared","--config",str(configs["claude"])]}}}
    write_new(root / "examples/claude.local.json",json.dumps(claude,ensure_ascii=False,indent=2)+"\n")
    print("已准备人工示例仓库、独立 Codex/Claude 数据目录配置和客户端配置片段；没有调用模型。")
    if not has_root_git:
        print("源码解压目录没有 .git，本次只授权人工示例 sample；请按连接教程在 config/*.local.toml 中配置自己的 Git 项目。")


if __name__ == "__main__":
    main()
