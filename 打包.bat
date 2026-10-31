@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
echo ============================================================
echo   鲸鱼娘 · 微信控制台   一键打包（程序 + 安装包）
echo ============================================================
echo.
echo 流程：PyInstaller 打包程序 - 打包后自检 - Inno Setup 生成安装包
echo 产物：dist\WhaleGirlWeChat\ 与 installer\WhaleGirlWeChat-Setup-*.exe
echo.

rem 优先用 PowerShell 7（对 UTF-8 无 BOM 的脚本也能正确解析）；
rem 没有 pwsh 时退回 Windows PowerShell 5.1 —— 那种情况下脚本必须带 UTF-8 BOM，
rem build_installer.ps1 已按带 BOM 保存，请不要用会去掉 BOM 的编辑器保存它。
where pwsh >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1"
) else (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_installer.ps1"
)

set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
    echo [失败] 打包未完成，请把上面的报错发给开发者。
) else (
    echo [完成] 安装包在 installer 目录里，双击即可安装。
)
pause
exit /b %code%
