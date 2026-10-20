"""handoff_io —— 三文件交接协议 IO（v2 §4.5，签名级实现）。

三个文件都在 main，随 commit 更新（v2 R4/D12）：
  - HANDOFF.md：中断方/编排器生成的交接摘要，固定五块结构，覆盖写
  - SESSION.md：append-only 执行日志，只追加 `## ↪ Return` 块（只追加不修改 → 无合并冲突）
  - TASK.md  ：编排器据 HANDOFF + SESSION 最后 ↪ Return + current.json 生成的 ODAF 任务书

模板字符串作为模块常量（HANDOFF_TEMPLATE / RETURN_TEMPLATE / TASK_TEMPLATE），禁止增删块。
parse_handoff 失败 raise PMError(E_HANDOFF_IO)。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pm_common import (
    E_HANDOFF_IO,
    PMError,
    atomic_write_text,
    read_text_if_exists,
    utc_now_iso,
)

# ---------------------------------------------------------------------------
# 模板（固定结构，禁止增删块）
# ---------------------------------------------------------------------------

HANDOFF_TEMPLATE = """# HANDOFF · 交接摘要

> 生成：{generated_at} · 生成方：{generated_by} · 机器：{hostname}

## 1. 当前目标
{goal}

## 2. 已定约束
{constraints}

## 3. 关键文件
{key_files}

## 4. 待办清单
{todos}

## 5. 已知阻塞/坑
{blockers}
"""

RETURN_TEMPLATE = """## ↪ Return · {ts} · {agent}
- 做了什么：{did}
- 改动文件：{changed_files}
- 测试结果：{test_result}
- 报告路径：{report_path}
- 建议下一步：{next_step}
- 备注：{note}
"""

TASK_TEMPLATE = """# TASK · {task_id}

> 由编排器据 HANDOFF.md + SESSION.md 最后 ↪ Return 生成于 {generated_at}

## O — Outcome（预期结果）
{outcome}

## D — Data（输入）
{data}

## A — Action（动作清单）
{actions}

