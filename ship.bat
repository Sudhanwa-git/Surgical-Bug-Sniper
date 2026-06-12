@echo off
if "%~1"=="" (
    echo Error: Please provide a commit message.
    echo Usage: ship.bat "Your commit message"
    exit /b 1
)
git add .
git commit -m "%~1"
git push
