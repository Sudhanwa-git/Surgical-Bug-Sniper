@echo off
setlocal

if "%~1"=="" (
    echo.
    echo   ERROR: No commit message provided.
    echo   Usage: ship "your commit message"
    echo.
    exit /b 1
)

echo.
echo   ── SHIP ──────────────────────────────────────────
echo.

git add -A
if errorlevel 1 (
    echo   FAILED: git add
    exit /b 1
)

git commit -m "%~1"
if errorlevel 1 (
    echo   FAILED: git commit (nothing to commit?)
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo   FAILED: git push — check remote and auth
    exit /b 1
)

echo.
echo   SHIPPED: "%~1"
echo   ──────────────────────────────────────────────────
echo.
