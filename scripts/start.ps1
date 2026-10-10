param(
    [ValidateSet('codex','claude')][string]$Client = 'codex',
    [string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '先运行 uv sync --locked --no-install-package deepseek-harness-runtime-bin 安装桌面连接依赖。' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $taskRoot "config\$Client.local.toml" }
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw '先运行 scripts/setup_local.py 生成配置。' }
& $taskPython -m harness_mcp.server --config $ConfigPath
exit $LASTEXITCODE
