"""agent_provider —— AgentProvider 抽象与四个实现（v2 §4.2 / §6 / §7 T04）。

- AgentProvider（ABC）：run(task, handoff_path) / check_quota() / capabilities()
- WorkBuddyProvider：同进程执行器（WB 即编排器宿主），run() 返回 executed_in_process
- OpenCodeProvider：CLI 子进程，永远在 git worktree 内工作；build_command 按 v2 §6.2 模板
- MockOpenCodeProvider：demo 路径——**真实 worktree + 真实 commit**，结果取 DEMO_AGENT_RESULT
- MockWorkBuddyProvider：demo 路径——main 的 work/ 写 demo 文件并 commit

退出码映射（v2 §6.3）：0 -> success；超时 kill -> E_AGENT_TIMEOUT=42；其他非零 ->
E_AGENT_CRASH=40（error_detail=stderr 尾 500 字符）；json 取不到 text -> E_AGENT_RESULT_INVALID=41。
不可逆操作（push/deploy/rm 等）只记入 irreversible_ops，由编排器审批后执行。
"""
from __future__ import annotations

import json
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pm_common
from pm_common import (
    E_AGENT_CRASH,
    E_AGENT_RESULT_INVALID,
    E_AGENT_TIMEOUT,
    E_OPENCODE_BAD_JSON,
    E_SCRIPT_NOT_FOUND,
    PMError,
    atomic_write_text,
    utc_now_iso,
)
from quota import QuotaInfo, QuotaService

#: 不可逆操作关键词（回答中出现即记入 irreversible_ops，v2 §6.3）
IRREVERSIBLE_KEYWORDS: tuple[str, ...] = (
    "git push",
    "push origin",
    "git push origin",
    "deploy",
    "部署",
    "rm -rf",
    "删除文件",
    "格式化",
    "drop database",
)


def detect_irreversible_ops(text: str) -> list[str]:
    """从文本中探测不可逆操作标记。"""
    low = text.lower()
    ops: list[str] = []
    for kw in IRREVERSIBLE_KEYWORDS:
        if kw.lower() in low:
            ops.append(kw)
    # 显式声明优先：【不可逆】git_push
    for m in ("【不可逆】", "[irreversible]"):
        if m in text:
            for tok in ("git_push", "deploy", "rm_files"):
                if tok in text:
                    ops.append(tok)
    return sorted(set(ops))


# ---------------------------------------------------------------------------
# 数据结构（v2 §4.2 / §4.6）
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    """派活任务书（v2 增加 acceptance / allowed_paths；v2.1 增加 reverse_target）。"""

    id: str = "task-000"
    instruction: str = ""
    irreversible: bool = False
    script_route: str | None = None
    acceptance: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=lambda: ["work/"])
    reverse_target: str | None = None  # v2.1：逆向任务的目标文件路径（dispatcher 复制到 worktree 副本）


@dataclass
class AgentResult:
    """代理执行结果（v2 增加 error_detail / session_id）。"""

    status: str = "success"
    agent: str = ""
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    irreversible_ops: list[str] = field(default_factory=list)
    error_code: int | None = None
    error_detail: str | None = None
    session_id: str | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# json 解析（v2 §6.3）
# ---------------------------------------------------------------------------

