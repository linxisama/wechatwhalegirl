# 安装包冒烟验证：静默安装 -> 跑装好的程序自检 -> 卸载
param([string]$PreviousInstaller = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
# 版本号从源码读，避免脚本里写死版本号导致"打包了 1.0.1 却在验证 1.0.0"
$identity = Get-Content (Join-Path $root 'src\runtime_identity.py') -Raw -Encoding UTF8
if ($identity -notmatch "APP_VERSION\s*=\s*'([^']+)'") { throw '读不到 APP_VERSION' }
$version = $Matches[1]
$setup = Join-Path $root "installer\WhaleGirlWeChat-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "找不到安装包：$setup（先运行 build_installer.ps1）" }
Write-Host "待验证的安装包：$setup"
$target = Join-Path $root '.tmp-install-test\WhaleGirlWeChat'
$smoke = Join-Path $root '.tmp-install-test\installed-self-test.json'

$verificationParent = [IO.Path]::GetFullPath((Split-Path $target))
$workspacePrefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
if (-not $verificationParent.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "验证目录超出工作区：$verificationParent"
}
if (Test-Path (Split-Path $target)) { Remove-Item (Split-Path $target) -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null

$python = Join-Path $root '.venv-wechat\Scripts\python.exe'
$isolatedProfile = Join-Path $verificationParent 'UserProfile'
$isolatedData = Join-Path $isolatedProfile 'WhaleGirlWeChat'
$upgradeResult = Join-Path $verificationParent 'upgrade-verification.json'

function Check-UserData {
    & $python (Join-Path $root 'tools\upgrade_fixture.py') verify $isolatedData
    if ($LASTEXITCODE -ne 0) { throw '配置、对话记录或记忆未完整保留' }
}

function Start-IsolatedProgram([string]$Executable, [string[]]$Arguments) {
    # 验证程序的日志与启动资源也隔离，不能碰正式用户数据。
    $previousLocalData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = $isolatedProfile
        $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WindowStyle Hidden -Wait -PassThru
    } finally {
        $env:LOCALAPPDATA = $previousLocalData
    }
    return $process
}

if (-not $PreviousInstaller) {
    $candidates = Get-ChildItem -LiteralPath (Join-Path $root 'installer') -Filter 'WhaleGirlWeChat-Setup-*.exe' |
        ForEach-Object {
            if ($_.BaseName -match 'Setup-(\d+\.\d+\.\d+)$' -and [version]$Matches[1] -lt [version]$version) {
                [pscustomobject]@{ Path = $_.FullName; Version = [version]$Matches[1] }
            }
        } | Sort-Object Version -Descending
    if ($candidates) { $PreviousInstaller = @($candidates)[0].Path }
}
$initialSetup = if ($PreviousInstaller) { $PreviousInstaller } else { $setup }
if (-not (Test-Path -LiteralPath $initialSetup)) { throw "找不到升级起点安装包：$initialSetup" }

# 模拟旧版缺失图标的桌面入口，不勾选创建桌面选项，验证升级也能修复。
$iconTestDesktop = Join-Path $target '.verification-shortcuts\Desktop'
New-Item -ItemType Directory -Force -Path $iconTestDesktop | Out-Null
$iconTestLink = Join-Path $iconTestDesktop '鲸鱼娘 微信控制台.lnk'
$shortcutShell = New-Object -ComObject WScript.Shell
$legacyLink = $shortcutShell.CreateShortcut($iconTestLink)
$legacyLink.TargetPath = Join-Path $target '启动（管理员）.cmd'
$legacyLink.IconLocation = ',0'
$legacyLink.Save()
$realDesktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) '鲸鱼娘 微信控制台.lnk'
$realDesktopHash = if (Test-Path -LiteralPath $realDesktopLink) {
    (Get-FileHash -LiteralPath $realDesktopLink -Algorithm SHA256).Hash
} else { '' }

