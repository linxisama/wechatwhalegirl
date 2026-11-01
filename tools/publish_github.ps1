# 把本地仓库推到 GitHub 并给出发布步骤（只做本地准备，不自动联网推送，除非你显式加 -Push）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\publish_github.ps1                 # 只初始化/提交/打标签，打印后续命令
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\publish_github.ps1 -Push           # 真的推到 origin
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\publish_github.ps1 -Remote https://github.com/linxisama/wechatwhalegirl.git
param(
    [string]$Remote = 'https://github.com/linxisama/wechatwhalegirl.git',
    [switch]$Push,
    [switch]$SkipTag
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($message) { Write-Host "== $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "[失败] $message" -ForegroundColor Red; exit 1 }

$identity = Get-Content (Join-Path $root 'src\runtime_identity.py') -Raw -Encoding UTF8
if ($identity -notmatch "APP_VERSION\s*=\s*'([^']+)'") { Fail '在 src/runtime_identity.py 里读不到 APP_VERSION' }
$version = $Matches[1]
$tag = "v$version"

Step '检查 git'
git --version | Out-Host
if (-not (Test-Path (Join-Path $root '.git'))) {
    Step '初始化本地仓库'
    git init -b main | Out-Host
}

Step '检查有没有把不该提交的东西带进来'
$bad = @()
foreach ($pattern in @('data/', 'logs/', '.venv-wechat/', 'build/', 'dist/', 'installer/', 'release/')) {
    $tracked = git ls-files -- $pattern 2>$null
    if ($tracked) { $bad += $pattern }
}
if ($bad.Count -gt 0) { Fail ("这些目录里已有被跟踪的文件，请先 .gitignore 清理：" + ($bad -join ', ')) }

Step '暂存并提交'
git add -A | Out-Host
$pending = git status --porcelain
if ($pending) {
    git -c user.name=(git config user.name) -c user.email=(git config user.email) commit -m "release: $version" | Out-Host
} else {
    Write-Host '没有新改动需要提交'
}

if (-not $SkipTag) {
    Step "打标签 $tag"
    $existing = git tag --list $tag
    if ($existing) { Write-Host "标签 $tag 已存在，跳过" } else { git tag -a $tag -m "$version" | Out-Host }
}

Step '设置远端'
$current = git remote get-url origin 2>$null
if ($LASTEXITCODE -ne 0 -or -not $current) {
    git remote add origin $Remote | Out-Host
} elseif ($current -ne $Remote) {
    git remote set-url origin $Remote | Out-Host
}
Write-Host "origin = $Remote"

if ($Push) {
    Step '推送到 GitHub'
    git push -u origin main | Out-Host
    if (-not $SkipTag) { git push origin $tag | Out-Host }
    Write-Host '推送完成。' -ForegroundColor Green
} else {
    Write-Host ''
    Write-Host '未加 -Push，只做了本地准备。要真推上去请执行：' -ForegroundColor Yellow
    Write-Host "  git push -u origin main"
    if (-not $SkipTag) { Write-Host "  git push origin $tag" }
}

Write-Host ''
Write-Host '接下来在 GitHub 网页上建 Release：' -ForegroundColor Cyan
Write-Host "  1) 打开 $Remote/releases/new"
Write-Host "  2) 选择标签 $tag，标题填 $version"
Write-Host "  3) 正文直接粘贴 release/$version/release-notes-$version.md"
Write-Host "  4) 上传 release/$version/ 里的全部资产（安装包、.sha256、源码包、插件包、update.json、SHA256SUMS.txt）"
Write-Host "  5) 如果装了 gh CLI，也可以：gh release create $tag --title $version --notes-file release/$version/release-notes-$version.md release/$version/*"
