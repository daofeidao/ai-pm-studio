"""activity —— append-only 活动日志（v2.1 架构强化⑤）。

设计（确定性内核 + 生成性外围分离）：
  - activity.jsonl 是**全量追加日志**：每次状态转换、LLM 调用、验证结果都 append 一行
    NDJSON，绝不被重写（崩溃安全、可审计）。
  - current.json 只留作**指针**（最后一条记录的折叠摘要 / 行号），不镜像全量。
  - 崩溃恢复：读 activity.jsonl 重建最近上下文（当前状态 = 日志折叠）。

事件类型（kind）：
  - "phase"       ：状态转换（from -> to, round, task_id）
  - "llm"         ：网页 AI 调用（provider, round, prompt_len, reply_len）
  - "dispatch"    ：派活（route, task_id, agent）
  - "verify"      ：验收判定（passed, accepted_n, failed_n, evidence）
  - "approval"    ：进入/离开审批（AWAITING_APPROVAL / HUMAN_REVIEW）
  - "circuit"     ：熔断触发（reason, blocked）
  - "security"    ：安全检查（violation, action）
  - "reverse"     ：逆向路由（loaded, target, copied_to）
  - "quota"       ：额度查询（max_percent, stale, source）
  - "recover"     ：崩溃恢复（action）
  - "error"       ：错误（code, detail）

仅标准库；原子 append（先写 .tmp 再 os.replace 保证行完整）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pm_common
from pm_common import append_ndjson, atomic_write_text, read_text_if_exists, utc_now_iso


class ActivityLog:
    """append-only 活动日志封装。"""

    def __init__(self, root: Path | str, path: str = "activity.jsonl") -> None:
        self.root = Path(root).resolve()
        self.path = self.root / path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """追加一条事件；返回该事件 dict（含 ts / kind / line）。"""
        event: dict[str, Any] = {"ts": utc_now_iso(), "kind": kind}
        event.update(fields)
        append_ndjson(self.path, event)
        return event

    # ------------------------------------------------------------------

    # 便捷封装（编排器/派发器统一调用，保持日志字段一致）

    def phase(self, fro: str, to: str, round_no: int = 0, task_id: str | None = None,
              detail: str = "") -> None:
        self.append("phase", frm=fro, to=to, round=round_no, task_id=task_id or "",
                    detail=detail)

    def llm(self, provider: str, round_no: int, prompt_len: int, reply_len: int,
            intent_valid: bool | None = None) -> None:
        self.append("llm", provider=provider, round=round_no,
                    prompt_len=prompt_len, reply_len=reply_len,
                    intent_valid=intent_valid if intent_valid is not None else -1)

    def dispatch(self, route: str, task_id: str, agent: str) -> None:
        self.append("dispatch", route=route, task_id=task_id, agent=agent)

    def verify(self, passed: bool, accepted: list[str], failed: list[str],
               evidence: list[str]) -> None:
        self.append("verify", passed=bool(passed),
                    accepted_n=len(accepted), failed_n=len(failed),
                    accepted=accepted, failed=failed, evidence=evidence)

    def approval(self, state: str, task_id: str | None = None, op_type: str = "") -> None:
        self.append("approval", state=state, task_id=task_id or "", op_type=op_type)

    def circuit(self, reason: str, blocked: bool, round_no: int = 0) -> None:
        self.append("circuit", reason=reason, blocked=bool(blocked), round=round_no)

    def security(self, violation: str, action: str, detail: str = "") -> None:
        self.append("security", violation=violation, action=action, detail=detail)

    def reverse(self, loaded: bool, target: str, copied_to: str = "") -> None:
        self.append("reverse", loaded=bool(loaded), target=target, copied_to=copied_to)

    def quota(self, max_percent: float, stale: bool, source: str,
              decision: str = "") -> None:
        self.append("quota", max_percent=float(max_percent), stale=bool(stale),
                    source=source, decision=decision)

    def recover(self, action: str, detail: str = "") -> None:
        self.append("recover", action=action, detail=detail)

    def error(self, code: int, detail: str) -> None:
        self.append("error", code=int(code), detail=detail)

    # ------------------------------------------------------------------

    def read_all(self) -> list[dict[str, Any]]:
        """读全部事件（不修改文件）。"""
        text = read_text_if_exists(self.path)
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def last(self, kind: str | None = None) -> dict[str, Any] | None:
        """最近一条（可选按 kind 过滤）。"""
        events = self.read_all()
        if kind is not None:
            events = [e for e in events if e.get("kind") == kind]
        return events[-1] if events else None

    def count(self) -> int:
        return len(self.read_all())

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return self.read_all()[-n:]

    def is_append_only(self) -> bool:
        """健康检查：文件应以 newline 结尾且能被完整解析（无截断行）。"""
        text = read_text_if_exists(self.path)
        if not text:
            return True
        lines = text.splitlines()
        if not text.endswith("\n"):
            return False
        for ln in lines:
            try:
                json.loads(ln)
            except json.JSONDecodeError:
                return False
        return True


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """append-only：多次追加只增不改；只读不破坏；is_append_only 通过。"""
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-activity-selftest-"))
    try:
        log = ActivityLog(tmp)
        n0 = log.count()
        log.phase("IDLE", "PLANNING", round_no=1, task_id="task-001")
        log.llm("deepseek", 1, 10, 200, intent_valid=1)
        log.verify(True, ["file_exists:x"], [], ["ok"])
        n3 = log.count()
        checks.append(("追加 3 条后 count=3", n3 - n0 == 3))

        snapshot = read_text_if_exists(log.path)
        # 再追加不应改写历史
        log.approval("AWAITING_APPROVAL", task_id="task-001", op_type="git_push")
        after = read_text_if_exists(log.path)
        checks.append(("追加只增不改（历史行保留）", snapshot in after and after.startswith(snapshot)))
        checks.append(("last(approval) 命中", (log.last("approval") or {}).get("state") == "AWAITING_APPROVAL"))
        checks.append(("tail 取最近", (log.tail(1)[0].get("kind") == "approval")))

        # 缺失文件不应崩溃
        empty = ActivityLog(tmp / "missing_dir")
        checks.append(("空日志 read_all=[]", empty.read_all() == []))
        checks.append(("is_append_only 通过", log.is_append_only() is True))

        # 破坏文件（截断最后一行）-> is_append_only 应 False
        broken = after.rstrip("\n")  # 去掉末尾换行模拟截断
        atomic_write_text(log.path, broken)
        checks.append(("截断后 is_append_only=False", log.is_append_only() is False))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] activity selftest：{failed} 项未过")
        return 1
    print("[OK] activity selftest 全部通过（append-only 不破坏历史）")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="activity：append-only 活动日志自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
