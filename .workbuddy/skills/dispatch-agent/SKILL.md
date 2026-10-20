# dispatch-agent（项目级 skill）

> 三段式路由（脚本优先 → WorkBuddy → OpenCode）+ worktree 生命周期。
> 对应架构：ARCHITECTURE-v2 §7 T04；P0-3 / P1-2 / P1-4。

## 调用约定

```bash
# 自检（三分支 + worktree 闭环，临时仓库 Git 全真）
python .workbuddy/skills/dispatch-agent/dispatcher.py --selftest

# 由 pm-orchestrator 在 DISPATCHING 相调用（不单独对用户暴露 run 子命令）
```

Python 解释器固定 managed 3.13.12（`pm.config.json` 的 `python` 字段）。

## 三段式路由

| 顺序 | 条件 | 执行器 | 说明 |
|------|------|--------|------|
| ① | `ScriptRouter.match(instruction)` 命中 `script-index.json` | worktree 内跑脚本 | **零 LLM、零额度**；`PM_OUTPUT_DIR=<wt>/work` |
| ② | 未命中且 `QuotaInfo.max_percent > 90`（非 stale） | `WorkBuddyProvider` | 同进程直写 main 的 `work/`，零额外内存 |
| ③ | 否则 | `OpenCodeProvider` | worktree 路径作 `opencode run` 的 `<directory>`，带 `--auto` |

`stale=true`（两源都查不到）**不拦截**，放行继续派活（v2 R9）。

## Magpie 集成（T7）

`WorkBuddyProvider` 在 `pm.config.json` 的 `agents.magpie` 打开时，**经 Magpie 网关**
路由到多账号 smart 路由组：

```json
"agents": {
  "magpie": {
    "enabled": true,
    "base_url": "http://127.0.0.1:3425/v1",
    "model": "group/workbuddy-3x",
    "timeout_s": 3
  }
}
```

| 情形 | 行为 | 记录 |
|------|------|------|
| `enabled=true` + 网关可达 | 走网关，`model=group/workbuddy-3x` | — |
| `enabled=true` + 网关不可达 | **回退直连**，**不阻塞循环** | `capability_gap`（`capabilities.json`） |
| `enabled=false` | 直连（默认 WorkBuddy 行为） | — |

网关探测：`GET {base_url}/models`，超时 3s（`capability_probe._probe_magpie_gateway`），
任何异常都吞掉转 gap，**绝不 raise 断主流程**。

运维脚本（**已就绪，勿重写**）：

| 脚本 | 用途 |
|------|------|
| `tools/magpie/check-magpie-health.ps1` | 日常巡检：账号列表 / 额度 / 路由组 / 网关连通性 |
| `tools/magpie/setup-magpie-workbuddy.ps1` | 初始化：装插件 → 登录 → 建路由组 → 指向网关 → 签到 |

> 路由逻辑全在 Magpie 内部；**不要改 dispatch-agent 来修「多账号只涨一个用量」**
> （那是 Magpie 路由组配置问题，见巡检脚本的排障提示）。

## worktree 生命周期

```
add：git worktree add worktrees/task-<id> -B task-<id> main   （主工作区永不切分支）
复用：worktree 已存在 -> git reset --hard main（只重置已跟踪文件，保留 _reverse/ 副本）
执行：脚本 cwd=worktree；OpenCode 子进程 cwd=worktree，跑完退出
diff：git diff --name-only main...task-<id>（三点式 = merge-base 差异；WB 路径走 git status）
merge：git merge --no-ff task-<id>（冲突 → merge --abort → E_MERGE_CONFLICT 转人工）
回收：git worktree remove --force + git branch -d task-<id>
```

> **为什么要 `reset --hard` 复用**：代理写入的内容若与既有 commit 相同，
> `git commit` 会空转（nothing to commit）→ task 分支无新 commit →
> `git diff main...task` 为空 → REVIEWING_DIFF 误判「无任何改动」→ 白白耗尽
> `max_rework`。`reset --hard` **不动未跟踪文件**，故逆向副本
> `worktrees/<id>/_reverse/copy/` 得以保留。

## 安全边界

1. `--auto` 可带：worktree 已隔离，代理破坏被限制在 task 分支内。
2. **不可逆操作（git push / 部署 / 删文件）OpenCode 不做**：只写进 `AgentResult.irreversible_ops`，
   由编排器在人工审批后在 main 上执行（v2 §10.11）。
3. `--continue` / `--session` **仅本机**接续；跨机一律 `--file TASK.md` 冷启动。
4. 重做循环 `≤ max_rework`（默认 2）：验收不过携带 diff 反馈重派，超限 `E_DIFF_REJECTED=95`。
5. `script-index.json` 的 `exclude`（`阿虎-*`、`截图-CDP-v014~v027`）为安全网，命中即跳过。

## 错误码

`E_SCRIPT_NOT_FOUND=60` / `E_AGENT_TIMEOUT=42` / `E_AGENT_CRASH=40` /
`E_DIFF_REJECTED=95` / `E_GIT_WORKTREE_FAILED=93` / `E_MERGE_CONFLICT=94`。
