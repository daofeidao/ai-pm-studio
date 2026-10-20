# setup-magpie-workbuddy.ps1
# 用途：Magpie + WorkBuddy 多账号 smart 路由初始化（安装 → 装插件 → 登录 → 建路由组 → 指向网关 → 签到）
# 运行：右键"使用 PowerShell 运行"，或在 WorkBuddy 对话里让它执行
#
# 用法：
#   .\setup-magpie-workbuddy.ps1                     # 默认 2 个账号（推荐：先小规模验证风控）
#   .\setup-magpie-workbuddy.ps1 -Accounts 3         # 三个账号
#   .\setup-magpie-workbuddy.ps1 -MagpieCli "D:\tools\magpie-cli-windows-amd64.exe"
#
# 风险提示：多账号同 IP 登录在平台侧与"多账号同设备"特征重合，封号代价不是脚本能消除的。
#          强烈建议先用 2 个账号跑一两周确认没触发风控，再把第三个加进路由组。

[CmdletBinding()]
param(
    [int]$Accounts = 2,
    [string]$MagpieCli = "magpie",
    [string]$WorkBuddyConfigDir = $env:WORKBUDDY_CONFIG_DIR
)

$ErrorActionPreference = "Stop"

function Test-Magpie {
    try { & $MagpieCli version 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0 -or $null -ne $LASTEXITCODE) }
    catch { return $false }
}

# ── 0. 前置检查：magpie 命令是否存在 ──────────────────────────────────────
if (-not (Test-Magpie)) {
    Write-Host "未找到 magpie 命令。请先安装：" -ForegroundColor Red
    Write-Host "  下载页：https://usemagpie.ai" -ForegroundColor Yellow
    Write-Host "  或 release 中的 magpie-cli-windows-amd64.exe（纯终端版）" -ForegroundColor Yellow
    Write-Host "  把 exe 所在目录加入 PATH，或用 -MagpieCli 指定绝对路径后重跑。" -ForegroundColor Yellow
    exit 1
}

Write-Host "magpie 版本：" -ForegroundColor DarkGray
& $MagpieCli version

# ── 1. 安装 WorkBuddy 社区插件 ────────────────────────────────────────────
Write-Host "`n==> 安装 WorkBuddy 插件..." -ForegroundColor Cyan
& $MagpieCli plugin add "@magpie-community/opencode-workbuddy-auth"

# ── 2. 逐个登录账号（OAuth 必须人工在浏览器完成） ────────────────────────
Write-Host "`n==> 登录 WorkBuddy 账号（共 $Accounts 个，逐个完成浏览器授权）" -ForegroundColor Cyan
Write-Host "每完成一个账号的授权，回到这里按 Enter 继续下一个。" -ForegroundColor Yellow

for ($i = 1; $i -le $Accounts; $i++) {
    Write-Host "`n--- 第 $i 个账号 ---" -ForegroundColor Green
    & $MagpieCli accounts add workbuddy
    Write-Host "请在浏览器中完成第 $i 个 WorkBuddy 账号的授权。" -ForegroundColor Yellow
    Read-Host "完成后按 Enter 继续"
}

# ── 3. 确认账号列表 ──────────────────────────────────────────────────────
Write-Host "`n==> 当前 Magpie 识别到的 WorkBuddy 账号：" -ForegroundColor Cyan
& $MagpieCli accounts list workbuddy

# ── 4. 创建 smart 路由组（多账号合为一个 group） ─────────────────────────
$GroupName = "workbuddy-$($Accounts)x"
Write-Host "`n==> 创建 smart 路由组 $GroupName ..." -ForegroundColor Cyan
& $MagpieCli group add $GroupName models=workbuddy/workbuddy-default routing=smart

# ── 5. 把 WorkBuddy 指向路由组 ───────────────────────────────────────────
Write-Host "`n==> 将 WorkBuddy 指向路由组..." -ForegroundColor Cyan
if ($WorkBuddyConfigDir) {
    $env:WORKBUDDY_CONFIG_DIR = $WorkBuddyConfigDir
    Write-Host "使用 WORKBUDDY_CONFIG_DIR=$WorkBuddyConfigDir" -ForegroundColor DarkGray
}
& $MagpieCli workbuddy "group/$GroupName" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "自动回写失败，请手动把 WorkBuddy 的 base URL 设为 http://127.0.0.1:3425/v1" -ForegroundColor Yellow
    Write-Host "并把模型名填为 group/$GroupName" -ForegroundColor Yellow
}

# ── 6. 每日签到（积分主要来源） ───────────────────────────────────────────
Write-Host "`n==> 执行每日签到..." -ForegroundColor Cyan
& $MagpieCli accounts checkin

Write-Host "`n==> 打开每日自动签到：Magpie → Settings → Accounts → 开启 automatic daily check-in" -ForegroundColor Yellow

# ── 7. 输出状态 ──────────────────────────────────────────────────────────
Write-Host "`n==> 当前额度状态：" -ForegroundColor Cyan
& $MagpieCli quota

Write-Host "`n初始化完成。" -ForegroundColor Green
Write-Host "网关地址：http://127.0.0.1:3425/v1"
Write-Host "WorkBuddy 的模型：group/$GroupName"
Write-Host "如果 WorkBuddy 仍显示旧模型，重启 WorkBuddy 会话。"
Write-Host ""
Write-Host "编排器侧对应配置（pm.config.json）：" -ForegroundColor DarkGray
Write-Host "  agents.magpie.enabled = true"
Write-Host "  agents.magpie.base_url = http://127.0.0.1:3425/v1"
Write-Host "  agents.magpie.model = group/$GroupName"
Write-Host "（dispatch-agent 的 WorkBuddy 分支会自动优先走 Magpie；网关不可达时回退直连，不阻塞循环）" -ForegroundColor DarkGray
