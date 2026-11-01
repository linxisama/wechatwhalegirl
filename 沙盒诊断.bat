@echo off
chcp 65001 >nul
setlocal
set "EXE=%~dp0WhaleGirlWeChat.exe"
if not exist "%EXE%" set "EXE=%LOCALAPPDATA%\Programs\WhaleGirlWeChat\WhaleGirlWeChat.exe"
if not exist "%EXE%" set "EXE=%ProgramFiles%\WhaleGirlWeChat\WhaleGirlWeChat.exe"
if not exist "%EXE%" (
  echo 没找到 WhaleGirlWeChat.exe，请把本文件放到安装目录后重试。
  pause
  exit /b 1
)
"%EXE%" --sandbox-doctor --probe-launch
exit /b 0
