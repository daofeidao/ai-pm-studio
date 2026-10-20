"""memory —— pm-memory skill 入口：规则压缩 + 记忆包注入（v2 §7 T05 / P1-3）。

- MemoryService.should_inject(usage)：usage_chars ≥ threshold_chars 才注入
- compress(transcript)：规则压缩（按「决策/约束/偏好/进度」四桶关键词归行，超 max_pack_items 截断）
- update_anchors(pack)：原子写 memory/anchors.json（固化锚点）
- render_pack(pack)：输出 v2 §10.7 固定格式（禁止增删行，逐行比对）
- archive_transcript(round_no, text)：写 memory/transcripts/round-<n>-<ts>.md

压缩用规则而非 AI 自总结（PRD 硬约束 5 / 内存铁约束）。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 本文件位于 <root>/.workbuddy/skills/pm-memory/memory.py，向上四级为仓库根
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

from pm_common import E_OK, atomic_write_json, atomic_write_text, compact_ts, load_config, utc_now_iso


# ---------------------------------------------------------------------------
# 数据结构（v2 §4.2 类图）
# ---------------------------------------------------------------------------

@dataclass
class MemoryPack:
    """压缩后的记忆包（固定注入格式的载体）。"""

    goal: str = ""
    done: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    current_question: str = ""
    updated_at: str = ""


@dataclass
class Anchors:
    """固化锚点（memory/anchors.json）。"""

    decisions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    progress: str = ""


@dataclass
class ContextUsage:
    """上下文用量（按字符数）。"""

    chars_used: int = 0
    chars_limit: int = 0


# ---------------------------------------------------------------------------
# 规则压缩关键词表
# ---------------------------------------------------------------------------

BUCKET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "decisions": ("决策", "决定", "采用", "选定", "方案定为"),
    "constraints": ("约束", "必须", "禁止", "不得", "限制", "只能", "务必"),
    "preferences": ("偏好", "习惯", "风格", "倾向于", "喜好"),
    "progress": ("进度", "完成", "待办", "进行到", "已实现", "已生成", "收尾"),
}

BLOCKED_KEYWORDS: tuple[str, ...] = ("阻塞", "卡住", "受阻", "失败", "未解决", "问题：", "坑")

GOAL_KEYWORDS: tuple[str, ...] = ("目标", "任务", "goal")


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------

class MemoryService:
    """阈值判定 + 规则压缩 + anchors 固化 + 固定格式渲染 + transcript 归档。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any] | None = None) -> None:
        self.root = Path(root).resolve()
        self.cfg = cfg or load_config(self.root / "pm.config.json")
        mem = self.cfg.get("memory", {})
        self.threshold_chars: int = int(mem.get("threshold_chars", 24000))
        self.limit_chars: int = int(mem.get("limit_chars", 120000))
        self.max_pack_items: int = int(mem.get("max_pack_items", 5))
        paths = self.cfg.get("paths", {})
        self.anchors_path = self.root / paths.get("anchors", "memory/anchors.json")
        self.transcripts_dir = self.root / paths.get("transcripts_dir", "memory/transcripts")
        # compress 的四桶结果（供 update_anchors 使用；render_pack 只依赖 MemoryPack 本身）
        self._last_buckets: dict[str, list[str]] = {k: [] for k in BUCKET_KEYWORDS}

    # ------------------------------------------------------------------

    def should_inject(self, usage: ContextUsage) -> bool:
        """usage_chars ≥ threshold_chars 才注入记忆包。"""
        return int(usage.chars_used) >= self.threshold_chars

    # ------------------------------------------------------------------

    @staticmethod
    def _clean_lines(transcript: str) -> list[str]:
        out: list[str] = []
        for raw in transcript.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            if s.startswith(">"):
                continue  # 引用/注释行（如 SESSION.md 头部说明）不进桶
            if s.startswith("【"):
                continue  # 结构标记行（【分析】【结论】等）不进桶
            if s.startswith("- "):
                s = s[2:]
            elif s.startswith("* "):
                s = s[2:]
            s = re.sub(r"^\d+[.、)]\s*", "", s)  # 去序号
            s = s.strip()
            if s:
                out.append(s)
        return out

    def compress(self, transcript: str) -> MemoryPack:
        """规则压缩：按四桶关键词归行；超 max_pack_items 截断。

        current_question 由调用方在注入前赋值（compress 签名不含问题全文）。
        """
        lines = self._clean_lines(transcript)
        buckets: dict[str, list[str]] = {k: [] for k in BUCKET_KEYWORDS}
        blocked: list[str] = []
        goal = ""
        for line in lines:
            low = line.lower()
            if not goal and any(k in line for k in GOAL_KEYWORDS):
                # “目标：xxx” / “任务：xxx” 取冒号后
                m = re.split(r"[：:]", line, maxsplit=1)
                goal = (m[1].strip() if len(m) > 1 else line).strip()
            # 剥离桶标签前缀（“决策：xxx” -> “xxx”），保证记忆包干净
            body = re.sub(r"^(决策|约束|偏好|进度|阻塞|目标|任务)[：:]\s*", "", line).strip() or line
            hit = False
            for bucket, keywords in BUCKET_KEYWORDS.items():
                if any(k in line or k.lower() in low for k in keywords):
                    buckets[bucket].append(body)
                    hit = True
                    break
            if any(k in line for k in BLOCKED_KEYWORDS):
                blocked.append(body)
                hit = True
            if not hit and not goal and len(buckets["decisions"]) == 0:
                # 无处归类的首行暂不处理（保持桶纯净）
                continue
        # 截断
        for k in buckets:
            buckets[k] = buckets[k][: self.max_pack_items]
        blocked = blocked[: self.max_pack_items]
        self._last_buckets = buckets
        if not goal:
            goal = lines[0][:80] if lines else ""
        return MemoryPack(
            goal=goal,
            done=list(buckets["progress"]),
            blocked=blocked,
            current_question="",
            updated_at=utc_now_iso(),
        )

    # ------------------------------------------------------------------

    def load_anchors(self) -> Anchors:
        """读取 memory/anchors.json；缺失/损坏返回空 Anchors（不 raise）。"""
        if not self.anchors_path.exists():
            return Anchors()
        try:
            data = json.loads(self.anchors_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return Anchors()
        return Anchors(
            decisions=list(data.get("decisions", [])),
            constraints=list(data.get("constraints", [])),
            preferences=list(data.get("preferences", [])),
            progress=str(data.get("progress", "")),
        )

    def update_anchors(self, pack: MemoryPack) -> None:
        """把压缩结果固化进 anchors.json（原子写；去重；上限 20 条/桶）。"""
        anchors = self.load_anchors()
        buckets = self._last_buckets or {k: [] for k in BUCKET_KEYWORDS}
        for bucket, attr in (
            ("decisions", "decisions"),
            ("constraints", "constraints"),
            ("preferences", "preferences"),
        ):
            existing = getattr(anchors, attr)
            for item in buckets.get(bucket, []):
                if item not in existing:
                    existing.append(item)
            setattr(anchors, attr, existing[-20:])
        if pack.goal:
            anchors.progress = f"{utc_now_iso()} {pack.goal}"
        elif pack.done:
            anchors.progress = f"{utc_now_iso()} {'；'.join(pack.done[:3])}"
        atomic_write_json(
            self.anchors_path,
            {
                "decisions": anchors.decisions,
                "constraints": anchors.constraints,
                "preferences": anchors.preferences,
                "progress": anchors.progress,
            },
        )

    # ------------------------------------------------------------------

    def render_pack(self, pack: MemoryPack) -> str:
        """固定格式（v2 §10.7，禁止增删行，恰好 6 行）。"""
        done = "；".join(pack.done[:3]) if pack.done else "无"
        blocked = "；".join(pack.blocked) if pack.blocked else "无"
        lines = [
            "【项目记忆·自动生成】",
            f"目标：{pack.goal}",
            f"已完成：{done}",
            f"阻塞：{blocked}",
            "【当前问题】",
            pack.current_question or "（无）",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------

    def archive_transcript(self, round_no: int, text: str) -> Path:
        """归档本轮问答到 memory/transcripts/round-<n>-<ts>.md，返回路径。"""
        ts = compact_ts()
        p = self.transcripts_dir / f"round-{int(round_no)}-{ts}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, text)
        return p


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """阈值边界 / 超长 transcript 压缩 / anchors 更新 / 固定格式逐行比对 / 归档。"""
    import shutil
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-memory-selftest-"))
    try:
        cfg = load_config(ROOT / "pm.config.json")
        # 调小阈值复现超限（T05 验收：threshold_chars 调小触发压缩）
        cfg["memory"]["threshold_chars"] = 100
        svc = MemoryService(tmp, cfg)

        # 1) 阈值边界
        checks.append(("边界：未超阈值不注入", svc.should_inject(ContextUsage(99, 24000)) is False))
        checks.append(("边界：等于阈值即注入", svc.should_inject(ContextUsage(100, 24000)) is True))

        # 2) 超长 transcript 压缩
        transcript = (
            "目标：实现登录页并与后端联调\n"
            "- 决策：用 MUI + Tailwind 做登录页样式\n"
            "- 决策：鉴权走 session cookie 方案\n"
            "- 约束：内存可用仅 2.68GB，零常驻进程\n"
            "- 约束：不可逆操作必须人工审批\n"
            "- 偏好：看板用单文件 HTML\n"
            "- 进度：第 1 轮登录页静态结构已完成\n"
            "- 进度：第 2 轮表单校验已实现\n"
            "- 阻塞：DeepSeek 会话需保持 Edge 登录态\n"
            "无关紧要的闲聊一行\n"
        )
        pack = svc.compress(transcript)
        checks.append(("压缩 goal 提取", pack.goal == "实现登录页并与后端联调"))
        checks.append(("压缩 decisions 两桶归行", "用 MUI + Tailwind 做登录页样式" in svc._last_buckets["decisions"]))
        checks.append(("压缩 constraints 归行", any("2.68GB" in c for c in svc._last_buckets["constraints"])))
        checks.append(("压缩 blocked 归行", any("Edge 登录态" in b for b in pack.blocked)))
        checks.append(("压缩 done 归行", len(pack.done) >= 2))

        # 3) update_anchors 写 anchors.json
        pack.current_question = "第 3 轮问：鉴权方案选型？"
        svc.update_anchors(pack)
        anchors = svc.load_anchors()
        checks.append(("anchors decisions 固化", len(anchors.decisions) >= 2))
        checks.append(("anchors progress 非空", bool(anchors.progress)))
        # 幂等：再次 update 不重复堆积
        svc.update_anchors(pack)
        anchors2 = svc.load_anchors()
        checks.append(("anchors 去重（不重复堆积）", len(anchors2.decisions) == len(anchors.decisions)))

        # 4) render_pack 逐行比对（v2 §10.7 固定格式）
        expected = [
            "【项目记忆·自动生成】",
            "目标：实现登录页并与后端联调",
            "已完成：第 1 轮登录页静态结构已完成；第 2 轮表单校验已实现",
            "阻塞：DeepSeek 会话需保持 Edge 登录态",
            "【当前问题】",
            "第 3 轮问：鉴权方案选型？",
        ]
        actual = svc.render_pack(pack).splitlines()
        checks.append(("render_pack 恰好 6 行", len(actual) == 6))
        checks.append(("render_pack 逐行一致", actual == expected))

        # 空列表场景
        empty_pack = MemoryPack(goal="g", done=[], blocked=[], current_question="q")
        empty_lines = svc.render_pack(empty_pack).splitlines()
        checks.append(
            ("空 done/blocked 写『无』", empty_lines[2] == "已完成：无" and empty_lines[3] == "阻塞：无")
        )

        # 5) 归档
        p = svc.archive_transcript(3, "# Q\n问题\n\n# A\n回答\n")
        checks.append(
            (
                "归档 memory/transcripts/round-3-<ts>.md",
                p.exists() and p.name.startswith("round-3-") and p.name.endswith(".md"),
            )
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] memory selftest：{failed} 项未过")
        return 1
    print("[OK] memory selftest 全部通过（固定格式逐行比对通过）")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="pm-memory：规则压缩 + 记忆包注入自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
