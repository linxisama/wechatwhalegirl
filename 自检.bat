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
echo ============================================================
echo   离线自检：不连微信、不连模型接口
echo ============================================================
echo.
echo [1/2] 后端链路自检（消息 - 插件 - 回复 - 发送）...
".venv-wechat\Scripts\python.exe" tools\selfcheck_backend.py
if errorlevel 1 (
    echo [失败] 后端自检未通过，请把上面的报错发给开发者。
    pause
    exit /b 1
)
echo.
echo [2/2] 控制台渲染自检（会在 out\ 目录留下截图）...
".venv-wechat\Scripts\python.exe" wechat_gui.py --self-test out\smoke.json
if errorlevel 1 (
    echo [失败] 控制台自检未通过，详细原因见 out\smoke.error.txt
    pause
    exit /b 1
)
echo.
echo 自检全部通过。截图与摘要：out\ 目录。
pause
