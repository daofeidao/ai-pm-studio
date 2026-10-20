"""run_e2e_loop.py —— E2E 闭环演练（v2.2）。

串联四段：
  ① 网页 AI（DeepSeek）把用户目标拆成 JSON 指令            —— 真实 CDP 提问
  ② dispatch-agent 解析指令 + 真实 worktree / 真实 git 分支 —— 真实派发链路
  ③ 执行端                                      —— mock（只替换「写什么代码」）
  ④ 结果 + 记忆包回传网页 AI，再问一次                       —— 真实 CDP 提问

设计取舍：
  - 编码环节用 mock（HelloMockProvider）替代真实 OpenCode，避免消耗额度；
    但 worktree 创建、任务分支 commit、diff 收集全部是真的。
  - 记忆压缩用 MemoryService 真实实现；为在短会话里触发注入，
    沙箱配置把 memory.threshold_chars 临时降到 200（默认 24000），报告里注明。

产出：reports/e2e-loop-001.md（完整流转日志 + 验收点表）。

用法：
    python tools/run_e2e_loop.py [--report reports/e2e-loop-001.md]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / ".workbuddy" / "skills" / "dispatch-agent"))
sys.path.insert(0, str(ROOT / ".workbuddy" / "skills" / "pm-memory"))

import pm_common  # noqa: E402
import webai_adapter as wa  # noqa: E402
import agent_provider as ap  # noqa: E402
from pm_common import atomic_write_text, load_config, run_cli, utc_now_iso  # noqa: E402
from git_ops import GitOps  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from memory import MemoryService, ContextUsage as MemUsage  # noqa: E402
from agent_provider import AgentResult, TaskSpec, worktree_rel_for  # noqa: E402

TASK_ID = "task-e2e-001"
GOAL = "写一个 hello.py，运行后打印 hello"
ROUND1_PROMPT = f"""你是项目经理。请把下面的任务拆成一条严格 JSON 指令。
只输出 JSON，不要解释，不要 markdown 代码围栏。字段：
{{"id":"{TASK_ID}","instruction":"给执行端的完整指令","acceptance":["可验证的验收点"],"allowed_paths":["work/"]}}
任务：{GOAL}"""


class HelloMockProvider(ap.MockOpenCodeProvider):
    """只替换「写什么代码」：worktree 与 git commit 仍然是真的。"""

    kind = "opencode-mock-e2e"

    def run(self, task: TaskSpec, handoff_path: Path) -> AgentResult:
        started = time.time()
        wt = self._ensure_worktree(task)
        target = wt / "work" / "hello.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, "print('hello')\n")
        changed = self.git.status_paths(worktree_rel_for(task))
        if not changed:
            return AgentResult(
                status="failed",
                agent="opencode",
                error_code=pm_common.E_AGENT_RESULT_INVALID,
                error_detail="e2e mock 写后自检失败：worktree git status 为空",
            )
        self.git._git(["add", "-A"], cwd=wt)
        self.git._git(
            ["commit", "-m", f"e2e: {task.id} hello.py"],
            cwd=wt,
            env=self.git._identity_env(),
        )
        return AgentResult(
            status="success",
            agent="opencode",
            summary=f"已生成 work/hello.py 并提交到 {task.id} 分支",
            artifacts=["work/hello.py"],
            session_id="ses_e2e_hello",
            duration_ms=int((time.time() - started) * 1000),
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def make_sandbox() -> Path:
    """临时 git 仓库：main 基线 + 空脚手架，供真实 worktree 使用。"""
    d = Path(tempfile.mkdtemp(prefix="pm-e2e-loop-"))
    run_cli(["git", "init"], cwd=d)
    run_cli(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=d)
    for rel in ("work/.gitkeep", "reports/.gitkeep", "memory/transcripts/.gitkeep"):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    run_cli(["git", "add", "-A"], cwd=d)
    run_cli(
        ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@local",
         "commit", "-m", "e2e: baseline"],
        cwd=d,
    )
    return d


def parse_json_reply(text: str) -> dict:
    """从网页 AI 回复里抠出 JSON（容忍代码围栏 / 前后解释）。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    i = s.find("{")
    if i < 0:
        raise ValueError("回复里没有 JSON 对象")
    obj, _ = json.JSONDecoder().raw_decode(s[i:])
    if not isinstance(obj, dict):
        raise ValueError("顶层不是对象")
    return obj


def fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.rstrip()}\n```"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E2E 闭环演练：网页 AI -> 派发 -> 回传")
    parser.add_argument("--report", default=str(ROOT / "reports" / "e2e-loop-001.md"))
    parser.add_argument("--max-reply-chars", type=int, default=4000)
    args = parser.parse_args(argv)

    started_at = utc_now_iso()
    t0 = time.time()
    cfg = load_config(ROOT / "pm.config.json")
    checks: list[tuple[str, str, bool]] = []
    notes: list[str] = []

    # ---------------- ① 前置检查 + 第一跳：网页 AI 规划 ----------------
    adapter = wa.build_adapter(ROOT, cfg, demo=False, provider="deepseek")
    t1 = time.time()
    reply1 = adapter.send_prompt(ROUND1_PROMPT)
    dur1 = time.time() - t1
    preflight = list(getattr(adapter, "preflight_log", []))
    checks.append(("a", "ask-web-ai 发出提问并取回非空回答", bool(reply1.text.strip())))

    try:
        instruction_obj = parse_json_reply(reply1.text)
        parsed_ok = bool(instruction_obj.get("instruction"))
    except (ValueError, json.JSONDecodeError) as e:
        instruction_obj = {"_parse_error": str(e)}
        parsed_ok = False
    checks.append(("a", "回答可解析为 JSON 指令（含 instruction）", parsed_ok))

    instruction = str(instruction_obj.get("instruction") or GOAL)
    acceptance = instruction_obj.get("acceptance") or ["python work/hello.py 输出 hello"]
    allowed = instruction_obj.get("allowed_paths") or ["work/"]
    if not isinstance(acceptance, list):
        acceptance = [str(acceptance)]
    if not isinstance(allowed, list):
        allowed = [str(allowed)]

    # ---------------- ② 第二跳：dispatch-agent（真实 worktree） ----------------
    sandbox = make_sandbox()
    sandbox_cfg = json.loads(json.dumps(cfg))
    sandbox_cfg.setdefault("demo", {})["enabled"] = True
    sandbox_cfg.setdefault("memory", {})["threshold_chars"] = 100
    sandbox_cfg.setdefault("paths", {})["task"] = "TASK.md"
    notes.append(
        f"沙箱仓库：`{sandbox}`（临时目录，真实 git；报告生成后可删）"
    )
    notes.append(
        "编码环节为 mock（HelloMockProvider）：只替换“写什么代码”，"
        "worktree 创建、任务分支 commit、diff 全为真实 git 操作"
    )
    notes.append(
        "记忆压缩阈值在沙箱内临时降到 100 字符（默认 24000），以便在短会话里触发注入"
    )

    git = GitOps(sandbox, sandbox_cfg)
    disp = Dispatcher(sandbox, sandbox_cfg, git, demo=True)
    disp._provider = lambda kind: HelloMockProvider(sandbox, sandbox_cfg, git)  # type: ignore[assignment]
    task = TaskSpec(
        id=TASK_ID,
        instruction=instruction,
        acceptance=acceptance,
        allowed_paths=allowed,
    )
    phases: list[str] = []
    t2 = time.time()
    result = disp.dispatch(task, phase_hook=phases.append)
    dur2 = time.time() - t2

    wt = sandbox / worktree_rel_for(task)
    branch_commits = git.branch_commit_count(TASK_ID, git.default_branch) if wt.exists() else 0
    artifacts = list(result.artifacts or [])
    hello_txt = wt / "work" / "hello.py"
    hello_out = ""
    if hello_txt.exists():
        proc = run_cli([sys.executable, "work/hello.py"], cwd=wt, timeout_s=30)
        hello_out = (proc.stdout or "").strip()

    checks.append(("b", "dispatch 返回 success", result.status == "success"))
    checks.append(("b", "worktree 目录真实生成", wt.is_dir()))
    checks.append(("b", "任务分支有提交（diff 非空）", branch_commits >= 1))
    checks.append(("b", "产物 work/hello.py 落盘且可运行", hello_out == "hello"))

    # ---------------- ③ 结果 + 记忆压缩 ----------------
    mem = MemoryService(sandbox, sandbox_cfg)
    transcript = "\n".join(
        [
            f"目标：{GOAL}",
            "决策：采用 DeepSeek 规划 + dispatch-agent worktree 隔离",
            f"进度：worktree 已生成，分支 {TASK_ID}",
            f"进度：执行端产出 {'、'.join(artifacts) or 'work/hello.py'}，运行输出 {hello_out or '（未运行）'}",
            f"约束：只允许改 {'、'.join(allowed)}",
            "阻塞：无",
        ]
    )
    archive1 = mem.archive_transcript(1, f"# Q\n{ROUND1_PROMPT}\n\n# A\n{reply1.text}\n")
    usage = MemUsage(chars_used=len(transcript), chars_limit=mem.threshold_chars)
    inject = mem.should_inject(usage)
    pack = mem.compress(transcript)
    checks.append(("c", "上下文达阈值触发记忆压缩", inject))

    round2_question = (
        f"执行结果已回传：{result.summary}；产物 {'、'.join(artifacts) or '无'}；"
        f"运行 `python work/hello.py` 输出：{hello_out or '（未运行）'}。"
        "请判断任务是否完成，并用不超过三行给出：1) 结论 2) 证据 3) 下一步建议。"
    )
    pack.current_question = round2_question
    pack_text = mem.render_pack(pack)
    checks.append(("c", "记忆包固定 6 行且含目标/已完成", len(pack_text.splitlines()) == 6
                   and "目标：" in pack_text and "已完成：" in pack_text))

    t3 = time.time()
    reply2 = adapter.send_prompt(round2_question, memory_pack=pack_text)
    dur3 = time.time() - t3
    checks.append(("c", "回传后再次提问并取回非空回答", bool(reply2.text.strip())))
    archive2 = mem.archive_transcript(2, f"# Q\n{pack_text}\n\n{round2_question}\n\n# A\n{reply2.text}\n")

    # ---------------- ④ 报告 ----------------
    failed = [c for c in checks if not c[2]]
    verdict = "PASS" if not failed else "FAIL"
    cap = args.max_reply_chars

    def clip(s: str) -> str:
        s = s or ""
        return s if len(s) <= cap else s[:cap] + f"\n…（截断，原长 {len(s)} 字符）"

    lines: list[str] = [
        "# E2E 闭环演练 001",
        "",
        f"- 开始：{started_at}",
        f"- 结束：{utc_now_iso()}（总耗时 {time.time() - t0:.1f}s）",
        f"- 结论：**{verdict}**（{len(checks) - len(failed)}/{len(checks)} 通过）",
        f"- 目标：{GOAL}",
        f"- 任务 id：`{TASK_ID}`",
        "",
        "## 流转总览",
        "",
        "| 跳 | 动作 | 实现 | 耗时 |",
        "|----|------|------|------|",
        f"| ① | 网页 AI 拆指令 | 真实 DeepSeek（CDP） | {dur1:.1f}s |",
        f"| ② | 派发 + worktree | 真实 dispatch-agent / git（执行 mock） | {dur2:.1f}s |",
        f"| ③ | 结果 + 记忆包回传再问 | 真实 DeepSeek（CDP） | {dur3:.1f}s |",
        "",
        "## 1. 前置检查",
        "",
        fence("\n".join(preflight) or "（无）", "text"),
        "",
        "## 2. 第一跳：网页 AI 规划",
        "",
        "### 提问",
        "",
        fence(ROUND1_PROMPT, "text"),
        "",
        "### 回答（原文）",
        "",
        fence(clip(reply1.text), "text"),
        "",
        "### 解析出的指令",
        "",
        fence(json.dumps(instruction_obj, ensure_ascii=False, indent=2), "json"),
        "",
        "## 3. 第二跳：dispatch-agent 派发",
        "",
        f"- 阶段回调：`{' -> '.join(phases) or '（未触发）'}`",
        f"- 返回状态：`{result.status}` / agent=`{result.agent}`",
        f"- worktree：`{wt}`",
        f"- 任务分支：`{TASK_ID}`（相对 main 新增 {branch_commits} 个提交）",
        f"- 产物：{', '.join(f'`{a}`' for a in artifacts) or '（无）' }",
        f"- 运行验证：`python work/hello.py` -> `{hello_out or '（未运行）'}`",
        "",
        "## 4. 记忆压缩与回传",
        "",
        f"- 触发判定：usage={usage.chars_used}/{usage.chars_limit} -> should_inject=`{inject}`",
        f"- transcript 归档：`memory/transcripts/{archive1.name}`、`memory/transcripts/{archive2.name}`",
        "",
        "### 记忆包（注入内容，固定 6 行）",
        "",
        fence(pack_text, "text"),
        "",
        "### 第二问（记忆包 + 结果）",
        "",
        fence(round2_question, "text"),
        "",
        "### 回答（原文）",
        "",
        fence(clip(reply2.text), "text"),
        "",
        "## 5. 验收点",
        "",
        "| 点 | 断言 | 结果 |",
        "|----|------|------|",
    ]
    for tag, name, ok in checks:
        lines.append(f"| {tag} | {name} | {'PASS' if ok else '**FAIL**'} |")
    lines += [
        "",
        "## 6. 备注与边界",
        "",
    ]
    lines += [f"- {n}" for n in notes]
    lines += [
        f"- 报告生成时间：{utc_now_iso()}",
        "",
    ]
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(report_path, "\n".join(lines))

    print(f"[e2e] 结论 {verdict}（{len(checks) - len(failed)}/{len(checks)}）")
    print(f"[e2e] 报告：{report_path}")
    print(f"[e2e] 沙箱：{sandbox}")
    for tag, name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] ({tag}) {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
