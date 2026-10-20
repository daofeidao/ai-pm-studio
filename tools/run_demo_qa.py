"""run_demo_qa.py —— 离线 demo 四场景 QA（happy / 熔断 / 降级 / 选择器失效）。

设计原则：
  - **每个场景一个独立沙箱**（临时目录里复制一份项目源码），绝不污染真实仓库
    （真实仓库只在末尾写一份 reports/qa-demo-<ts>.md 作为证据）。
  - Git 全真：init --demo 会建本地 bare remote，push/merge 都是真操作。
  - 断言直接读 `git show pm/state:state/current.json`（状态以 Git 为准），
    外加产物文件、worktree 残留、stdout 关键字、activity.jsonl 事件。

用法：
    python tools/run_demo_qa.py                 # 跑全部四个场景
    python tools/run_demo_qa.py --scenario happy
    python tools/run_demo_qa.py --keep          # 保留沙箱目录（排障用）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORCH = ".workbuddy/skills/pm-orchestrator/orchestrator.py"

#: 复制沙箱时排除的运行时目录（.git 必须重建；reports/memory 由 init 重新脚手架）
IGNORE = shutil.ignore_patterns(
    ".git", "worktrees", ".demo-remote", "__pycache__", "*.tmp",
    "activity.jsonl", "reports", "memory",
)

SCENARIOS = ("happy", "circuit", "degrade", "selector")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def force_rmtree(path: Path) -> None:
    """删除沙箱：Windows 上 `.git/objects` 是只读，普通 rmtree 会静默留垃圾。"""
    def _on_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_error)
    else:  # pragma: no cover —— 兼容 3.11 及更早
        shutil.rmtree(path, onerror=_on_error)

def load_cfg() -> dict:
    return json.loads((ROOT / "pm.config.json").read_text(encoding="utf-8"))


def python_exe(cfg: dict) -> str:
    return str(cfg.get("python") or sys.executable)


def run_cmd(argv: list[str], cwd: Path, timeout_s: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_s,
    )


def git(sandbox: Path, *args: str) -> subprocess.CompletedProcess:
    return run_cmd(["git", *args], sandbox, timeout_s=120)


def read_state(sandbox: Path) -> dict:
    proc = git(sandbox, "show", "pm/state:state/current.json")
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def read_activity(sandbox: Path) -> list[dict]:
    path = sandbox / "activity.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def make_sandbox(tag: str) -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix=f"pm-qa-{tag}-"))
    shutil.copytree(ROOT, sandbox, ignore=IGNORE, dirs_exist_ok=True)
    return sandbox


class Checklist:
    """场景内断言收集器。"""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((name, bool(ok), detail))

    @property
    def failed(self) -> int:
        return sum(1 for _n, ok, _d in self.items if not ok)

    def render(self) -> str:
        lines = []
        for name, ok, detail in self.items:
            suffix = f" —— {detail}" if detail and not ok else ""
            lines.append(f"  [{'OK' if ok else 'FAIL'}] {name}{suffix}")
        return "\n".join(lines)


def bootstrap(cfg: dict, sandbox: Path) -> subprocess.CompletedProcess:
    return run_cmd([python_exe(cfg), ORCH, "init", "--demo"], sandbox)


def run_demo(cfg: dict, sandbox: Path, task: str, flags: list[str]) -> subprocess.CompletedProcess:
    return run_cmd(
        [python_exe(cfg), ORCH, "run", "--demo", *flags, task], sandbox, timeout_s=600
    )


def tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# 四个场景
# ---------------------------------------------------------------------------

def scenario_happy(cfg: dict, sandbox: Path) -> Checklist:
    """场景 1：happy path —— 问 → 派 → worktree → diff → merge → push → 交接 → DONE。"""
    c = Checklist("happy")
    init = bootstrap(cfg, sandbox)
    c.add("init --demo 成功", init.returncode == 0, tail(init.stderr))

    proc = run_demo(cfg, sandbox, "演示：生成登录页", [])
    st = read_state(sandbox)
    c.add("run 退出码 0", proc.returncode == 0, tail(proc.stderr))
    c.add("phase=DONE", st.get("phase") == "DONE", str(st.get("phase")))
    c.add("status=DONE", st.get("status") == "DONE", str(st.get("status")))
    c.add("lock 已释放", st.get("lock") is None)
    c.add("push 成功（last_push_ok=true）", st.get("last_push_ok") is True)
    c.add("产物 work/demo-login.html 已合并回 main", (sandbox / "work/demo-login.html").exists())
    c.add("产物 work/demo-login.css 已合并回 main", (sandbox / "work/demo-login.css").exists())
    c.add("main 含 merge commit", "merge: task-001" in git(sandbox, "log", "--oneline", "main").stdout)
    c.add("主工作区仍是 main", git(sandbox, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main")
    c.add("无 task-001 worktree 残留", not (sandbox / "worktrees/task-001").exists())
    c.add("生成轮次报告", bool(list((sandbox / "reports").glob("round-1-*.md"))))
    session = (sandbox / "SESSION.md").read_text(encoding="utf-8")
    c.add("SESSION.md 追加 ≥2 条 ↪ Return", session.count("## ↪ Return") >= 2, str(session.count("## ↪ Return")))
    state_js = sandbox / "dashboard/state.js"
    c.add("dashboard/state.js 已派生", state_js.exists() and "board" in state_js.read_text(encoding="utf-8"))
    bare_log = git(
        sandbox, "-C", str(sandbox / ".demo-remote/pm-software.git"), "log", "--oneline", "main"
    ).stdout
    c.add("push 真的到了 bare remote", "merge: task-001" in bare_log)
    c.add("activity.jsonl 记录了 phase 事件",
          any(e.get("kind") == "phase" for e in read_activity(sandbox)))
    return c


def scenario_circuit(cfg: dict, sandbox: Path) -> Checklist:
    """场景 2：熔断 —— 同一 task 连续失败 3 次 → PAUSED + circuit_open（不再烧额度）。"""
    c = Checklist("circuit")
    init = bootstrap(cfg, sandbox)
    c.add("init --demo 成功", init.returncode == 0, tail(init.stderr))

    proc = run_demo(cfg, sandbox, "演示：连续失败", ["--demo-agent-fail"])
    st = read_state(sandbox)
    events = [e for e in read_activity(sandbox) if e.get("kind") == "circuit"]
    c.add("run 退出码 0（受控暂停，非崩溃）", proc.returncode == 0, tail(proc.stderr))
    c.add("phase=PAUSED", st.get("phase") == "PAUSED", str(st.get("phase")))
    c.add("status=PAUSED", st.get("status") == "PAUSED", str(st.get("status")))
    c.add("circuit_open=true", st.get("circuit_open") is True, str(st.get("circuit_open")))
    c.add("consecutive_failures=3", st.get("consecutive_failures") == 3, str(st.get("consecutive_failures")))
    c.add("stdout 记录 3 次连续失败", proc.stdout.count("连续失败") >= 3, str(proc.stdout.count("连续失败")))
    c.add("stdout 出现「熔断」", "熔断" in proc.stdout)
    c.add("activity.jsonl 有 circuit 事件", len(events) >= 3, str(len(events)))
    c.add("最后一条 circuit 事件 blocked=true", bool(events) and events[-1].get("blocked") is True)
    c.add("失败重试不留 worktree 残留", not (sandbox / "worktrees/task-001").exists())
    c.add("熔断后仍 push 状态（可跨机接续）", st.get("last_push_ok") is True)
    return c


def scenario_degrade(cfg: dict, sandbox: Path) -> Checklist:
    """场景 3：降级 —— OpenCode 额度 95% > 90% → 走 WorkBuddy 同进程（不派 OC）。"""
    c = Checklist("degrade")
    init = bootstrap(cfg, sandbox)
    c.add("init --demo 成功", init.returncode == 0, tail(init.stderr))

    proc = run_demo(cfg, sandbox, "演示：额度超限降级", ["--demo-quota-percent", "95"])
    st = read_state(sandbox)
    c.add("run 退出码 0", proc.returncode == 0, tail(proc.stderr))
    c.add("stdout 记录「额度 95% … 降级 WorkBuddy」",
          "降级 WorkBuddy" in proc.stdout and "95" in proc.stdout, tail(proc.stdout, 6))
    c.add("phase=DONE", st.get("phase") == "DONE", str(st.get("phase")))
    c.add("WB 产物 work/wb-demo-login.html 存在", (sandbox / "work/wb-demo-login.html").exists())
    c.add("未走 OpenCode（无 demo-login.html）", not (sandbox / "work/demo-login.html").exists())
    c.add("降级路径不建 worktree", not (sandbox / "worktrees/task-001").exists())
    c.add("主工作区仍是 main", git(sandbox, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main")
    c.add("push 成功（last_push_ok=true）", st.get("last_push_ok") is True)
    return c


def scenario_selector(cfg: dict, sandbox: Path) -> Checklist:
    """场景 4：选择器失效 —— 主路径选择器缺失且无兜底 → E_WEB_AI_SELECTOR + 人审。"""
    c = Checklist("selector")
    init = bootstrap(cfg, sandbox)
    c.add("init --demo 成功", init.returncode == 0, tail(init.stderr))

    proc = run_demo(cfg, sandbox, "演示：网页 AI 选择器失效", ["--demo-selector-missing"])
    st = read_state(sandbox)
    apr = st.get("pending_approval") or {}
    c.add("run 退出码 0（转人审，不崩溃）", proc.returncode == 0, tail(proc.stderr))
    c.add("stdout 报 E_WEB_AI_SELECTOR", "E_WEB_AI_SELECTOR" in proc.stdout)
    c.add("phase=HUMAN_REVIEW", st.get("phase") == "HUMAN_REVIEW", str(st.get("phase")))
    c.add("status=WAITING_APPROVAL", st.get("status") == "WAITING_APPROVAL", str(st.get("status")))
    c.add("pending_approval.op_type=web_ai_selector", apr.get("op_type") == "web_ai_selector", str(apr))
    c.add("审批条目给出解除方式", "selectors" in str(apr.get("command", "")))
    c.add("未创建 worktree", not (sandbox / "worktrees/task-001").exists())
    session = (sandbox / "SESSION.md").read_text(encoding="utf-8")
    c.add("SESSION.md 记录 web_ai_selector 失败轮", "web_ai_selector" in session)
    return c


RUNNERS = {
    "happy": scenario_happy,
    "circuit": scenario_circuit,
    "degrade": scenario_degrade,
    "selector": scenario_selector,
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线 demo 四场景 QA（独立沙箱，不污染仓库）")
    parser.add_argument("--scenario", choices=SCENARIOS, action="append", default=None,
                        help="只跑指定场景（可多次；默认全部）")
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录（排障）")
    parser.add_argument("--no-report", action="store_true", help="不写 reports/qa-demo-*.md")
    args = parser.parse_args(argv)

    cfg = load_cfg()
    selected = args.scenario or list(SCENARIOS)
    results: list[tuple[str, Checklist, Path, str]] = []
    total_failed = 0

    for name in selected:
        sandbox = make_sandbox(name)
        print(f"\n=== 场景 {name}（沙箱 {sandbox}） ===")
        try:
            checklist = RUNNERS[name](cfg, sandbox)
        except Exception as e:  # noqa: BLE001 —— 场景异常也要落成 FAIL 记录
            checklist = Checklist(name)
            checklist.add(f"场景执行异常：{e!r}", False)
        print(checklist.render())
        total_failed += checklist.failed
        results.append((name, checklist, sandbox, ""))
        if not args.keep:
            force_rmtree(sandbox)

    print("\n=== 汇总 ===")
    for name, checklist, sandbox, _ in results:
        n = len(checklist.items)
        print(f"  {name:9s} {'PASS' if checklist.failed == 0 else 'FAIL'}  "
              f"({n - checklist.failed}/{n} 通过)")

    if not args.no_report:
        ts = __import__("datetime").datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        report = ROOT / "reports" / f"qa-demo-{ts}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# demo QA 报告（场景清单）",
            "",
            f"- 生成时间：{ts}",
            f"- 场景：{', '.join(selected)}",
            f"- 结论：{'全部通过' if total_failed == 0 else f'{total_failed} 项未过'}",
            "",
        ]
        for name, checklist, _sandbox, _ in results:
            lines += [f"## {name}", "", "```text", checklist.render(), "```", ""]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"\n[QA] 报告已写入 {report.relative_to(ROOT)}")

    if total_failed:
        print(f"[FAIL] demo QA：{total_failed} 项未过")
        return 1
    print(f"[OK] demo QA：{len(selected)} 个场景全部通过（{', '.join(selected)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