def _dig_text(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _extract_text(obj: dict) -> str:
    """按多候选键路径探测 text 字段（兼容未固化 schema，Q4）。"""
    for path in (("text",), ("message", "content"), ("content",), ("parts",)):
        v = _dig_text(obj, path)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            joined = "".join(
                p.get("text", "") for p in v if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
            if joined.strip():
                return joined
    return ""


def parse_opencode_json(raw_stdout: str) -> AgentResult:
    """解析 `opencode run --format json` 输出。

    1) 先试整串 json.loads；2) 失败按行 NDJSON 解析，取**最后一个能探测到 text** 的对象；
    3) 字段多候选探测：text / sessionID / tokens；4) 取不到 text -> E_AGENT_RESULT_INVALID=41。
    """
    obj: Any = None
    text = (raw_stdout or "").strip()
    if text:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and _extract_text(candidate):
                    obj = candidate  # 最后一个含 text 的对象
    if not isinstance(obj, dict):
        raise PMError(E_OPENCODE_BAD_JSON, "opencode json 输出不可解析（整串与按行均失败）")

    out_text = _extract_text(obj)
    if not out_text.strip():
        raise PMError(E_AGENT_RESULT_INVALID, "opencode json 中取不到 text 字段")

    session = None
    for k in ("sessionID", "session_id", "sessionId"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            session = v
            break
    return AgentResult(
        status="success",
        agent="opencode",
        summary=out_text.strip()[:200],
        artifacts=[],
        irreversible_ops=detect_irreversible_ops(out_text),
        session_id=session,
        duration_ms=0,
    )


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class AgentProvider(ABC):
    """代理执行器接口。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any], git: Any) -> None:
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.git = git

    @abstractmethod
    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        """执行任务并返回结果。"""

    @abstractmethod
    def check_quota(self) -> QuotaInfo:
        """查询该代理额度。"""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """能力描述（供 capabilities.json / 看板）。"""


def worktree_rel_for(task: TaskSpec) -> str:
    """task 分支 worktree 相对路径（编排器/派发器/provider 三方一致的约定）。"""
    return f"worktrees/{task.id}"


# ---------------------------------------------------------------------------
# WorkBuddy 同进程执行器
# ---------------------------------------------------------------------------

class WorkBuddyProvider(AgentProvider):
    """WB 即编排器宿主本身：run() 后由当前会话把改动直接写在 main 的 work/ 下。"""

    kind = "workbuddy"

    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        # 同进程执行：不产子进程；改动由当前 WB 会话直写 main 的 work/，
        # REVIEWING_DIFF 走 git status 未提交改动审查 -> 提交（v2 R10）。
        return AgentResult(
            status="executed_in_process",
            agent="workbuddy",
            summary="改动由当前 WorkBuddy 会话直写 main 的 work/（同进程，零额外内存）",
            artifacts=[],
            irreversible_ops=[],
            session_id=None,
            duration_ms=0,
        )

    def check_quota(self) -> QuotaInfo:
        return QuotaInfo(
            agent="workbuddy",
            max_percent=0.0,
            windows={},
            source="in_process",
            fetched_at=utc_now_iso(),
            stale=False,
        )

    def capabilities(self) -> dict[str, Any]:
        return {"kind": "in_process", "memory": "~0"}


# ---------------------------------------------------------------------------
# OpenCode CLI 执行器
# ---------------------------------------------------------------------------

class OpenCodeProvider(AgentProvider):
    """OpenCode CLI：永远在 git worktree 内工作，跑完退出（v2 §6）。"""

    kind = "opencode"

    def __init__(self, root: Path | str, cfg: dict[str, Any], git: Any) -> None:
        super().__init__(root, cfg, git)
        self.quota_service = QuotaService(cfg)

    # ---- v2 §6.2 命令模板 ----

    def build_command(
        self,
        task: TaskSpec,
        worktree: Path,
        handoff_path: Path,
        session_id: str | None = None,
        continue_last: bool = False,
    ) -> list[str]:
        oc = self.cfg.get("opencode", {})
        cmd = [
            oc.get("exe", ""), "run",
            "--format", "json",
            "--auto",                                    # worktree 隔离，安全
            "--title", f"pm-{task.id}",
        ]
        model = (oc.get("model") or "").strip()
        if model:
            cmd += ["--model", model]                    # 为空则用 opencode 默认模型（Q5）
        cmd += ["--file", str(handoff_path)]             # HANDOFF.md 或 TASK.md（冷启动用 TASK.md）
        if session_id:
            cmd += ["--session", session_id]             # 仅本机接续
        if continue_last:
            cmd += ["--continue"]                        # 仅本机接续上次 session
        cmd += [task.instruction, str(worktree)]         # <message...> <directory>
        return cmd

    # ---- 执行 ----

    def _ensure_worktree(self, task: TaskSpec) -> Path:
        rel = worktree_rel_for(task)
        wt = self.root / rel
        if wt.exists():
            return wt
        return self.git.worktree_add(rel, task.id, self.git.default_branch)

    def _save_raw(self, task_id: str, raw: str) -> str:
        """原始输出整体落盘 reports/opencode-<task-id>-raw.json（解析失败也落盘）。"""
        p = self.root / "reports" / f"opencode-{task_id}-raw.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, raw or "")
        return str(p.relative_to(self.root))

    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        started = time.time()
        wt = self._ensure_worktree(task)
        cmd = self.build_command(task, wt, handoff_path)
        timeout_s = float(self.cfg.get("opencode", {}).get("timeout_s", 1800))
        try:
            proc = pm_common.run_cli(cmd, cwd=wt, timeout_s=timeout_s)
        except pm_common.CliTimeout:
            return AgentResult(
                status="failed",
                agent="opencode",
                error_code=E_AGENT_TIMEOUT,
                error_detail=f"opencode 超时({timeout_s}s)，已 kill 进程树",
                duration_ms=int((time.time() - started) * 1000),
            )
        self._save_raw(task.id, proc.stdout or "")
        if proc.returncode != 0:
            return AgentResult(
                status="failed",
                agent="opencode",
                error_code=E_AGENT_CRASH,
                error_detail=(proc.stderr or "")[-500:],
                duration_ms=int((time.time() - started) * 1000),
            )
        try:
            result = parse_opencode_json(proc.stdout or "")
        except PMError as e:
            return AgentResult(
                status="failed",
                agent="opencode",
                error_code=e.code,
                error_detail=e.message,
                duration_ms=int((time.time() - started) * 1000),
            )
        result.duration_ms = int((time.time() - started) * 1000)
        return result

    def check_quota(self) -> QuotaInfo:
        return self.quota_service.fetch()

    def capabilities(self) -> dict[str, Any]:
        return {"kind": "cli", "worktree_isolation": True, "auto": True}


# ---------------------------------------------------------------------------
# Mock 实现（demo：Git 全真、执行假）
# ---------------------------------------------------------------------------

class MockOpenCodeProvider(OpenCodeProvider):
    """demo：真实 worktree + 真实 commit，结果取 demo_fixtures.DEMO_AGENT_RESULT。"""

    kind = "opencode-mock"

    def __init__(self, root, cfg, git, irreversible: bool = False) -> None:
        super().__init__(root, cfg, git)
        self.irreversible = irreversible

    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        from demo_fixtures import (
            DEMO_AGENT_RESULT,
            DEMO_AGENT_RESULT_IRREVERSIBLE,
            DEMO_WORKTREE_FILES,
        )

        started = time.time()
        wt = self._ensure_worktree(task)
        # 真实写文件 + 真实 commit 到 task 分支（Git 真）
        for rel, content in DEMO_WORKTREE_FILES.items():
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, content)
        self.git._git(["add", "-A"], cwd=wt)
        self.git._git(
            ["commit", "-m", f"demo: {task.id} 登录页产物"], cwd=wt, env=self.git._identity_env()
        )
        data = DEMO_AGENT_RESULT_IRREVERSIBLE if self.irreversible else DEMO_AGENT_RESULT
        result = AgentResult(
            status=data["status"],
            agent=data["agent"],
            summary=data["summary"],
            artifacts=list(data["artifacts"]),
            irreversible_ops=list(data["irreversible_ops"]),
            error_code=data.get("error_code"),
            error_detail=data.get("error_detail"),
            session_id=data.get("session_id"),
            duration_ms=int((time.time() - started) * 1000),
        )
        # raw 落盘（与真实路径一致的行为）
        self._save_raw(task.id, json.dumps(data, ensure_ascii=False, indent=2))
        return result


class MockWorkBuddyProvider(WorkBuddyProvider):
    """demo：在 main 的 work/ 写 demo 文件并 commit（Git 真、执行假）。

    产物文件名与 MockOpenCodeProvider 不同（wb-demo-login.html），避免同内容文件
    导致 task 分支 diff 为空（验收需要可见改动）。
    """

    kind = "workbuddy-mock"

    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        from demo_fixtures import DEMO_WORKTREE_FILES

        html = DEMO_WORKTREE_FILES["work/demo-login.html"]
        written: list[str] = []
        p = self.root / "work" / "wb-demo-login.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, html.replace("演示登录页", "演示登录页（WorkBuddy 同进程产物）"))
        written.append("work/wb-demo-login.html")
        self.git.stage_and_commit(written, f"demo(wb): {task.id} 登录页产物")
        return AgentResult(
            status="success",
            agent="workbuddy",
            summary="MockWorkBuddyProvider：main 的 work/ 写入 demo 产物并提交",
            artifacts=written,
            irreversible_ops=[],
            session_id=None,
            duration_ms=0,
        )
