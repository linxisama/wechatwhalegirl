<#
  一键打包：PyInstaller 出 onedir 程序目录 → Inno Setup 出安装包。
  用法（在项目根目录）：
      powershell -ExecutionPolicy Bypass -File build_installer.ps1
  或直接双击「打包.bat」。

  版本号取自 src/runtime_identity.py 的 APP_VERSION（单一来源，避免两处版本不一致）。
  注意：本文件必须以 **UTF-8 带 BOM** 保存 —— Windows PowerShell 5.1 会按 ANSI
  读取无 BOM 的 .ps1，中文会变成乱码并直接语法报错。

  产物：
      dist\WhaleGirlWeChat\                        程序目录（exe + _internal）
      installer\WhaleGirlWeChat-Setup-<版本>.exe   安装包
      .build-tools\packaged-self-test.json         打包后自检结果（截图在同目录）

  打包后会**自动跑一遍离线自检**：直接把冻结出来的 exe 用 --self-test 跑一遍，
  验证打包没有漏模块 —— 这是"改了代码忘记更新包"最容易出问题的地方。
#>
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Fail($message) { Write-Host "[失败] $message" -ForegroundColor Red; exit 1 }
function Step($message) { Write-Host "== $message" -ForegroundColor Cyan }

$python = Join-Path $PSScriptRoot '.venv-wechat\Scripts\python.exe'
if (-not (Test-Path $python)) { Fail '找不到 .venv-wechat，请先运行「一键安装依赖.bat」' }

$iscc = Join-Path $PSScriptRoot '.build-tools\InnoSetup\ISCC.exe'
if (-not (Test-Path $iscc)) { Fail "找不到 Inno Setup 编译器：$iscc" }

Step '读取版本号'
$identity = Get-Content (Join-Path $PSScriptRoot 'src\runtime_identity.py') -Raw -Encoding UTF8
if ($identity -notmatch "APP_VERSION\s*=\s*'([^']+)'") { Fail '在 src/runtime_identity.py 里读不到 APP_VERSION' }
$version = $Matches[1]
Write-Host "版本：$version"

Step '检查 PyInstaller'
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '未安装 PyInstaller，正在安装…'
    & $python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
    if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 安装失败' }
}

Step '清理旧的构建产物'
foreach ($path in @('build', 'dist\WhaleGirlWeChat')) {
    $absoluteTarget = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $path))
    $workspacePrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (-not $absoluteTarget.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "构建清理目标超出工作区：$absoluteTarget"
    }
    if (Test-Path -LiteralPath $absoluteTarget) { Remove-Item -LiteralPath $absoluteTarget -Recurse -Force }
}

Step 'PyInstaller 打包（onedir）'
$env:WECHAT_APP_VERSION = $version
& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build wechat_bot.spec
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller 打包失败' }

$exe = Join-Path $PSScriptRoot 'dist\WhaleGirlWeChat\WhaleGirlWeChat.exe'
if (-not (Test-Path $exe)) { Fail "打包后找不到 $exe" }
$sizeMb = [math]::Round((Get-ChildItem 'dist\WhaleGirlWeChat' -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "程序目录大小：$sizeMb MB"

Step '最小系统 PATH 原生依赖检查（不连微信）'
$nativeJson = Join-Path $PSScriptRoot '.build-tools\packaged-native-check.json'
Remove-Item -LiteralPath $nativeJson -Force -ErrorAction SilentlyContinue
$previousTaskPath = $env:PATH
$previousTaskLocalData = $env:LOCALAPPDATA
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:LOCALAPPDATA = Join-Path $PSScriptRoot '.build-tools\native-probe-profile'
    $nativeProcess = Start-Process -FilePath $exe -ArgumentList '--native-check', $nativeJson -WindowStyle Hidden -Wait -PassThru
} finally {
    $env:PATH = $previousTaskPath
    $env:LOCALAPPDATA = $previousTaskLocalData
}
if ($nativeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $nativeJson)) {
    Fail '冻结程序无法加载原生依赖，不能发布安装包'
}
$native = Get-Content -LiteralPath $nativeJson -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $native.ok -or $native.app_local_mfc -ne 'mfc140u.dll') { Fail '原生依赖检查未确认应用本地 MFC' }

Step '打包后离线自检（不连微信、不连模型）'
$smokeJson = Join-Path $PSScriptRoot '.build-tools\packaged-self-test.json'
$smokeError = [System.IO.Path]::ChangeExtension($smokeJson, '.error.txt')
Remove-Item $smokeJson, $smokeError -Force -ErrorAction SilentlyContinue
# 必须用 Start-Process -Wait：exe 是 GUI 子系统程序（console=False），
# 直接 `& $exe ...` 时 PowerShell 不会等它结束，会立刻去读还没生成的 JSON。
$proc = Start-Process -FilePath $exe -ArgumentList '--self-test', $smokeJson -WindowStyle Hidden -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    if (Test-Path $smokeError) { Get-Content $smokeError -Tail 40 }
    Fail '打包后的程序没通过自检：多半是漏了某个模块或数据文件，见上面的报错'
}
if (-not (Test-Path $smokeJson)) { Fail "自检没有产出结果文件：$smokeJson" }
$smoke = Get-Content $smokeJson -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $smoke.ok -or -not $smoke.acceptance.ok -or -not $smoke.native_dependencies.ok) { Fail '冻结程序、原生依赖或真实 HTTP 界面贯通验收未通过' }
Write-Host ("自检通过：插件 {0} 个，端到端发送 {1} 条，回复「{2}」" -f `
    $smoke.backend.plugins, $smoke.backend.sent, $smoke.backend.reply)

Step '实际桌面启动与独立后台崩溃恢复验收'
$desktopJson = Join-Path $PSScriptRoot '.build-tools\packaged-desktop-check.json'
Remove-Item $desktopJson -Force -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $exe -ArgumentList '--desktop-check', $desktopJson -WindowStyle Hidden -Wait -PassThru
if ($proc.ExitCode -ne 0 -or -not (Test-Path $desktopJson)) {
    $desktopError = [IO.Path]::ChangeExtension($desktopJson, '.error.txt')
    if (Test-Path $desktopError) { Get-Content $desktopError -Encoding UTF8 -Tail 30 }
    Fail '桌面启动、保存或后台异常恢复验收失败'
}
$desktop = Get-Content $desktopJson -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $desktop.ok -or -not $desktop.backend_crash_survived -or -not $desktop.reconnect_preserved_settings) { Fail '桌面启动验收结果不完整' }

Step 'Inno Setup 生成安装包'
& $iscc "/DAppVersion=$version" installer.iss
if ($LASTEXITCODE -ne 0) { Fail 'Inno Setup 编译失败' }

$setup = Join-Path $PSScriptRoot "installer\WhaleGirlWeChat-Setup-$version.exe"
if (-not (Test-Path $setup)) { Fail "找不到安装包：$setup" }
$setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)

Write-Host ''
Write-Host '========== 打包完成 ==========' -ForegroundColor Green
Write-Host "程序目录：dist\WhaleGirlWeChat\（$sizeMb MB）"
Write-Host "安装包  ：$setup（$setupMb MB）"
Write-Host "自检结果：.build-tools\packaged-self-test.json（含界面截图）"
