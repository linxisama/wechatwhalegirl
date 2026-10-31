@echo off
setlocal
set "EXE=%~dp0dist53\WhaleGirlWeChat\WhaleGirlWeChat.exe"
if not exist "%EXE%" ( echo [ERROR] exe not found: %EXE% & pause & exit /b 1 )
echo Stopping old console ...
taskkill /F /IM WhaleGirlWeChat.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo Requesting administrator rights (click Yes on the UAC dialog) ...
powershell -NoProfile -Command "Start-Process -FilePath '%EXE%' -Verb RunAs"
if errorlevel 1 ( echo [ERROR] elevation failed & pause )
exit /b 0