## F — Format（交付格式）
{format}
"""

#: 空列表渲染占位；parse 时该行不计入列表（保证空列表渲染->解析往返一致）
EMPTY_ITEM = "（无）"


# ---------------------------------------------------------------------------
# 数据结构（v2 §4.5）
# ---------------------------------------------------------------------------

@dataclass
class HandoffContext:
    """render_handoff 的输入。"""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    key_files: list[tuple[str, str]] = field(default_factory=list)  # (path, note)
    todos: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    generated_by: str = "pm-orchestrator"
    hostname: str = ""


@dataclass
class HandoffDoc:
    """parse_handoff 的输出。"""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    generated_at: str = ""


@dataclass
class ReturnBlock:
    """SESSION.md 的一条 ↪ Return。"""

    ts: str = ""
    agent: str = ""
    did: str = ""
    changed_files: list[str] = field(default_factory=list)
    test_result: str = ""
    report_path: str = ""
    next_step: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _bullets(items: list[str]) -> str:
    """列表渲染为 markdown  bullet 行；空列表渲染占位行。"""
    if not items:
        return f"- {EMPTY_ITEM}"
    return "\n".join(f"- {it}" for it in items)


def _checkbox(items: list[str]) -> str:
    """待办清单渲染为 `- [ ]` 行；空列表渲染占位行。"""
    if not items:
        return f"- [ ] {EMPTY_ITEM}"
    return "\n".join(f"- [ ] {it}" for it in items)


def _split_list_field(value: str) -> list[str]:
    """`改动文件` 字段解析：分号分隔；占位符视为空列表。"""
    value = value.strip()
    if not value or value == EMPTY_ITEM:
        return []
    return [p.strip() for p in re.split(r"[；;]", value) if p.strip()]


# ---------------------------------------------------------------------------
# HandoffIO
# ---------------------------------------------------------------------------

class HandoffIO:
    """HANDOFF/SESSION/TASK 三文件渲染、解析、追加。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.handoff_path = self.root / "HANDOFF.md"
        self.session_path = self.root / "SESSION.md"
        self.task_path = self.root / "TASK.md"

    # ---------------- HANDOFF.md ----------------

    def render_handoff(self, ctx: HandoffContext) -> str:
        """固定五块模板渲染 HANDOFF.md 内容。"""
        key_files = [f"{path}：{note}" if note else path for path, note in ctx.key_files]
        return HANDOFF_TEMPLATE.format(
            generated_at=utc_now_iso(),
            generated_by=ctx.generated_by,
            hostname=ctx.hostname,
            goal=ctx.goal,
            constraints=_bullets(ctx.constraints),
            key_files=_bullets(key_files),
            todos=_checkbox(ctx.todos),
            blockers=_bullets(ctx.blockers),
        )

    def write_handoff(self, content: str) -> None:
        """覆盖写 HANDOFF.md（原子写）。"""
        atomic_write_text(self.handoff_path, content)

    def parse_handoff(self, text: str) -> HandoffDoc:
        """按 `## 1.` ~ `## 5.` 标题切块解析；缺块或格式错误 raise E_HANDOFF_IO。"""
        doc = HandoffDoc()
        # 生成时间行：> 生成：<iso> · ...
        m = re.search(r"^>\s*生成：(\S+)", text, flags=re.MULTILINE)
        if m:
            doc.generated_at = m.group(1)

        sections: dict[str, list[str]] = {}
        current_key: str | None = None
        for line in text.splitlines():
            hm = re.match(r"^##\s*(\d+)\.\s*(.+?)\s*$", line)
            if hm:
                current_key = hm.group(1)
                sections.setdefault(current_key, [])
                continue
            if current_key is not None:
                sections[current_key].append(line)

        for idx in ("1", "2", "3", "4", "5"):
            if idx not in sections:
                raise PMError(E_HANDOFF_IO, f"HANDOFF.md 缺少第 {idx} 块（## {idx}.）")

        def _para(lines: list[str]) -> str:
            return "\n".join(l for l in lines if l.strip()).strip()

        def _items(lines: list[str]) -> list[str]:
            out: list[str] = []
            for l in lines:
                s = l.strip()
                if not s.startswith("-"):
                    continue
                body = s.lstrip("-").strip()
                body = re.sub(r"^\[[ xX]\]\s*", "", body)  # 去复选框
                if body == EMPTY_ITEM:
                    continue
                out.append(body)
            return out

        doc.goal = _para(sections["1"])
        doc.constraints = _items(sections["2"])
        # 关键文件：取 "路径：备注" 中的路径部分
        key_files: list[str] = []
        for l in sections["3"]:
            s = l.strip()
            if not s.startswith("-"):
                continue
            body = s.lstrip("-").strip()
            if body == EMPTY_ITEM:
                continue
            key_files.append(re.split(r"[：:]", body, maxsplit=1)[0].strip())
        doc.key_files = key_files
        doc.todos = _items(sections["4"])
        doc.blockers = _items(sections["5"])
        return doc

    # ---------------- SESSION.md（append-only） ----------------

    def render_return(self, block: ReturnBlock) -> str:
        """固定模板渲染一条 ↪ Return 块。"""
        return RETURN_TEMPLATE.format(
            ts=block.ts,
            agent=block.agent,
            did=block.did,
            changed_files="；".join(block.changed_files) if block.changed_files else EMPTY_ITEM,
            test_result=block.test_result,
            report_path=block.report_path,
            next_step=block.next_step,
            note=block.note,
        )

    def append_return(self, block: ReturnBlock) -> None:
        """只追加 ↪ Return 块到 SESSION.md 末尾，绝不修改历史行（v2 §10.4）。"""
        existing = read_text_if_exists(self.session_path)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        if existing and not existing.endswith("\n\n"):
            existing += "\n"  # 与上一块之间留空行（markdown 规范）
        with open(self.session_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(self.render_return(block))
            if not self.render_return(block).endswith("\n"):
                f.write("\n")

    def _split_return_blocks(self, text: str) -> list[str]:
        """按 `## ↪ Return` 标题切出所有块文本（含标题行）。"""
        parts = re.split(r"(?m)^(?=##\s*↪\s*Return)", text)
        return [p for p in parts if re.match(r"(?m)^##\s*↪\s*Return", p)]

    def _parse_return_block(self, chunk: str) -> ReturnBlock:
        hm = re.search(r"^##\s*↪\s*Return\s*·\s*(\S+)\s*·\s*(\S+)\s*$", chunk, flags=re.MULTILINE)
        block = ReturnBlock(ts=hm.group(1) if hm else "", agent=hm.group(2) if hm else "")
        fields = {
            "做了什么": "did",
            "改动文件": "changed_files",
            "测试结果": "test_result",
            "报告路径": "report_path",
            "建议下一步": "next_step",
            "备注": "note",
        }
        for line in chunk.splitlines():
            lm = re.match(r"^-\s*([^：:]+)[：:]\s*(.*)$", line.strip())
            if not lm:
                continue
            key, value = lm.group(1).strip(), lm.group(2).strip()
            attr = fields.get(key)
            if attr is None:
                continue
            if attr == "changed_files":
                block.changed_files = _split_list_field(value)
            else:
                setattr(block, attr, value)
        return block

    def read_last_return(self) -> ReturnBlock | None:
        """最后一个 `## ↪ Return` 块；无则 None。"""
        chunks = self._split_return_blocks(read_text_if_exists(self.session_path))
        if not chunks:
            return None
        return self._parse_return_block(chunks[-1])

    def read_session_tail(self, n: int = 3) -> list[ReturnBlock]:
        """最后 n 条 ↪ Return（按时间正序）。"""
        chunks = self._split_return_blocks(read_text_if_exists(self.session_path))
        return [self._parse_return_block(c) for c in chunks[-n:]]

    # ---------------- TASK.md（ODAF 四段式） ----------------

    def build_task_odaf(
        self, handoff: HandoffDoc, last_return: ReturnBlock | None, current: dict
    ) -> str:
        """ODAF 四段式 TASK.md：Outcome / Data / Action / Format。"""
        rnd = current.get("round", 0)
        phase = current.get("phase", "IDLE")
        task_id = current.get("task_id") or f"task-{int(rnd or 0):03d}"
        allowed = current.get("allowed_paths") or ["work/"]
        acceptance = current.get("acceptance") or [
            "构建退出码 0；git diff --name-only 全部 ∈ work/"
        ]

        if last_return is not None:
            exec_line = f"- 执行史：SESSION.md 最后 ↪ Return（{last_return.ts} · {last_return.agent}）"
        else:
            exec_line = "- 执行史：SESSION.md（暂无 ↪ Return 记录）"

        actions = handoff.todos or ["按交接摘要推进当前目标"]
        action_lines = "\n".join(f"{i}. {a}" for i, a in enumerate(actions, 1))

        data_lines = "\n".join(
            [
                "- 交接摘要：HANDOFF.md（目标/约束/关键文件/待办/阻塞）",
                exec_line,
                f"- 当前指针：pm/state:state/current.json（round={rnd}, phase={phase}）",
                "- 记忆包：【项目记忆·自动生成】…（未超阈值则无）",
            ]
        )
        format_lines = "\n".join(
            [
                f"- 改动只允许落在 {'/'.join(allowed)} 下（allowed_paths={json.dumps(allowed, ensure_ascii=False)}）",
                "- 完成后在 SESSION.md 由编排器代追加 ↪ Return 块",
                f"- 验收标准：{'；'.join(acceptance)}",
            ]
        )
        return TASK_TEMPLATE.format(
            task_id=task_id,
            generated_at=utc_now_iso(),
            outcome=handoff.goal or "（未填写目标）",
            data=data_lines,
            actions=action_lines,
            format=format_lines,
        )

    def write_task(self, content: str) -> None:
        """覆盖写 TASK.md（原子写）。"""
        atomic_write_text(self.task_path, content)


# ---------------------------------------------------------------------------
# 初始模板（clone 后立即可被解析）
# ---------------------------------------------------------------------------

INITIAL_SESSION = """# SESSION · 执行日志（append-only）

> 本文件只追加 `## ↪ Return` 块，禁止修改历史行（无合并冲突的关键，v2 §10.4）。
> 每条 Return 由编排器在每轮 PERSISTING 时代为追加。
"""


def write_initial_files(root: Path | str) -> None:
    """缺失时才写初始 HANDOFF/SESSION/TASK，保证 clone 后可立即解析。"""
    io = HandoffIO(root)
    if not io.handoff_path.exists():
        ctx = HandoffContext(
            goal="（待编排器首次 run 填写）",
            constraints=["内存可用仅 2.68GB，零常驻进程", "不可逆操作必须人工审批"],
            key_files=[("HANDOFF.md", "本交接摘要"), ("pm/state:state/current.json", "最小状态指针")],
            todos=["执行 orchestrator.py run 启动首轮闭环"],
            blockers=["（暂无）"],
        )
        io.write_handoff(io.render_handoff(ctx))
    if not io.session_path.exists():
        atomic_write_text(io.session_path, INITIAL_SESSION)
    if not io.task_path.exists():
        doc = io.parse_handoff(read_text_if_exists(io.handoff_path))
        io.write_task(
            io.build_task_odaf(doc, None, {"round": 0, "phase": "IDLE", "task_id": None})
        )


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """render→parse 往返；append_return 只增不改；build_task_odaf 四段齐全。"""
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-handoff-selftest-"))
    try:
        io = HandoffIO(tmp)
        ctx = HandoffContext(
            goal="实现登录页并与后端联调",
            constraints=["内存可用仅 2.68GB，零常驻进程", "不可逆操作必须人工审批"],
            key_files=[("work/login.html", "登录页初版，缺表单校验"), ("memory/anchors.json", "已固化 3 条决策")],
            todos=["登录表单校验", "与后端联调"],
            blockers=["DeepSeek 会话需保持 Edge 登录态"],
            hostname="SELFTEST-HOST",
        )
        content = io.render_handoff(ctx)
        io.write_handoff(content)
        doc = io.parse_handoff(content)
        checks.append(("render→parse goal 一致", doc.goal == ctx.goal))
        checks.append(("render→parse constraints 一致", doc.constraints == ctx.constraints))
        checks.append(("render→parse key_files 一致", doc.key_files == ["work/login.html", "memory/anchors.json"]))
        checks.append(("render→parse todos 一致", doc.todos == ctx.todos))
        checks.append(("render→parse blockers 一致", doc.blockers == ctx.blockers))
        checks.append(("generated_at 解析非空", bool(doc.generated_at)))

        # 空列表往返
        ctx2 = HandoffContext(goal="空列表场景", constraints=[], key_files=[], todos=[], blockers=[])
        doc2 = io.parse_handoff(io.render_handoff(ctx2))
        checks.append(
            ("空列表往返一致", doc2.constraints == [] and doc2.todos == [] and doc2.blockers == [])
        )

        # append_return 只增不改
        before = read_text_if_exists(io.session_path) or "# SESSION\n"
        atomic_write_text(io.session_path, before)
        b1 = ReturnBlock(
            ts="2026-10-20T11:40:00Z", agent="opencode", did="实现登录页静态结构与表单校验",
            changed_files=["work/login.html（+120/-3）"], test_result="demo 自检通过；构建 ok",
            report_path="reports/round-1-20261020T114000Z.md",
            next_step="询问 DeepSeek 鉴权方案后联调", note="session=ses_abc123（仅本机 --continue 可用）",
        )
        io.append_return(b1)
        mid = read_text_if_exists(io.session_path)
        lines_mid = mid.splitlines()
        b2 = ReturnBlock(
            ts="2026-10-20T11:50:00Z", agent="workbuddy", did="补充后端联调 mock",
            changed_files=[], test_result="ok", report_path="reports/round-2-x.md",
            next_step="收尾", note="",
        )
        io.append_return(b2)
        after = read_text_if_exists(io.session_path)
        lines_after = after.splitlines()
        checks.append(("append 只增行（历史行不变）", lines_after[: len(lines_mid)] == lines_mid))
        checks.append(("append 两次后共 2 个 Return 块", len(io.read_session_tail(10)) == 2))
        last = io.read_last_return()
        checks.append(
            ("read_last_return 取到最后一块",
            bool(last) and last.agent == "workbuddy" and last.ts == "2026-10-20T11:50:00Z"),
        )
        checks.append(("改动文件分号解析", io.read_session_tail(2)[0].changed_files == ["work/login.html（+120/-3）"]))
        checks.append(("空改动文件解析为 []", io.read_session_tail(2)[1].changed_files == []))

        # build_task_odaf 四段
        task = io.build_task_odaf(doc, last, {"round": 3, "phase": "EXECUTING", "task_id": "task-014"})
        for seg in ("## O — Outcome", "## D — Data", "## A — Action", "## F — Format"):
            checks.append((f"TASK.md 含 {seg}", seg in task))
        checks.append(("TASK.md 标题含 task-014", "# TASK · task-014" in task))
        checks.append(("TASK.md 动作清单来自待办", "1. 登录表单校验" in task and "2. 与后端联调" in task))

        # 解析失败路径
        try:
            io.parse_handoff("# 不是合法 HANDOFF\n只有一行\n")
            checks.append(("非法 HANDOFF raise E_HANDOFF_IO", False))
        except PMError as e:
            checks.append(("非法 HANDOFF raise E_HANDOFF_IO", e.code == E_HANDOFF_IO))

        # 初始模板可解析
        write_initial_files(tmp)
        doc3 = io.parse_handoff(read_text_if_exists(io.handoff_path))
        checks.append(("初始 HANDOFF 可解析", bool(doc3.goal)))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] handoff_io selftest：{failed} 项未过")
        return 1
    print("[OK] handoff_io selftest 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：python lib/handoff_io.py --selftest"""
    import argparse

    parser = argparse.ArgumentParser(description="HandoffIO：三文件交接协议自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
