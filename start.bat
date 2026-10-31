@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
if not exist ".venv-wechat\Scripts\python.exe" (
    echo [错误] 还没有安装依赖，请先双击「一键安装依赖.bat」。
    pause
    exit /b 1
)
echo 只启动后台（不开控制台窗口）。
echo 浏览器打开 http://127.0.0.1:8091 可以看状态与改配置。
echo 按 Ctrl+C 停止。
echo.
".venv-wechat\Scripts\python.exe" wechat_main.py
if errorlevel 1 pause
