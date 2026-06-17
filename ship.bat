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
    echo.
    echo   ✗ UNSUCCESSFUL
    echo   Reason: git add failed
    echo   ──────────────────────────────────────────────────
    echo.
    exit /b 1
)

git commit -m "%~1"
if errorlevel 1 (
    echo.
    echo   ✗ UNSUCCESSFUL
    echo   Reason: nothing to commit, or commit failed
    echo   ──────────────────────────────────────────────────
    echo.
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo.
    echo   ✗ UNSUCCESSFUL
    echo   Reason: push failed — check remote and auth
    echo   ──────────────────────────────────────────────────
    echo.
    exit /b 1
)

echo.
echo   ✓ SUCCESSFUL
echo   Committed: "%~1"
echo   ──────────────────────────────────────────────────
echo.
