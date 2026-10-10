[CmdletBinding()]
param(
    [string]$Output = "",
    [string]$ConfigPath = "",
    [switch]$RequireExecution
)

$ErrorActionPreference = "Stop"

# 使用项目自己的解释器；盘点入口不安装依赖、不修改执行策略或系统配置。
$projectRoot = Split-Path -Parent $PSScriptRoot
$doctorEntry = Join-Path $projectRoot "src\harness_mcp\doctor.py"
$projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pythonExecutable = $null
$pythonPrefix = @()

if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
    $pythonExecutable = $projectPython
} else {
    $pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $pythonCommand) {
        $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($null -ne $pythonCommand) {
        $pythonExecutable = $pythonCommand.Source
    } else {
        $pythonLauncher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $pythonLauncher) {
            throw "未找到 Python。请先配置 Python 3.11+ 或项目 .venv。"
        }
        $pythonExecutable = $pythonLauncher.Source
        $pythonPrefix = @("-3")
    }
}

$doctorArguments = @()
if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
    $doctorArguments += @("--config", $ConfigPath)
}
if (-not [string]::IsNullOrWhiteSpace($Output)) {
    $doctorArguments += @("--output", $Output)
}
if ($RequireExecution) {
    $doctorArguments += "--require-execution"
}

# 参数数组保留中文与空格路径，调用后原样传播 Python 的验收退出码。
& $pythonExecutable @pythonPrefix $doctorEntry @doctorArguments
exit $LASTEXITCODE
