@echo off
REM 运行机器人。用法见 README，例如：
REM     tools\run_bot.cmd status
REM     tools\run_bot.cmd play --games 1
REM     tools\run_bot.cmd eval --games 20
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_bot.ps1" %*
