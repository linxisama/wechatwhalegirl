@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

rem 为什么要管理员：微信 4.x 的界面自动化发送在普通权限下会被拒绝，
rem 典型症状是"能收到消息但发不出去"。这个启动器自己申请提权后再启动程序。
net session >nul 2>&1
if not errorlevel 1 (
    start "" "%~dp0WhaleGirlWeChat.exe"
    exit /b 0
)

echo 正在申请管理员权限（微信 4.x 的发送需要）...
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0WhaleGirlWeChat.exe' -Verb RunAs"
if errorlevel 1 (
    echo [错误] 提权失败。请右键 WhaleGirlWeChat.exe，选择「以管理员身份运行」。
    pause
)
exit /b 0
