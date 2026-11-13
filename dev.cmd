@echo off
setlocal
where node.exe >nul 2>&1
if errorlevel 1 (
  echo Node.js is not available on PATH.
  exit /b 1
)
where npm.cmd >nul 2>&1
if errorlevel 1 (
  echo npm.cmd is not available on PATH.
  exit /b 1
)
call npm.cmd --prefix "%~dp0frontend" run dev:all
exit /b %errorlevel%
