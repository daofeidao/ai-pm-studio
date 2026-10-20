# pm-orchestrator（项目级 skill）

> 17 态可重入状态机 + Git 交接编排：问 → 取答 → 派活 → worktree 隔离执行 → 验收合并 → 持久化 → 交接。
> 对应架构：ARCHITECTURE-v2 §4/§5/§7 T02+T05；PRD P0-4 / P1-1 / US-1~US-5。

## 调用约定

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"

# 一次性初始化（建仓库 + 本地 bare remote + pm/state 孤儿分支 + 交接三件套）
$PY .workbuddy/skills/pm-orchestrator/orchestrator.py init --demo

# 端到端单轮（问→派→worktree→diff→merge→commit→push→交接→看板派生）
$PY .workbuddy/skills/pm-orchestrator/orchestrator.py run --demo "演示：生成登录页"

# 多轮循环（max_rounds=2 自动收尾）
$PY .workbuddy/skills/pm-orchestrator/orchestrator.py run --demo "演示任务" --max-rounds 2

# 跨机模拟（clone demo remote 到临时目录后 resume）
$PY .workbuddy/skills/pm-orchestrator/orchestrator.py resume --demo

# 查看状态
$PY .workbuddy/skills/pm-orchestrator/orchestrator.py status
```

## 环境自检（skill 同步说明，解法 A）

**若当前 WorkBuddy 会话未加载本仓库 `.workbuddy/skills/` 下的 4 个项目级 skill
（pm-orchestrator / ask-web-ai / dispatch-agent / pm-memory），请先把目录复制到用户级
skills 目录后重试：**

```
xcopy /E /I .workbuddy\skills %USERPROFILE%\.workbuddy\skills
```

原因：WB 不自动发现仓库内 `.workbuddy/skills/`（v2 R6/Q7）；仓库随 clone 走可开箱即用，
但同步非自动，避免静默失效。复制后本 SKILL.md 及其余三份随之生效。

自检命令（全部应 exit 0）：

```bash
$PY lib/pm_common.py --selftest
$PY lib/git_ops.py --selftest
$PY lib/handoff_io.py --selftest
$PY lib/quota.py --selftest
$PY .workbuddy/skills/ask-web-ai/web_ai.py --selftest
$PY .workbuddy/skills/dispatch-agent/dispatcher.py --selftest
$PY .workbuddy/skills/pm-memory/memory.py --selftest
$PY lib/capability_probe.py --selftest
```

## 17 态语义表

| 相位 | 语义 | 落盘动作 |
|------|------|---------|
| IDLE | 空闲 | — |
| PLANNING | 规划：读 current.json / 崩溃恢复判定 | commit pm/state |
| ASKING_WEB_AI | 向网页 AI 提问（可带记忆包） | commit pm/state |
| WAITING_RESPONSE | 等待回答（CDP 轮询） | commit pm/state |
| PARSING | 解析动作清单 + 验收标准 | commit pm/state |
| DISPATCHING | 三段式路由决策 | commit pm/state + lock=held |
| PREPARING_WORKTREE | `git worktree add worktrees/task-<id> -b task-<id> main` | commit pm/state |
| EXECUTING | 代理在 worktree 内执行（跑完退出） | commit pm/state |
| REVIEWING_DIFF | `diff --name-only main...task-<id>` 验收（∈ allowed_paths） | commit pm/state |
| MERGING | `merge --no-ff` 回 main；冲突 → 转人工 | commit pm/state |
| HUMAN_REVIEW | 不可逆操作待人工审批（pending_approval） | commit pm/state |
| COLLECTING | 收集结果 / 报告 | commit pm/state |
| FEEDING_BACK | 未完成且 round<max_rounds → 下一轮 | commit pm/state |
| PERSISTING | commit main（reports/SESSION/HANDOFF）+ best-effort push | commit pm/state + main |
| HANDING_OFF | 交接就绪 | commit pm/state |
| DONE / PAUSED / FAILED | 终态（可 resume：PAUSED） | commit pm/state |

## 零常驻约定

- 按需启动、跑完退出；无常驻进程、无常驻 HTTP 服务、无 socket。
- 看板 = 单文件 HTML（Edge 只读）轮询派生的 `dashboard/state.js`（meta refresh 2s）。
- 跨层通信只走文件系统 + git CLI。

## 交接协议（三文件，都在 main）

- `HANDOFF.md`：五块结构，每轮 PERSISTING 由编排器重写（Q8 默认）。
- `SESSION.md`：append-only，只追加 `## ↪ Return` 块（无合并冲突关键）。
- `TASK.md`：ODAF 四段（Outcome/Data/Action/Format），接手方冷启动输入。

## 审批通道

`commands/queue.jsonl`（append-only NDJSON）：
`{"cmd":"approve|reject|pause|resume|retry","id":"apr-xxxx","by":"user","note":"","at":"<iso>"}`
`consume()` 读后打 `consumed:true`（单消费者）；否决默认 `handover` → PAUSED，可 resume。

## 错误码（退出码=错误码）

`E_WEB_AI_TIMEOUT=30` `E_WEB_AI_NOT_LOGGED_IN=31` `E_CDP_UNREACHABLE=32`
`E_AGENT_CRASH=40` `E_AGENT_RESULT_INVALID=41` `E_AGENT_TIMEOUT=42` `E_OPENCODE_BAD_JSON=43`
`E_HUMAN_REJECTED=50` `E_SCRIPT_NOT_FOUND=60` `E_CAPABILITY_GAP=70` `E_STATE_CORRUPT=80`
`E_GIT_INIT_FAILED=90` `E_GIT_COMMIT_FAILED=91` `E_GIT_PUSH_FAILED=92` `E_GIT_WORKTREE_FAILED=93`
`E_MERGE_CONFLICT=94` `E_DIFF_REJECTED=95` `E_HANDOFF_IO=96`。
