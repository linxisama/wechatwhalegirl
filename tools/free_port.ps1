# 清理占用 8091 端口的"旧后台"。
#
# 什么时候需要它：控制台提示"后台使用了其他目录的配置"或"检测到旧版或其他后台占用 8091"。
# 典型原因：源码版后台（start.bat / wechat_main.py）没关，或上一次控制台被强杀留下了后台。
#
# 安全边界：**只结束确实是我们自己后台的进程** —— 先请求 /api/runtime，
# 只有返回的 app 标识是 WhaleGirlWeChat 才动手；端口上是别的程序就只提示、不动它。
$ErrorActionPreference = 'Stop'
$port = 8091
$url = "http://127.0.0.1:$port/api/runtime"

Write-Host "检查 $port 端口…"
try {
    $response = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing
    # PowerShell 5.1 默认按 ANSI 解码 JSON，这里显式按 UTF-8 解一遍，避免中文路径变乱码
    $json = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
} catch {
    Write-Host "端口没有被占用，无需清理。" -ForegroundColor Green
    exit 0
}

if ($json.app -ne 'WhaleGirlWeChat') {
    Write-Host "端口上有别的程序在跑（app=$($json.app)），不会动它。" -ForegroundColor Yellow
    Write-Host "请自行确认并关闭它，或改端口后重新打开控制台。"
    exit 1
}

Write-Host "发现本项目的旧后台：PID $($json.pid)，数据目录 $($json.data_root)"
Stop-Process -Id $json.pid -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$still = $null
try { Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing | Out-Null; $still = $true } catch { $still = $false }
if ($still) {
    Write-Host "关闭失败，请到任务管理器手动结束 PID $($json.pid)。" -ForegroundColor Red
    exit 1
}
Write-Host "已关闭旧后台，端口已释放。现在可以重新打开控制台。" -ForegroundColor Green
exit 0
