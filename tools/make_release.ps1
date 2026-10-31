# 一次发布出两份产物：安装包 + DIY 指令包（放进 发布/<版本>/）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File tools/make_release.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
& (Join-Path $root '.venv-wechat\Scripts\python.exe') (Join-Path $root 'tools\make_release.py') @args
exit $LASTEXITCODE
