"""orchestrator —— pm-orchestrator skill 入口：17 态状态机 + Git 交接编排（v2 §4/§5/§7）。

用法：
    python .workbuddy/skills/pm-orchestrator/orchestrator.py init [--demo] [--remote URL]
    python .workbuddy/skills/pm-orchestrator/orchestrator.py run "任务" [--demo] [--max-rounds N] [--demo-irreversible]
    python .workbuddy/skills/pm-orchestrator/orchestrator.py resume [--demo]
    python .workbuddy/skills/pm-orchestrator/orchestrator.py status [--demo]

设计铁律（v2 §10）：
  - Git 即单一事实源：main 承载产出与交接三件套；pm/state 孤儿分支只放 current.json 指针
  - 主工作区永不切分支：task 分支走 worktree；pm/state 走 plumbing
  - 每次 _transition 即 commit pm/state；每轮 PERSISTING commit main + best-effort push
  - demo 模式：Git 全真（含本地 bare remote），网页 AI/OpenCode/额度全 mock

退出码与错误码一致（v2 §10.8）。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# 本文件位于 <root>/.workbuddy/skills/pm-orchestrator/orchestrator.py，向上四级为仓库根
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

import pm_common
from pm_common import (
    E_AGENT_CRASH,
    E_HUMAN_REJECTED,
    E_MERGE_CONFLICT,
    E_OK,
    E_STATE_CORRUPT,
    E_WEB_AI_SELECTOR,
    PMError,
    atomic_write_json,
    atomic_write_text,
    append_ndjson,
    compact_ts,
    load_config,
    read_text_if_exists,
    utc_now_iso,
)
from git_ops import GitOps
from handoff_io import (
    HandoffContext,
    HandoffDoc,
    HandoffIO,
    ReturnBlock,
    write_initial_files,
)
import webai_adapter as wa
from webai_adapter import ContextUsage, build_adapter
from quota import QuotaInfo, QuotaService
from agent_provider import AgentResult, TaskSpec, worktree_rel_for
from capability_probe import CapabilityProbe
from permissions import PermissionGuard, SecurityAuditor
from activity import ActivityLog

# memory.py 与 dispatcher.py 是 skill 脚本（位于 .workbuddy/skills/*/），动态加载
sys.path.insert(0, str(ROOT / ".workbuddy" / "skills" / "pm-memory"))
sys.path.insert(0, str(ROOT / ".workbuddy" / "skills" / "dispatch-agent"))
from memory import MemoryPack, MemoryService  # type: ignore
from dispatcher import Dispatcher  # type: ignore


# ---------------------------------------------------------------------------
# 17 态状态机（v2 §4.1）
# ---------------------------------------------------------------------------

class LoopPhase(str, Enum):
    """循环相位（v2：17 态）。"""

    IDLE = "IDLE"
    PLANNING = "PLANNING"
    ASKING_WEB_AI = "ASKING_WEB_AI"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    PARSING = "PARSING"
    DISPATCHING = "DISPATCHING"
    PREPARING_WORKTREE = "PREPARING_WORKTREE"
    EXECUTING = "EXECUTING"
    REVIEWING_DIFF = "REVIEWING_DIFF"
    MERGING = "MERGING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COLLECTING = "COLLECTING"
    FEEDING_BACK = "FEEDING_BACK"
    PERSISTING = "PERSISTING"
    HANDING_OFF = "HANDING_OFF"
    DONE = "DONE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


#: COLLECTING 阶段 `git status --porcelain` 的放行前缀 —— 编排器自己的记账/证据目录。
#: 其余任何 worktree 之外的改动都视为越界（见 `_collect_escape_check`）。
COLLECT_OWN_PREFIXES: tuple[str, ...] = (
    "worktrees/",
    "reports/",
    "dashboard/",
    "memory/",
    "commands/",
    "state/",
    "activity.jsonl",
    "SESSION.md",
    "HANDOFF.md",
    ".demo-remote/",
    "__pycache__/",
)


#: 网页 AI 回复「任务完成」语义标记（据此收尾不派活）
DONE_MARKERS: tuple[str, ...] = ("已完成", "无需继续派活", "任务完成", "无需进一步改动")


def parse_web_reply(text: str) -> tuple[list[str], list[str], bool]:
    """从网页 AI 回复解析（动作清单, 验收标准, 是否收尾）。

    动作清单：`【动作清单】` 段的编号行；验收标准：`【验收标准】` 段的 `-` 行或编号行。
    """
    actions: list[str] = []
    acceptance: list[str] = []
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "动作清单" in line and line.startswith("【"):
            section = "actions"
            continue
        if "验收标准" in line and line.startswith("【"):
            section = "acceptance"
            continue
        if line.startswith("【") and section is not None:
            # 进入其他段（如【结论】）→ 当前段结束
            section = None
            continue
        if section == "actions":
            import re

            m = re.match(r"^(\d+)[.、)]\s*(.+)$", line)
            if m:
                actions.append(m.group(2).strip())
        elif section == "acceptance":
            if line.startswith("-"):
                acceptance.append(line.lstrip("-* ").strip())
            else:
                import re

                m = re.match(r"^(\d+)[.、)]\s*(.+)$", line)
                if m:
                    acceptance.append(m.group(2).strip())
    done = any(k in text for k in DONE_MARKERS)
    return actions, acceptance, done


# ---------------------------------------------------------------------------
# 指令通道（commands/queue.jsonl，append-only NDJSON，单消费者）
# ---------------------------------------------------------------------------

class CommandQueue:
    """审批/指令通道：append(cmd) / consume()（读后按 at 排序，打 consumed 标记）。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any]) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / cfg.get("paths", {}).get("queue", "commands/queue.jsonl")

    def append(self, cmd: dict[str, Any]) -> None:
        obj = dict(cmd)
        obj.setdefault("cmd", "")
        obj.setdefault("id", "")
        obj.setdefault("by", "user")
        obj.setdefault("note", "")
        obj.setdefault("at", utc_now_iso())
        append_ndjson(self.path, obj)

    def consume(self) -> list[dict[str, Any]]:
        """读全部未消费行（按 at 排序返回），处理后在行内打 consumed=true（重写文件）。"""
        if not self.path.exists():
            return []
        raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        rewritten: list[str] = []
        for ln in raw_lines:
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                rewritten.append(s)  # 注释行等原样保留
                continue
            if isinstance(obj, dict) and not obj.get("consumed"):
                obj["consumed"] = True
                out.append(obj)
            rewritten.append(json.dumps(obj, ensure_ascii=False))
        out.sort(key=lambda o: str(o.get("at", "")))
        if out:
            atomic_write_text(self.path, "\n".join(rewritten) + "\n")
        return out


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------

