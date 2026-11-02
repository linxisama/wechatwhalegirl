@echo off
chcp 65001 >nul
setlocal
set "EXE=%~dp0WhaleGirlWeChat.exe"
if not exist "%EXE%" set "EXE=%LOCALAPPDATA%\Programs\WhaleGirlWeChat\WhaleGirlWeChat.exe"
if not exist "%EXE%" set "EXE=%ProgramFiles%\WhaleGirlWeChat\WhaleGirlWeChat.exe"
if not exist "%EXE%" (
  echo [ERROR] WhaleGirlWeChat.exe not found next to this file.
  echo Put this .bat into the install folder and run it again.
  pause
  exit /b 1
)
set "OUT=%USERPROFILE%\Desktop\machine-check.txt"
echo Running machine check. It takes 10-30 seconds.
echo Report: %OUT%
echo.
"%EXE%" --machine-check "%OUT%"
echo.
if exist "%OUT%" (
  echo Done. Sending back machine-check.txt from your Desktop is enough.
  start "" notepad "%OUT%"
) else (
  echo No report was written. Send a screenshot of this window back.
)
timeout /t 8 >nul
exit /b 0
