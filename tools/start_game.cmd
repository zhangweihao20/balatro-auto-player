@echo off
REM 启动 Balatro + BalatroBot。存在这个 .cmd 的原因：
REM 系统默认的 PowerShell 执行策略是 Restricted，直接运行 .ps1 会被拒绝。
REM 双击本文件即可，等同于以 Bypass 策略运行同名 ps1。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_game.ps1" %*
if errorlevel 1 pause
