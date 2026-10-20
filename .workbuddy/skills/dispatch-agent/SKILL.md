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

## worktree 生命周期

```
add：git worktree add worktrees/task-<id> -b task-<id> main   （主工作区永不切分支）
执行：脚本 cwd=worktree；OpenCode 子进程 cwd=worktree，跑完退出
diff：git diff --name-only main...task-<id>（WB 路径走 git status）
merge：git merge --no-ff task-<id>（冲突 → merge --abort → E_MERGE_CONFLICT 转人工）
回收：git worktree remove --force + git branch -d task-<id>
```

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