Write-Host "== 安装升级起点：$initialSetup"
$install = Start-Process -FilePath $initialSetup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/VerifyInstall=1', '/TASKS=', "/DIR=`"$target`""
) -WindowStyle Hidden -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "安装失败，退出码 $($install.ExitCode)" }

Write-Host '== 写入隔离用户配置、人设、插件、密码摘要、记忆及 20025 条旧对话'
& $python (Join-Path $root 'tools\upgrade_fixture.py') seed $isolatedData
if ($LASTEXITCODE -ne 0) { throw '升级测试数据创建失败' }
# 早期手动运行时留在安装目录的数据也不得在覆盖安装时被抹掉。
$legacyConfig = Join-Path $target 'config.wechat.yaml'
$legacyHistory = Join-Path $target 'data\legacy-user-history.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $legacyHistory) | Out-Null
[IO.File]::WriteAllText($legacyConfig, 'legacy_user_config: keep')
[IO.File]::WriteAllText($legacyHistory, 'legacy conversation: keep')
$legacyConfigHash = (Get-FileHash -LiteralPath $legacyConfig -Algorithm SHA256).Hash
$legacyHistoryHash = (Get-FileHash -LiteralPath $legacyHistory -Algorithm SHA256).Hash

Write-Host '== 不卸载旧版、不指定目录，直接覆盖安装新版（检查自动沿用旧路径）'
$install = Start-Process -FilePath $setup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/VerifyInstall=1', '/TASKS='
) -WindowStyle Hidden -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "覆盖安装失败，退出码 $($install.ExitCode)" }
if ((Get-Item -LiteralPath (Join-Path $target 'WhaleGirlWeChat.exe')).VersionInfo.FileVersion -ne $version) {
    throw '覆盖安装没有沿用旧安装路径或程序版本不正确'
}
if ((Get-FileHash -LiteralPath $legacyConfig -Algorithm SHA256).Hash -ne $legacyConfigHash -or
    (Get-FileHash -LiteralPath $legacyHistory -Algorithm SHA256).Hash -ne $legacyHistoryHash) {
    throw '覆盖安装改写了安装目录里的旧用户数据'
}
Check-UserData

