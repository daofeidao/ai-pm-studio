# check-magpie-health.ps1
# 用途：日常巡检——检查 WorkBuddy 多账号额度、路由组状态、网关连通性
# 运行：按需手动执行，或挂到 Windows 计划任务

[CmdletBinding()]
param(
    [string]$MagpieCli = "magpie",
    [string]$Gateway = "http://127.0.0.1:3425/v1"
)

$ErrorActionPreference = "Continue"

if (-not (Get-Command $MagpieCli -ErrorAction SilentlyContinue)) {
    Write-Host "未找到 magpie 命令，跳过 CLI 检查（若已装但未在 PATH，请用 -MagpieCli 指定绝对路径）" -ForegroundColor Yellow
} else {
    Write-Host "==> 账号列表" -ForegroundColor Cyan
    & $MagpieCli accounts list workbuddy

    Write-Host "`n==> 额度（每个账号还剩多少）" -ForegroundColor Cyan
    & $MagpieCli quota

    Write-Host "`n==> 路由组" -ForegroundColor Cyan
    & $MagpieCli group list 2>&1
}

Write-Host "`n==> 网关连通性 ($Gateway/models)" -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "$Gateway/models" -UseBasicParsing -TimeoutSec 5
    Write-Host "网关正常，HTTP $($r.StatusCode)" -ForegroundColor Green
} catch {
    Write-Host "网关不可达：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "尝试启动 Magpie 应用或 TUI：magpie tui" -ForegroundColor Yellow
    Write-Host "（若选择常驻：把 Magpie 设为登录启动项或 Windows 服务，避免每轮重新拉起）" -ForegroundColor DarkGray
}

Write-Host "`n==> 排障提示" -ForegroundColor DarkGray
Write-Host "若三个账号的用量始终只涨一个，说明路由组没生效——大概率是模型名/组名不匹配。" -ForegroundColor DarkGray
Write-Host "此时不要改 dispatch-agent，先修 Magpie 的组配置（路由逻辑全在 Magpie 内部）。" -ForegroundColor DarkGray