class Orchestrator:
    """可重入状态机：唯一写 current.json 与交接三件套的组件（v2 §4.2）。"""

    def __init__(self, root: Path | str | None = None, demo: bool = False) -> None:
        self.root = Path(root).resolve() if root is not None else ROOT
        cfg_path = self.root / "pm.config.json"
        self.cfg = load_config(cfg_path if cfg_path.exists() else None)
        self.demo = demo or bool(self.cfg.get("demo", {}).get("enabled", False))
        self.git = GitOps(self.root, self.cfg)
        self.handoff = HandoffIO(self.root)
        self.memory = MemoryService(self.root, self.cfg)
        self.probe = CapabilityProbe(self.root, self.cfg)
        self.queue = CommandQueue(self.root, self.cfg)
        self.dispatcher = Dispatcher(
            self.root, self.cfg, self.git, demo=self.demo,
            demo_irreversible=getattr(self, "demo_irreversible", False),
        )
        self.quota_service = QuotaService(self.cfg, demo=self.demo)
        self.activity = ActivityLog(self.root, "activity.jsonl")
        self.current: dict[str, Any] = self._default_state()
        self._last_result: AgentResult | None = None
        self._last_reply_text: str = ""
        self._goal: str = ""
        self._round_done: bool = False
        self._max_rounds: int = int(self.cfg.get("loop", {}).get("max_rounds", 10))
        self.max_rework: int = int(self.cfg.get("loop", {}).get("max_rework", 2))
        #: 连续失败熔断阈值（同一 task 连续失败 N 次 -> PAUSED，不再烧额度）
        self.circuit_threshold: int = int(
            self.cfg.get("loop", {}).get("circuit_breaker_threshold", 3)
        )
        #: 上一次审批是否处于「无决策、落盘等待」状态（_run_task 据此停止）
        self._awaiting_approval: bool = False
        #: 派发前 main 工作区的脏文件基线（collect 阶段只报**新增**越界，不误伤既有改动）
        self._dirty_before_dispatch: set[str] | None = None
        # T2：demo 演练「主路径选择器缺失」开关
        self.demo_selector_missing: bool = False
        # T3：demo 演练「代理越界写 worktree 之外」开关
        self.demo_escape_write: bool = False

    # ------------------------------------------------------------------
    # 状态基础
    # ------------------------------------------------------------------

    def _default_state(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "project": self.cfg.get("project", "pm-software"),
            "status": "IDLE",
            "phase": "IDLE",
            "round": 0,
            "iteration": 0,
            "active_agent": "",
            "task_id": None,
            "lock": None,
            "pending_approval": None,
            "last_commit": "",
            "last_push_ok": False,
            #: 连续失败计数（成功即清零；到阈值 -> circuit_open）
            "consecutive_failures": 0,
            #: 熔断标志（True 时状态为 PAUSED，需人工 resume）
            "circuit_open": False,
            "updated_at": utc_now_iso(),
        }

    def _load_current(self) -> dict[str, Any]:
        cur = self.git.read_current_json()
        if cur is None:
            return self._default_state()
        merged = self._default_state()
        merged.update(cur)
        return merged

    def _sync_state(self, **fields: Any) -> str:
        """合并字段写 current.json（不改变 phase 的轻量同步）。"""
        self.current.update(fields)
        self.current["updated_at"] = utc_now_iso()
        return self.git.write_current_json(self.current)

    def _transition(self, to: "str | LoopPhase", **fields: Any) -> str:
        """状态转换：改 current.json 即 commit pm/state（v2 §10.6），并 append activity。"""
        phase = to.value if isinstance(to, LoopPhase) else str(to)
        previous = str(self.current.get("phase", ""))
        self.current.update(fields)
        self.current["phase"] = phase
        self.current["iteration"] = int(self.current.get("iteration", 0)) + 1
        sha = self.git.write_current_json(
            self.current, message=f"phase {phase} (round={self.current.get('round', 0)})"
        )
        # activity.jsonl 只追加：状态转换全量留痕（崩溃后可折叠出上下文）
        self.activity.phase(
            previous,
            phase,
            round_no=int(self.current.get("round", 0) or 0),
            task_id=self.current.get("task_id"),
        )
        suffix = f" task={self.current.get('task_id')}" if self.current.get("task_id") else ""
        print(f"[PM] phase {phase}{suffix}")
        return sha

    def _phase_hook(self, phase: str) -> None:
        """派发器相位回调：PREPARING_WORKTREE / EXECUTING / REVIEWING_DIFF。"""
        agent_map = {
            "PREPARING_WORKTREE": "git",
            "EXECUTING": self.dispatcher.last_route or "opencode",
            "REVIEWING_DIFF": "pm-orchestrator",
        }
        if phase == "PREPARING_WORKTREE" and self.current.get("task_id"):
            tid = self.current["task_id"]
            print(f"[PM] worktree worktrees/{tid} created (branch {tid})")
        self._transition(phase, active_agent=agent_map.get(phase, "pm-orchestrator"))

    # ------------------------------------------------------------------
    # 网页 AI / 额度
    # ------------------------------------------------------------------

    def _webai(self, round_no: int):
        return build_adapter(
            self.root,
            self.cfg,
            demo=self.demo,
            round_no=round_no,
            selector_missing=self.demo_selector_missing,
        )

    def _quota_info(self) -> QuotaInfo:
        """当前额度（demo 走假额度，可被 --demo-quota-percent 覆盖）。"""
        return self.quota_service.fetch()

    def set_demo_quota(self, percent: float) -> None:
        """demo 专属：覆盖 OpenCode 假额度百分比（>degrade_percent 触发降级）。"""
        self.quota_service.demo_percent = float(percent)
        self.dispatcher.quota.demo_percent = float(percent)
        self.dispatcher.quota._cache = None

    def _transcript_text(self) -> str:
        parts = [read_text_if_exists(self.handoff.session_path)]
        if self._last_reply_text:
            parts.append(f"# 最近回答\n{self._last_reply_text}")
        return "\n\n".join(p for p in parts if p.strip())

    # ------------------------------------------------------------------
    # 验收 / 崩溃恢复
    # ------------------------------------------------------------------

    def _review_diff(self, task: TaskSpec) -> bool:
        """REVIEWING_DIFF：改动 ∈ allowed_paths 且非空（v2 D9）。

        基线说明：task 分支自 main 分出，故 `git diff --name-only main...task-<id>`
        （三点式 = merge-base 差异）即为「本 task 相对 main 新增的改动」。
        用两点式 `main..task` 会把 main 上后续提交也算进来，属误判。
        """
        diff = list(self.dispatcher.last_diff)
        if not diff:
            if self.dispatcher.last_route in (None, "script", "opencode"):
                diff = self.git.diff_names(self.git.default_branch, task.id)
            else:
                diff = self.git.status_paths()
            self.dispatcher.last_diff = diff
        if not diff:
            print("[PM] REVIEWING_DIFF 未通过：无任何改动")
            return False
        bad = [
            p
            for p in diff
            if not any(p == a.rstrip("/") or p.startswith(a) for a in task.allowed_paths)
        ]
        if bad:
            print(f"[PM] REVIEWING_DIFF 未通过：越界改动 {bad}（allowed={task.allowed_paths}）")
            return False
        print(
            f"[PM] REVIEWING_DIFF 通过：diff ∈ {'/'.join(task.allowed_paths)}"
            f"（{len(diff)} 个文件：{'、'.join(diff[:5])}）"
        )
        return True

    def _recover_lock(self) -> str:
        """崩溃恢复（v2 §4.3 表）：清锁重派 / 清理重派 / 直接 REVIEWING_DIFF。"""
        cur = self.git.read_current_json() or {}
        lock = cur.get("lock")
        if not lock:
            return "no_lock"
        holder = lock.get("holder") or cur.get("task_id") or "task-000"
        wt_rel = lock.get("worktree") or f"worktrees/{holder}"
        branch = lock.get("branch") or holder
        wt_exists = (self.root / wt_rel).exists()
        if not wt_exists:
            # 崩溃残留：清锁 → 该 task 重派
            self._transition(
                LoopPhase.PREPARING_WORKTREE,
                lock=None,
                task_id=holder,
                status="RUNNING",
                active_agent="git",
            )
            print(f"[PM] 崩溃残留：worktree {wt_rel} 不存在，已清锁，准备重派 {holder}")
            return "cleared_stale"
        if self.git.branch_commit_count(branch, self.git.default_branch) > 0:
            # worktree 存在且 task 分支有 commit → 直接 REVIEWING_DIFF
            self._transition(LoopPhase.REVIEWING_DIFF, active_agent="pm-orchestrator")
            print(f"[PM] 恢复：task 分支 {branch} 已有 commit，直接进入 REVIEWING_DIFF")
            return "reviewing"
        # 代理未落任何改动：清理 worktree 重派
        self.git.worktree_remove(wt_rel)
        self._transition(
            LoopPhase.PREPARING_WORKTREE,
            lock=None,
            task_id=holder,
            status="RUNNING",
            active_agent="git",
        )
        print(f"[PM] 恢复：{branch} 无 commit，已清理 worktree，准备重派")
        return "cleared_empty"

    # ------------------------------------------------------------------
    # 人工审批（HUMAN_REVIEW）
    # ------------------------------------------------------------------

    def _human_review(self, rnd: int, spec: TaskSpec, result: AgentResult) -> int:
        apr_id = f"apr-{int(self.current.get('iteration', 0)) + 1:04d}"
        apr = {
            "id": apr_id,
            "op_type": "irreversible_ops",
            "command": "; ".join(result.irreversible_ops),
            "requested_by": result.agent,
            "requested_at": utc_now_iso(),
        }
        self._transition(
            LoopPhase.HUMAN_REVIEW, status="WAITING_APPROVAL", pending_approval=apr
        )
        print(f"[PM] HUMAN_REVIEW：不可逆操作待审批 id={apr_id} cmd={apr['command']}")
        print(
            f"[PM] 审批方式：向 commands/queue.jsonl 追加 "
            f'{{"cmd":"approve","id":"{apr_id}"}}（或 "reject"），然后执行 resume'
        )
        cmds = self.queue.consume()
        decision = next(
            (
                c
                for c in cmds
                if c.get("id") == apr_id and c.get("cmd") in ("approve", "reject")
            ),
            None,
        )
        if decision is None:
            # 无决策：落盘等待（状态 WAITING_APPROVAL，可 resume）
            # 关键：标记为「等待中」，调用方收到后必须停止，绝不能继续合并/收尾
            self._awaiting_approval = True
            self._persist_files(rnd, spec, result)
            self._push_best_effort()
            return E_OK
        if decision["cmd"] == "approve":
            ok = self.git.push(self.git.default_branch)
            print(f"[PM] 已批准并执行不可逆操作（push origin main ok={ok}）")
            self._transition(
                LoopPhase.EXECUTING, pending_approval=None, status="RUNNING"
            )
            return E_OK
        # 否决 → reject_action（默认 handover → PAUSED）
        action = str(self.cfg.get("loop", {}).get("reject_action", "handover"))
        self._transition(
            LoopPhase.PAUSED, status="PAUSED", pending_approval=None, lock=None, task_id=None
        )
        self._persist_files(
            rnd, spec, result, error=f"人工否决不可逆操作（{apr['command']}）"
        )
        self._push_best_effort()
        print(f"[PM] 已否决（reject_action={action}）-> PAUSED，可 resume 继续")
        return E_HUMAN_REJECTED

    # ------------------------------------------------------------------
    # 持久化（PERSISTING）
    # ------------------------------------------------------------------

    def _push_best_effort(self) -> bool:
        """push best-effort：失败记 last_push_ok=false，不阻塞（v2 D13）。"""
        ok_main = self.git.push(self.git.default_branch)
        ok_state = self.git.push_state()
        ok = bool(ok_main and ok_state)
        self._sync_state(last_push_ok=ok)
        print(f"[PM] push origin main + pm/state -> {'ok' if ok else 'FAILED(best-effort)'}")
        return ok

    def _persist_files(
        self,
        rnd: int,
        spec: TaskSpec | None,
        result: AgentResult | None,
        reply: str = "",
        error: str | None = None,
    ) -> str:
        """写报告 / ↪ Return / HANDOFF 重写 / TASK.md 重写，并 commit main。"""
        ts = compact_ts()
        report_rel = f"reports/round-{rnd}-{ts}.md"
        agent = result.agent if result else "pm-orchestrator"
        changed = list(self.dispatcher.last_diff) if result else []
        if result is not None and result.artifacts and not changed:
            changed = list(result.artifacts)

        if error:
            test_result = f"失败：{error}"
        elif result is not None:
            test_result = f"demo 自检通过；diff {len(changed)} 个文件 ∈ work/"
        else:
            test_result = "仅汇报轮（无需派活）"

        report_lines = [
            f"# 轮次报告 round-{rnd}",
            "",
            f"- 时间：{utc_now_iso()}",
            f"- 目标：{self._goal}",
            f"- 执行代理：{agent}",
            f"- 改动清单：{'、'.join(changed) if changed else '（无）'}",
            f"- 测试结果：{test_result}",
        ]
        if result is not None:
            report_lines.append(f"- 结果摘要：{result.summary}")
            if result.session_id:
                report_lines.append(f"- session：{result.session_id}（仅本机 --continue 可用）")
            if result.irreversible_ops:
                report_lines.append(f"- 不可逆操作：{'、'.join(result.irreversible_ops)}")
        if reply:
            report_lines += ["", "## 网页 AI 回答", "", reply.strip()]
        atomic_write_text(self.root / report_rel, "\n".join(report_lines) + "\n")

        # SESSION.md ↪ Return（只追加）
        if error:
            did = f"round-{rnd} 以失败收尾：{error}"
            next_step = "人工介入或修正后 resume"
        elif result is not None:
            did = result.summary
            next_step = "询问 DeepSeek 下一轮动作或收尾"
        else:
            did = "网页 AI 判定任务完成，无需派活"
            next_step = "进入持久化与交接"
        block = ReturnBlock(
            ts=utc_now_iso(),
            agent=agent,
            did=did,
            changed_files=changed,
            test_result=test_result,
            report_path=report_rel,
            next_step=next_step,
            note=(f"session={result.session_id}" if result and result.session_id else ""),
        )
        self.handoff.append_return(block)

        # HANDOFF.md 重写（五块）
        anchors = self.memory.load_anchors()
        constraints = anchors.constraints or [
            "内存可用仅 2.68GB，零常驻进程",
            "不可逆操作必须人工审批",
        ]
        if error:
            todos: list[str] = [f"处理 round-{rnd} 失败：{error}"]
        elif self._round_done or spec is None:
            todos = []  # 已完成/仅汇报：待办为空（渲染占位行，解析回空列表）
        else:
            todos = [f"继续推进：{self._goal}"]
        blockers = [] if self.demo else ["DeepSeek 会话需保持 Edge 登录态"]
        ctx = HandoffContext(
            goal=self._goal,
            constraints=constraints,
            key_files=[(p, "本轮改动") for p in changed[:8]] or [("work/", "项目产出目录")],
            todos=todos,
            blockers=blockers,
            hostname=socket.gethostname(),
        )
        handoff_content = self.handoff.render_handoff(ctx)
        self.handoff.write_handoff(handoff_content)

        # TASK.md（ODAF，据 HANDOFF + 最后 ↪ Return + current）
        doc = self.handoff.parse_handoff(handoff_content)
        last = self.handoff.read_last_return()
        task_ctx = dict(self.current)
        task_ctx.update(
            {
                "round": rnd,
                "task_id": spec.id if spec else None,
                "acceptance": spec.acceptance if spec else [],
                "allowed_paths": spec.allowed_paths if spec else ["work/"],
            }
        )
        self.handoff.write_task(self.handoff.build_task_odaf(doc, last, task_ctx))

        # anchors 进度（无错误轮才固化）
        if error is None:
            pack = self.memory.compress(self._transcript_text())
            pack.current_question = self._goal
            self.memory.update_anchors(pack)

        sha = self.git.stage_and_commit(
            ["reports", "SESSION.md", "HANDOFF.md", "TASK.md", "memory", "commands"],
            f"round-{rnd}: 持久化报告与交接三件套",
        )
        self._sync_state(last_commit=sha or self.current.get("last_commit", ""))
        self._derive_state_js()
        return sha

    def _finish_round(
        self,
        rnd: int,
        spec: TaskSpec | None = None,
        result: AgentResult | None = None,
        reply: str = "",
    ) -> bool:
        """收尾一轮：PERSISTING -> push -> 看板派生 -> FEEDING_BACK 或 DONE。"""
        self._transition(
            LoopPhase.PERSISTING, active_agent="pm-orchestrator", lock=None, task_id=None
        )
        sha = self._persist_files(rnd, spec, result, reply=reply)
        ok = self._push_best_effort()
        more = bool(spec is not None) and (rnd < self._max_rounds) and not self._round_done
        if more:
            self._transition(
                LoopPhase.FEEDING_BACK, last_commit=sha, last_push_ok=ok
            )
        else:
            self._transition(
                LoopPhase.HANDING_OFF, last_commit=sha, last_push_ok=ok
            )
            self._transition(LoopPhase.DONE, status="DONE")
            print("[PM] phase HANDING_OFF → DONE → exit 0")
        return more

    # ------------------------------------------------------------------
    # demo 基线复位（T1：保证 diff 可观测）
    # ------------------------------------------------------------------

    def _demo_reset_baseline(self) -> None:
        """demo 模式：清理 main 上与夹具**逐字节相同**的上一轮演示产物。

        为什么需要：demo 产物会被合并回 main，下一轮 task 分支写入同样内容时
        `git diff main...task` 为空 -> REVIEWING_DIFF 误判「无任何改动」。

        安全约束：**只删内容与夹具完全一致的文件**（`demo_baseline_reset_targets`
        逐字节比对），用户真实产出绝不会被误删。
        """
        from demo_fixtures import demo_baseline_reset_targets

        targets = demo_baseline_reset_targets(self.git, self.root)
        if not targets:
            return
        sha = self.git.remove_paths(targets, "chore(demo): 复位演示基线产物（保证 diff 可观测）")
        print(f"[PM] demo 基线复位：移除 {targets}（commit={sha or '无变更'}）")

    # ------------------------------------------------------------------
    # T2：网页 AI 选择器缺失 -> AWAITING_APPROVAL（人审，不崩溃）
    # ------------------------------------------------------------------

    def _await_selector_fix(self, rnd: int, err: PMError) -> int:
        """主路径选择器缺失且兜底不可用：进入 AWAITING_APPROVAL 等待人审。

        语义：不是「任务失败」，而是「需要一个外部决策」——人工提供选择器或
        安装兜底后端后 `resume` 继续。因此不置 FAILED，也不返回错误码。
        """
        apr_id = f"sel-{int(self.current.get('iteration', 0)) + 1:04d}"
        apr = {
            "id": apr_id,
            "op_type": "web_ai_selector",
            "command": "人工提供 web_ai.selectors 或安装 drissionpage-ai 后 resume",
            "requested_by": "ask-web-ai",
            "requested_at": utc_now_iso(),
            "detail": str(err),
        }
        self._transition(
            LoopPhase.HUMAN_REVIEW,
            status="WAITING_APPROVAL",
            pending_approval=apr,
            task_id=None,
            lock=None,
        )
        self.activity.approval("AWAITING_APPROVAL", task_id=None, op_type="web_ai_selector")
        print(f"[PM] 网页 AI 选择器缺失：{err}")
        print(f"[PM] HUMAN_REVIEW：进入 AWAITING_APPROVAL id={apr_id}（等待人工提供选择器）")
        print(
            "[PM] 解除方式：修正 pm.config.json 的 web_ai.selectors，或安装 drissionpage-ai；"
            "然后执行 resume"
        )
        self._persist_files(rnd, None, None, error=f"web_ai_selector 缺失：{err}")
        self._push_best_effort()
        return E_OK

    # ------------------------------------------------------------------
    # T3：`--auto` 事后安全检查（worktree 越界检测）
    # ------------------------------------------------------------------

    def _security_audit(self, spec: TaskSpec) -> list[str]:
        """执行后检测 worktree 之外是否被改动；返回越界路径清单。

        WorkBuddy 路由按设计**直接写主工作区**（同进程、零额外内存，不建 worktree），
        因此 provider 显式声明的产物路径不属于越界；其余改动照旧判违规。
        OpenCode / script 路由仍走 worktree，任何主工作区改动都是越界。
        """
        auditor = SecurityAuditor(self.root, worktree_rel_for(spec))
        before = auditor.snapshot()
        # 派发前的 main 脏文件基线：collect 阶段只报「派发后新增」的越界改动
        try:
            self._dirty_before_dispatch = set(self.git.status_paths())
        except Exception:  # noqa: BLE001 —— 基线取不到就不做增量过滤（从严）
            self._dirty_before_dispatch = None
        result = self.dispatcher.dispatch(
            spec,
            reviewer=self._review_diff,
            max_rework=self.max_rework,
            phase_hook=self._phase_hook,
        )
        violations = auditor.diff(before)
        self._last_result = result
        if self.dispatcher.last_route == "workbuddy":
            declared = {
                a.replace("\\", "/").strip("/") for a in (result.artifacts or [])
            }
            if declared:
                waived = [v for v in violations if v in declared]
                if waived:
                    print(
                        f"[PM] 安全审计：WB 路由在主工作区的已声明产物放行 "
                        f"{len(waived)} 处：{'、'.join(waived[:5])}"
                    )
                violations = [v for v in violations if v not in declared]
        return violations

    def _handle_security_violation(self, rnd: int, spec: TaskSpec, violations: list[str]) -> int:
        """发现 worktree 越界：记 security_violation 并 PAUSE（不自动续跑）。"""
        detail = "、".join(violations[:10])
        self.activity.security("worktree_escape", "PAUSE", detail=detail)
        print(f"[PM] 安全检查失败：worktree 之外被修改 {len(violations)} 处：{detail}")
        print("[PM] 已记 security_violation -> PAUSE（需人工确认后 resume）")
        self._transition(
            LoopPhase.PAUSED,
            status="PAUSED",
            pending_approval=None,
            lock=None,
            task_id=None,
        )
        self._persist_files(
            rnd, spec, self._last_result, error=f"security_violation：{detail}"
        )
        self._push_best_effort()
        return E_OK

    def _collect_escape_check(self) -> list[str]:
        """COLLECTING 阶段**无条件**检查 worktree 之外是否被改动。

        为什么必须无条件：实测 `opencode run` 不传 `--auto` 时 write 工具**照样**
        真实落盘（本次 status 仍是 completed 并创建了文件），所以 shell 权限位/
        `--auto` 开关都**不能**当作安全前提 —— 事后 `git status --porcelain`
        是唯一可靠的防线。这里不看任何 flag，每个成功轮次进 COLLECTING 前都跑。

        只报「相对派发前基线**新增**」的改动（`_dirty_before_dispatch`），
        并放行编排器自己的记账/证据目录（`COLLECT_OWN_PREFIXES`）。
        """
        dirty = self.git.status_paths()
        baseline = self._dirty_before_dispatch or set()
        violations: list[str] = []
        for raw in dirty:
            path = raw.replace("\\", "/")
            if path in baseline:
                continue
            if any(path.startswith(prefix) for prefix in COLLECT_OWN_PREFIXES):
                continue
            violations.append(path)
        return violations

    def _collect_guard(self, rnd: int, spec: TaskSpec) -> int | None:
        """无越界返回 None；有越界则记 security_violation -> PAUSE 并返回退出码。"""
        violations = self._collect_escape_check()
        if not violations:
            return None
        print(
            f"[PM] COLLECTING 越界检查：worktree 之外新增 {len(violations)} 处改动"
            f"（{', '.join(violations[:5])}）-> PAUSE"
        )
        return self._handle_security_violation(rnd, spec, violations)

    # ------------------------------------------------------------------
    # 派活 + 合并（一轮的任务执行）
    # ------------------------------------------------------------------

    def _run_task(self, rnd: int, spec: TaskSpec) -> tuple[bool, int]:
        """DISPATCHING -> (PREPARING_WORKTREE/EXECUTING/REVIEWING_DIFF) -> MERGING -> 收尾。

        连续失败熔断（离线可演练）：同一 task 失败后清理 worktree 重派，
        连续 `circuit_threshold`（默认 3）次失败 -> 熔断 PAUSED + circuit_open=true，
        不再继续烧额度；期间任何一次成功即清零计数。

        返回 (是否继续循环, 退出码)。
        """
        lock = {
            "state": "held",
            "holder": spec.id,
            "worktree": worktree_rel_for(spec),
            "branch": spec.id,
            "acquired_at": utc_now_iso(),
        }
        self._transition(
            LoopPhase.DISPATCHING, active_agent="dispatch-agent", task_id=spec.id, lock=lock
        )
        # T3：派发前后做 `--auto` 事后安全检查（worktree 越界 -> PAUSE）
        while True:
            violations = self._security_audit(spec)
            if violations:
                return False, self._handle_security_violation(rnd, spec, violations)
            result = self._last_result
            if result is None:
                result = AgentResult(
                    status="failed", agent="dispatch-agent", error_code=E_AGENT_CRASH,
                    error_detail="派发无结果",
                )
            if result.status in ("success", "executed_in_process"):
                self._sync_state(consecutive_failures=0, circuit_open=False)
                break

            # —— 失败：累计连续失败次数，判断是否熔断 ——
            fails = int(self.current.get("consecutive_failures", 0)) + 1
            opened = fails >= self.circuit_threshold
            self._sync_state(consecutive_failures=fails, circuit_open=opened)
            self.activity.circuit(
                f"连续失败 {fails}/{self.circuit_threshold}："
                f"{result.error_detail or result.status}",
                opened,
                rnd,
            )
            print(
                f"[PM] 代理失败（连续 {fails}/{self.circuit_threshold} 次）："
                f"{result.error_code} {(result.error_detail or '').strip()}".rstrip()
            )
            # 清理失败轮次的 worktree / task 分支，保证重派从干净基线出发
            self.git.worktree_remove(worktree_rel_for(spec))
            self.git.delete_branch(spec.id, force=True)
            if opened:
                return False, self._open_circuit(rnd, spec, result)
            print("[PM] 未到熔断阈值，重派同一 task")

        if result.irreversible_ops:
            self._awaiting_approval = False
            code = self._human_review(rnd, spec, result)
            if code != E_OK or self._awaiting_approval:
                # 无审批决策：落盘等待（状态 AWAITING_APPROVAL），不得继续合并
                return False, code
        if self.dispatcher.last_route != "workbuddy":
            self._transition(LoopPhase.MERGING, active_agent="pm-orchestrator")
            print(f"[PM] merge --no-ff {spec.id}")
            merge = self.git.merge_task_branch(spec.id, f"merge: {spec.id} {self._goal}")
            if not merge["ok"]:
                print(f"[PM] 合并冲突：{merge['conflicts']} -> 转人工")
                self._transition(
                    LoopPhase.HANDING_OFF, status="FAILED", lock=None, task_id=None
                )
                self._persist_files(
                    rnd, spec, result, error=f"合并冲突: {merge['conflicts']}"
                )
                self._push_best_effort()
                return False, E_MERGE_CONFLICT
            self.git.worktree_remove(worktree_rel_for(spec))
            self.git.delete_branch(spec.id)
        # T3/T5：进入 COLLECTING 前无条件跑 main 工作区 porcelain 检查（与 --auto 无关）
        guard = self._collect_guard(rnd, spec)
        if guard is not None:
            return False, guard
        self._transition(LoopPhase.COLLECTING, lock=None)
        more = self._finish_round(rnd, spec=spec, result=result)
        return more, E_OK

    def _open_circuit(self, rnd: int, spec: TaskSpec, result: AgentResult) -> int:
        """连续失败达阈值：熔断 -> PAUSED（不 FAILED，保留 resume 入口）。"""
        self.activity.circuit(
            f"连续 {self.circuit_threshold} 次失败，熔断", True, rnd
        )
        self._transition(
            LoopPhase.PAUSED,
            status="PAUSED",
            circuit_open=True,
            pending_approval=None,
            lock=None,
            task_id=None,
        )
        print(
            f"[PM] 熔断：连续 {self.circuit_threshold} 次失败 -> PAUSED"
            "（人工排查后写入 commands/queue.jsonl 的 resume 指令继续）"
        )
        self._persist_files(
            rnd,
            spec,
            result,
            error=(
                f"circuit_open：连续 {self.circuit_threshold} 次失败"
                f"（{result.error_code} {result.error_detail or ''}）".strip()
            ),
        )
        self._push_best_effort()
        return E_OK

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self, start_round: int) -> int:
        """round = start_round .. max_rounds 的多轮闭环。"""
        for rnd in range(max(1, start_round), self._max_rounds + 1):
            self._round_done = False
            # —— 记忆注入判定 ——
            question = (
                f"【项目目标】{self._goal}\n请给出本轮【动作清单】与【验收标准】。"
            )
            usage = self._webai(rnd).get_context_usage()
            if self.memory.should_inject(usage):
                pack = self.memory.compress(self._transcript_text())
                pack.current_question = question
                self.memory.update_anchors(pack)
                question = self.memory.render_pack(pack)
                print(f"[PM] 记忆包已注入（usage={usage.chars_used} ≥ {usage.chars_limit}）")

            # —— 提问 ——
            self._transition(
                LoopPhase.ASKING_WEB_AI,
                round=rnd,
                active_agent="deepseek",
                status="RUNNING",
            )
            try:
                reply = self._webai(rnd).send_prompt(question)
            except PMError as e:
                if e.code == E_WEB_AI_SELECTOR:
                    # T2：主路径选择器缺失且兜底不可用 -> 转人审（不崩溃）
                    return self._await_selector_fix(rnd, e)
                print(f"[PM] 网页 AI 失败：{e}")
                self._transition(LoopPhase.FAILED, status="FAILED")
                self._persist_files(rnd, None, None, error=str(e))
                self._push_best_effort()
                return e.code
            self._last_reply_text = reply.text
            self.memory.archive_transcript(
                rnd, f"# Q\n{question}\n\n# A\n{reply.text}\n"
            )

            # —— 解析 ——
            self._transition(LoopPhase.PARSING, active_agent="pm-orchestrator")
            actions, acceptance, done = parse_web_reply(reply.text)
            self._round_done = done
            print(
                f"[PM] PARSING：动作 {len(actions)} 项 / 验收 {len(acceptance)} 项 / "
                f"收尾语义={'是' if done else '否'}"
            )
            if done or not actions:
                # 无需派活（仅汇报）-> PERSISTING
                more = self._finish_round(rnd, reply=reply.text)
                if not more:
                    return E_OK
                continue

            # —— 派活 ——
            spec = TaskSpec(
                id=f"task-{rnd:03d}",
                instruction=f"{self._goal}；" + "；".join(actions),
                acceptance=acceptance,
                allowed_paths=["work/"],
            )
            more, code = self._run_task(rnd, spec)
            if not more:
                return code
        return E_OK

    # ------------------------------------------------------------------
    # 公开命令
    # ------------------------------------------------------------------

    def init(self, demo: bool = False, remote: str | None = None) -> int:
        """初始化仓库（demo：本地 bare remote）+ pm/state + 交接三件套 + 能力探测。"""
        remote_url = (
            remote if remote is not None else (self.cfg.get("git", {}).get("remote") or None)
        )
        self.git.init_repo(remote=remote_url, demo=demo or self.demo)
        write_initial_files(self.root)
        anchors_path = self.root / self.cfg.get("paths", {}).get(
            "anchors", "memory/anchors.json"
        )
        if not anchors_path.exists():
            atomic_write_json(
                anchors_path,
                {"decisions": [], "constraints": [], "preferences": [], "progress": ""},
            )
        caps = self.probe.probe_all()
        gaps = caps.get("capability_gaps", [])
        print(f"[PM] 能力探测：{json.dumps(caps['agents'], ensure_ascii=False)}")
        if gaps:
            print(f"[PM] 能力缺口 {len(gaps)} 条（不阻塞）：{[g['agent'] for g in gaps]}")
        sha = self.git.stage_and_commit(
            ["HANDOFF.md", "SESSION.md", "TASK.md", "memory", "commands", "capabilities.json"],
            "chore: 初始化交接三件套与能力探测",
        )
        self.current = self._load_current()
        self._derive_state_js()
        print(f"[PM] init 完成：branch={self.git.current_branch()} commit={sha or '(无变更)'}")
        print("[PM] pm/state 初始 current.json 就绪；主工作区保持 main（永不切分支）")
        return E_OK

    def run(self, task: str, max_rounds: int | None = None) -> int:
        """启动新一轮闭环（P1 主链路 + P2 循环）。"""
        if max_rounds:
            self._max_rounds = int(max_rounds)
        self._goal = task.strip()
        if not self.git.is_repo():
            print("[PM] 尚未 init，自动执行 init(demo=%s)" % self.demo)
            self.init(demo=self.demo)
        self.current = self._load_current()
        if self.current.get("lock"):
            self._recover_lock()
        if self.demo:
            # T1：demo 基线复位（必须在建 worktree 之前，否则 diff 为空）
            self._demo_reset_baseline()
        self._transition(
            LoopPhase.PLANNING,
            status="RUNNING",
            round=0,
            active_agent="pm-orchestrator",
            task_id=None,
            pending_approval=None,
            consecutive_failures=0,
            circuit_open=False,
        )
        print(f"[PM] run: {self._goal}（max_rounds={self._max_rounds}）")
        return self._loop(1)

    def resume(self) -> int:
        """跨机/中断接续：fetch pm/state -> 读指针 -> 恢复 -> 生成 TASK.md -> 续跑。"""
        self.git.fetch(self.git.state_branch)
        cur = self._load_current()
        self.current = cur
        phase = str(cur.get("phase", "IDLE"))
        status = str(cur.get("status", "IDLE"))
        print(f"[PM] resume: phase={phase} status={status} round={cur.get('round')}")
        self._goal = self._goal_from_handoff() or self._goal

        # 0) PAUSED：需 resume 指令放行
        if status == "PAUSED":
            cmds = self.queue.consume()
            if not any(c.get("cmd") == "resume" for c in cmds):
                print('[PM] 当前 PAUSED；向 commands/queue.jsonl 追加 {"cmd":"resume"} 后可继续')
                return E_OK
            self._transition(
                LoopPhase.PLANNING, status="RUNNING",
                consecutive_failures=0, circuit_open=False,
            )
            return self._loop(max(1, int(cur.get("round", 1))))

        # 1) 等待审批
        if status == "WAITING_APPROVAL" or phase == LoopPhase.HUMAN_REVIEW.value:
            apr = cur.get("pending_approval") or {}
            cmds = self.queue.consume()
            decision = next(
                (
                    c
                    for c in cmds
                    if c.get("id") == apr.get("id")
                    and c.get("cmd") in ("approve", "reject")
                ),
                None,
            )
            if decision is None:
                print("[PM] 仍等待审批（commands/queue.jsonl 未见 approve/reject）")
                return E_OK
            if decision["cmd"] == "approve":
                self.git.push(self.git.default_branch)
                self._transition(
                    LoopPhase.MERGING, pending_approval=None, status="RUNNING"
                )
                return self._continue_after_review()
            self._transition(
                LoopPhase.PAUSED, status="PAUSED", pending_approval=None, lock=None
            )
            print("[PM] 已否决 -> PAUSED")
            return E_HUMAN_REJECTED

        # 2) 崩溃恢复
        if cur.get("lock"):
            action = self._recover_lock()
            if action == "reviewing":
                return self._continue_after_review()
            return self._redispatch(cur)

        # 3) 终态
        if phase == LoopPhase.DONE.value:
            self._regenerate_task_md()
            print("[PM] 已是 DONE 终态；TASK.md 已按 ODAF 重新生成（跨机冷启动输入就绪）")
            return E_OK
        if phase == LoopPhase.FAILED.value:
            print("[PM] 上次以 FAILED 终态；请修正后重新 run")
            return E_STATE_CORRUPT

        # 4) 中段中断（无锁）：重新生成 TASK.md 并从当前 round 续跑
        self._regenerate_task_md()
        start = max(1, int(cur.get("round", 1)))
        self._transition(LoopPhase.PLANNING, status="RUNNING")
        return self._loop(start)

    def status(self) -> int:
        """打印 current.json / 分支 / worktree 列表。"""
        cur = self.git.read_current_json()
        if cur is None:
            print("(current.json 不存在：未 init 或 pm/state 未建立)")
        else:
            print(json.dumps(cur, ensure_ascii=False, indent=2))
        print(f"[branch] {self.git.current_branch()}")
        proc = self.git._git(["worktree", "list"])
        sys.stdout.write(proc.stdout)
        state_js = self.root / self.cfg.get("paths", {}).get(
            "state_js", "dashboard/state.js"
        )
        print(f"[dashboard] state.js {'存在' if state_js.exists() else '缺失（gitignore 派生物）'}")
        return E_OK

    # ------------------------------------------------------------------
    # 续跑辅助
    # ------------------------------------------------------------------

    def _goal_from_handoff(self) -> str:
        try:
            text = read_text_if_exists(self.handoff.handoff_path)
            if text.strip():
                return self.handoff.parse_handoff(text).goal
        except PMError:
            pass
        return ""

    def _regenerate_task_md(self) -> None:
        """跨机接续：据 HANDOFF + 最后 ↪ Return + current 生成 TASK.md（ODAF）并提交。"""
        doc = HandoffDoc(goal=self._goal)
        text = read_text_if_exists(self.handoff.handoff_path)
        if text.strip():
            try:
                doc = self.handoff.parse_handoff(text)
            except PMError:
                doc = HandoffDoc(goal=self._goal)
        if not doc.goal:
            doc.goal = self._goal or "（未填写目标）"
        last = self.handoff.read_last_return()
        content = self.handoff.build_task_odaf(doc, last, self.current)
        self.handoff.write_task(content)
        sha = self.git.stage_and_commit(["TASK.md"], "chore: 跨机接续生成 TASK.md（ODAF）")
        self._sync_state(last_commit=sha or self.current.get("last_commit", ""))
        self._derive_state_js()
        print("[PM] TASK.md 已按 ODAF 四段生成（冷启动输入就绪）")

    def _spec_from_current(self, cur: dict[str, Any]) -> TaskSpec:
        rnd = int(cur.get("round", 1))
        task_id = cur.get("task_id") or f"task-{rnd:03d}"
        return TaskSpec(
            id=task_id,
            instruction=self._goal or "续跑任务",
            acceptance=list(cur.get("acceptance") or []),
            allowed_paths=["work/"],
        )

    def _redispatch(self, cur: dict[str, Any]) -> int:
        """崩溃清锁后重派该 task。"""
        spec = self._spec_from_current(cur)
        lock = {
            "state": "held",
            "holder": spec.id,
            "worktree": worktree_rel_for(spec),
            "branch": spec.id,
            "acquired_at": utc_now_iso(),
        }
        self._transition(
            LoopPhase.DISPATCHING, active_agent="dispatch-agent", task_id=spec.id, lock=lock
        )
        try:
            self._dirty_before_dispatch = set(self.git.status_paths())
        except Exception:  # noqa: BLE001 —— 基线取不到就不做增量过滤（从严）
            self._dirty_before_dispatch = None
        result = self.dispatcher.dispatch(
            spec, reviewer=self._review_diff, max_rework=self.max_rework,
            phase_hook=self._phase_hook,
        )
        self._last_result = result
        if result.status not in ("success", "executed_in_process"):
            self._transition(LoopPhase.FAILED, status="FAILED", lock=None, task_id=None)
            self._persist_files(int(cur.get("round", 1)), spec, result)
            self._push_best_effort()
            return result.error_code or E_AGENT_CRASH
        return self._merge_and_finish(int(cur.get("round", 1)), spec, result)

    def _continue_after_review(self) -> int:
        """审批批准后：合并 task 分支 -> 收尾该轮。"""
        cur = self.current
        spec = self._spec_from_current(cur)
        rnd = int(cur.get("round", 1))
        if not self._review_diff(spec):
            return self._redispatch(cur)
        return self._merge_and_finish(rnd, spec, self._last_result)

    def _merge_and_finish(
        self, rnd: int, spec: TaskSpec, result: AgentResult | None
    ) -> int:
        if self.dispatcher.last_route != "workbuddy":
            self._transition(LoopPhase.MERGING, active_agent="pm-orchestrator")
            merge = self.git.merge_task_branch(spec.id, f"merge: {spec.id}（续跑）")
            if not merge["ok"]:
                self._transition(LoopPhase.HANDING_OFF, status="FAILED", lock=None, task_id=None)
                return E_MERGE_CONFLICT
            self.git.worktree_remove(worktree_rel_for(spec))
            self.git.delete_branch(spec.id)
        # T5：续跑路径同样无条件做 main 工作区越界检查
        guard = self._collect_guard(rnd, spec)
        if guard is not None:
            return guard
        self._transition(LoopPhase.COLLECTING, lock=None)
        more = self._finish_round(rnd, spec=spec, result=result)
        if more:
            return self._loop(rnd + 1)
        return E_OK

    # ------------------------------------------------------------------
    # 看板派生（derive don't mirror，v2 R7）
    # ------------------------------------------------------------------

    def _derive_state_js(self) -> None:
        """从 current.json + SESSION tail + reports + git log 派生 dashboard/state.js。"""
        current = self.git.read_current_json() or self.current
        tail = self.handoff.read_session_tail(3)
        reports = sorted((self.root / "reports").glob("round-*.md"))
        gitlog = self.git.log_oneline(self.git.default_branch, 10)
        anchors = self.memory.load_anchors()
        try:
            caps = json.loads(
                (self.root / self.cfg.get("paths", {}).get(
                    "capabilities", "capabilities.json"
                )).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            caps = {"capability_gaps": []}

        quota = self._quota_info()
        phase = str(current.get("phase", "IDLE"))
        board_done = [b.did[:48] for b in tail]
        board_executing: list[str] = []
        if phase in (
            LoopPhase.DISPATCHING.value,
            LoopPhase.PREPARING_WORKTREE.value,
            LoopPhase.EXECUTING.value,
            LoopPhase.REVIEWING_DIFF.value,
            LoopPhase.MERGING.value,
        ):
            who = current.get("active_agent") or "opencode"
            board_executing = [f"{who}: {self._goal}"[:48]]
        board_asking = (
            ["DeepSeek"]
            if phase in (LoopPhase.ASKING_WEB_AI.value, LoopPhase.WAITING_RESPONSE.value)
            else []
        )
        board_approval = (
            [current["pending_approval"]["id"]]
            if current.get("pending_approval")
            else []
        )
        board_todo: list[str] = []
        if phase not in (LoopPhase.DONE.value, LoopPhase.IDLE.value) and self._goal:
            board_todo = [self._goal[:48]]

        view = {
            "generated_at": utc_now_iso(),
            "loop": {
                "status": current.get("status"),
                "phase": phase,
                "round": current.get("round", 0),
                "iteration": current.get("iteration", 0),
                "active_agent": current.get("active_agent", ""),
                "task_id": current.get("task_id"),
                "goal": self._goal,
                "max_rounds": self._max_rounds,
                "updated_at": current.get("updated_at"),
            },
            "quota": {
                "opencode": quota.to_dict(),
                "workbuddy": {"note": "同进程执行，无外部额度"},
            },
            "board": {
                "todo": board_todo,
                "asking": board_asking,
                "executing": board_executing,
                "approval": board_approval,
                "done": board_done,
            },
            "memory": {
                "last_injected_at": getattr(self, "_last_injected_at", None),
                "threshold_chars": self.memory.threshold_chars,
                "usage_chars": int(self.memory.threshold_chars * 0.7),
                "anchors": {
                    "decisions": anchors.decisions,
                    "constraints": anchors.constraints,
                    "preferences": anchors.preferences,
                    "progress": anchors.progress,
                },
            },
            "approvals": (
                [dict(current["pending_approval"], status="pending")]
                if current.get("pending_approval")
                else []
            ),
            "capability_gaps": caps.get("capability_gaps", []),
            "history": gitlog,
            "reports": [str(p.relative_to(self.root)) for p in reports[-10:]],
            "session_tail": [
                {
                    "ts": b.ts,
                    "agent": b.agent,
                    "did": b.did[:80],
                    "report_path": b.report_path,
                }
                for b in tail
            ],
        }
        state_js = self.root / self.cfg.get("paths", {}).get(
            "state_js", "dashboard/state.js"
        )
        atomic_write_text(
            state_js,
            "window.PM_STATE = " + json.dumps(view, ensure_ascii=False, indent=2) + ";\n",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """argparse 入口：init / run / resume / status，全局 --demo。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--demo", action="store_true", default=argparse.SUPPRESS,
        help="demo 模式：Git 全真，网页 AI/OpenCode/额度全 mock",
    )

    parser = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="pm-orchestrator：17 态状态机 + Git 交接编排（零常驻）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", parents=[common], help="初始化仓库与 pm/state")
    p_init.add_argument("--remote", default=None, help="git remote 地址（留空跳过 push）")

    p_run = sub.add_parser("run", parents=[common], help="启动任务闭环")
    p_run.add_argument("task", help="自然语言任务")
    p_run.add_argument("--max-rounds", type=int, default=None, help="最大轮次（默认取配置）")
    p_run.add_argument(
        "--demo-irreversible", action="store_true",
        help="demo：注入不可逆操作以演练 HUMAN_REVIEW 审批链路",
    )
    p_run.add_argument(
        "--demo-selector-missing", action="store_true",
        help="demo：演练网页 AI 主路径选择器缺失（应转 AWAITING_APPROVAL 而非崩溃）",
    )
    p_run.add_argument(
        "--demo-escape-write", action="store_true",
        help="demo：演练代理越界写 worktree 之外（应记 security_violation 并 PAUSE）",
    )
    p_run.add_argument(
        "--demo-agent-fail", action="store_true",
        help="demo：代理每次派活都失败（演练「连续失败 3 次 -> 熔断 PAUSED」）",
    )
    p_run.add_argument(
        "--demo-quota-percent", type=float, default=None,
        help="demo：覆盖 OpenCode 假额度百分比（>90 触发「降级 WorkBuddy」）",
    )

    sub.add_parser("resume", parents=[common], help="跨机/中断接续")
    sub.add_parser("status", parents=[common], help="查看状态")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return E_OK

    orch = Orchestrator(demo=bool(getattr(args, "demo", False)))
    if getattr(args, "demo_irreversible", False):
        orch.dispatcher.demo_irreversible = True
    if getattr(args, "demo_selector_missing", False):
        orch.demo_selector_missing = True
    if getattr(args, "demo_escape_write", False):
        orch.demo_escape_write = True
        orch.dispatcher.demo_escape_write = True
    if getattr(args, "demo_agent_fail", False):
        orch.dispatcher.demo_agent_fail = True
    if getattr(args, "demo_quota_percent", None) is not None:
        orch.set_demo_quota(float(args.demo_quota_percent))
    try:
        if args.cmd == "init":
            return orch.init(demo=bool(getattr(args, "demo", False)), remote=args.remote)
        if args.cmd == "run":
            return orch.run(args.task, max_rounds=args.max_rounds)
        if args.cmd == "resume":
            return orch.resume()
        if args.cmd == "status":
            return orch.status()
    except PMError as e:
        print(f"[PM] 错误：{e}")
        return e.code
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
