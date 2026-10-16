@echo off
rem ============================================================================
rem  Install the in-game decision journal panel into the balatrobot mod.
rem
rem  This is the python-free install path: some machines have security software
rem  that blocks python.exe from writing outside the workspace, while built-in
rem  commands (mkdir / copy) go through fine. Messages are English-only so the
rem  console code page cannot mangle them.
rem
rem  Usage:  double-click, or run  mod_additions\install.cmd
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

set "SRC=%~dp0"
set "DST=%APPDATA%\Balatro\Mods\balatrobot"

echo Source : %SRC%
echo Target : %DST%
echo.

if not exist "%DST%\" (
  echo [FAIL] mod directory not found. Is BalatroBot installed?
  exit /b 1
)

if not exist "%SRC%balatrobot.lua" (
  echo [FAIL] %SRC%balatrobot.lua missing.
  echo        Run this first:  python mod_additions\install.py --emit-entry
  exit /b 1
)

rem ---- 1. create the new directories -------------------------------------
if not exist "%DST%\src\lua\ui\" (
  mkdir "%DST%\src\lua\ui"
  if errorlevel 1 (
    echo [FAIL] cannot create %DST%\src\lua\ui
    echo        "Access is denied" here means the block is NOT python-specific:
    echo        something is denying writes to %%APPDATA%%\Balatro for this account.
    echo        Check your security software / controlled-folder access.
    exit /b 2
  )
  echo [ok] created src\lua\ui
) else (
  echo [ok] src\lua\ui already exists
)

rem ---- 2. back up the entry file (once) ----------------------------------
if not exist "%DST%\balatrobot.lua.bak-journal" (
  copy /y "%DST%\balatrobot.lua" "%DST%\balatrobot.lua.bak-journal" >nul
  echo [ok] backed up balatrobot.lua
) else (
  echo [ok] backup already present
)

rem ---- 3. copy the two new files ----------------------------------------
copy /y "%SRC%src\lua\ui\journal.lua" "%DST%\src\lua\ui\journal.lua" >nul
if errorlevel 1 ( echo [FAIL] copy ui\journal.lua & exit /b 3 )
echo [ok] src\lua\ui\journal.lua

copy /y "%SRC%src\lua\endpoints\journal.lua" "%DST%\src\lua\endpoints\journal.lua" >nul
if errorlevel 1 ( echo [FAIL] copy endpoints\journal.lua & exit /b 3 )
echo [ok] src\lua\endpoints\journal.lua

rem ---- 4. install the pre-patched entry file ----------------------------
copy /y "%SRC%balatrobot.lua" "%DST%\balatrobot.lua" >nul
if errorlevel 1 ( echo [FAIL] copy balatrobot.lua & exit /b 3 )
echo [ok] balatrobot.lua (patched)

echo.
echo Done. Restart Balatro to load the panel.
echo The panel shows on the right side of the game window; press F8 to hide it.
exit /b 0
