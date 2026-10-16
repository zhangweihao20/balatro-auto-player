# 运行机器人。游戏要先用 tools\start_game.ps1 启动好。
#
# 用法：
#     .\tools\run_bot.ps1 status                 检查连接
#     .\tools\run_bot.ps1 play --games 1         打一局
#     .\tools\run_bot.ps1 eval --games 20        批量评测胜率
#     .\tools\run_bot.ps1 solve --top 10         看当前局面怎么打（不下指令）

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# 优先用项目内的虚拟环境（装了 pytest），没有就用系统 Python（机器人本身零依赖）
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

Push-Location $root
try {
    & $python -m balatro_bot @Args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