$exe = Join-Path $target 'WhaleGirlWeChat.exe'
$cmd = Join-Path $target '启动（管理员）.cmd'
$unins = Join-Path $target 'unins000.exe'
foreach ($path in @($exe, $cmd, $unins, (Join-Path $target '微信版使用说明.md'), (Join-Path $target '封号风险与功能取舍.md'))) {
    if (-not (Test-Path $path)) { throw "安装目录缺少文件：$path" }
    Write-Host "  存在：$(Split-Path $path -Leaf)"
}
Write-Host '== 校验桌面升级修复与开始菜单快捷方式图标'
$iconTestMenu = Join-Path $target '.verification-shortcuts\StartMenu'
$shortcutTargets = @(
    @{ Path = $iconTestLink; Target = $cmd },
    @{ Path = (Join-Path $iconTestMenu '鲸鱼娘 微信控制台（管理员启动）.lnk'); Target = $cmd },
    @{ Path = (Join-Path $iconTestMenu '鲸鱼娘 微信控制台.lnk'); Target = $exe }
)
foreach ($item in $shortcutTargets) {
    if (-not (Test-Path -LiteralPath $item.Path)) { throw "缺少快捷方式：$($item.Path)" }
    $shortcut = $shortcutShell.CreateShortcut($item.Path)
    if ($shortcut.TargetPath -ne $item.Target) { throw "快捷方式启动目标不正确：$($item.Path)" }
    if ($shortcut.IconLocation.Replace('"', '') -ne "$exe,0") {
        throw "快捷方式没有使用程序图标：$($shortcut.IconLocation)"
    }
    Write-Host "  图标与启动目标正确：$(Split-Path $item.Path -Leaf)"
}
$sizeMb = [math]::Round((Get-ChildItem $target -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "  安装后占用：$sizeMb MB"

Write-Host '== 运行装好的程序自检（不连微信、不连模型）'
$proc = Start-IsolatedProgram $exe @('--self-test', "`"$smoke`"")
if ($proc.ExitCode -ne 0) {
    $err = [System.IO.Path]::ChangeExtension($smoke, '.error.txt')
    if (Test-Path $err) { Get-Content $err -Tail 30 }
    throw "安装后的程序自检失败，退出码 $($proc.ExitCode)"
}
$data = Get-Content $smoke -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $data.acceptance.ok) { throw '装好的程序未通过真实 HTTP 界面贯通验收' }
Write-Host ("  自检通过：frozen={0}，插件 {1} 个，端到端发送 {2} 条，回复「{3}」" -f `
    $data.frozen, $data.backend.plugins, $data.backend.sent, $data.backend.reply)
Write-Host "  截图：$([System.IO.Path]::ChangeExtension($smoke, '.png'))"

Write-Host '== 验证实际桌面启动、配置保存和后台异常重连'
$desktopResult = Join-Path $verificationParent 'installed-desktop-check.json'
$proc = Start-IsolatedProgram $exe @('--desktop-check', "`"$desktopResult`"")
if ($proc.ExitCode -ne 0) { throw '安装后的桌面启动验收失败' }
$desktop = Get-Content -LiteralPath $desktopResult -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $desktop.ok -or -not $desktop.backend_crash_survived -or -not $desktop.reconnect_preserved_settings) { throw '安装后桌面验收结果不完整' }

Write-Host '== 新版冻结程序重载原用户数据，核对配置与全部历史'
$proc = Start-IsolatedProgram $exe @('--verify-upgrade-data', "`"$upgradeResult`"")
if ($proc.ExitCode -ne 0) {
    $err = [IO.Path]::ChangeExtension($upgradeResult, '.error.txt')
    if (Test-Path $err) { Get-Content $err -Tail 30 }
    throw '覆盖升级后的数据重载验证失败'
}
$upgrade = Get-Content -LiteralPath $upgradeResult -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $upgrade.ok -or -not $upgrade.runtime_reloaded -or $upgrade.history_count -ne 20025) {
    throw '覆盖升级数据验证结果不完整'
}
Write-Host "  配置与文件保留：$($upgrade.files_preserved) 个；对话保留：$($upgrade.history_count) 条"
Check-UserData

Write-Host '== 静默卸载'
$uninstall = Start-Process -FilePath $unins -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw "卸载失败，退出码 $($uninstall.ExitCode)" }
Start-Sleep -Seconds 2
if (Test-Path $exe) { throw '卸载后 exe 仍然存在' }
Write-Host '  卸载完成，程序文件已移除'

Write-Host '== 卸载后再次核对隔离用户数据（逐文件哈希与对话记录）'
Check-UserData
if (-not (Test-Path -LiteralPath $legacyConfig) -or -not (Test-Path -LiteralPath $legacyHistory)) {
    throw '卸载删除了旧版遗留的用户数据'
}

Write-Host '== 用户数据是否保留（设计上必须保留）'
$userData = Join-Path $env:LOCALAPPDATA 'WhaleGirlWeChat'
if (Test-Path $userData) { Write-Host "  保留：$userData" } else { Write-Host '  （本次测试未产生用户数据，符合预期）' }

Write-Host ''
if ($realDesktopHash) {
    if (-not (Test-Path -LiteralPath $realDesktopLink) -or
        (Get-FileHash -LiteralPath $realDesktopLink -Algorithm SHA256).Hash -ne $realDesktopHash) {
        throw '隔离验证意外修改了真实桌面快捷方式'
    }
    Write-Host '  真实桌面快捷方式未被验证安装/卸载修改'
}
Write-Host '安装包冒烟验证全部通过' -ForegroundColor Green
