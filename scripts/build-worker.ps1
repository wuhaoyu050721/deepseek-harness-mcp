$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskDocker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $taskDocker) { throw '未安装 Docker；请先安装并启动 Linux 容器执行环境。' }
Push-Location $taskRoot
try {
    & $taskDocker.Source build --tag deepseek-harness-worker:0.1.0 --file Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw 'Worker 镜像构建失败。' }
} finally { Pop-Location }
