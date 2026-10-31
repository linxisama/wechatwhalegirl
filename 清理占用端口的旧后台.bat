@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
echo ============================================================
echo   清理占用 8091 端口的旧后台
echo ============================================================
echo.
echo 适用情况：控制台提示"后台使用了其他目录的配置"、
echo           或"检测到旧版或其他后台占用 8091"。
echo 只会结束后台身份确认为本项目的进程，不会动其它程序。
echo.
where pwsh >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\free_port.ps1"
) else (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\free_port.ps1"
)
pause
