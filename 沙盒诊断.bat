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
echo Running sandbox diagnosis. It takes 30-90 seconds.
echo A report will open automatically when it finishes.
echo.
start /wait "" "%EXE%" --sandbox-doctor --probe-launch
echo.
echo Done. The report is sandbox-doctor.txt on your Desktop.
echo If no report appeared, send the screenshot of this window back.
timeout /t 8 >nul
exit /b 0
