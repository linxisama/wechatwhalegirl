@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

rem 微信 4.x 的界面自动化发送需要更高权限：普通权限启动时常见症状是
rem "能收到消息但发不出去"。这里先看有没有管理员权限，没有就用 UAC 提权重开。
net session >nul 2>&1
if not errorlevel 1 (
    echo 已有管理员权限，直接启动控制台。
    call "%~dp0start_gui.bat"
    exit /b 0
)

echo 正在申请管理员权限（微信 4.x 的发送需要）...
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0start_gui.bat' -Verb RunAs"
if errorlevel 1 (
    echo [错误] 提权失败。请右键本文件，选择「以管理员身份运行」。
    pause
)
exit /b 0
