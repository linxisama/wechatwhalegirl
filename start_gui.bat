@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
if not exist ".venv-wechat\Scripts\python.exe" (
    echo [错误] 还没有安装依赖，请先双击「一键安装依赖.bat」。
    pause
    exit /b 1
)
if not exist ".tmp" mkdir .tmp
set "TEMP=%~dp0.tmp"
set "TMP=%~dp0.tmp"
echo 正在打开「鲸鱼娘 · 微信控制台」...
echo 后台地址：http://127.0.0.1:8091
".venv-wechat\Scripts\python.exe" wechat_gui.py
if errorlevel 1 (
    echo.
    echo 控制台异常退出。若看不到原因，请改用「以管理员身份启动控制台.bat」再试一次。
    pause
)
