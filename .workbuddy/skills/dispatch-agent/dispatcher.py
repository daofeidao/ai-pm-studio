"""dispatcher —— dispatch-agent skill 入口：三段式路由 + worktree 生命周期（v2 §7 T04）。

三段式路由（v2 §5.1 / P0-3）：
  ① ScriptRouter.match 命中 script-index.json -> worktree 内执行脚本（零 LLM、零额度）
  ② 未命中且 OpenCode 额度 max_percent > degrade_percent(90) -> WorkBuddyProvider（同进程）
  ③ 否则 -> OpenCodeProvider（worktree 路径作 <directory>，--auto 隔离）

重做循环：reviewer 返回 False 时携带反馈重派，≤ max_rework（v2 D9）。
--selftest 覆盖三分支：命中路由零 LLM；mock 额度 95% -> WB；stale -> 放行走 OC。

用法：
    python .workbuddy/skills/dispatch-agent/dispatcher.py --selftest
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# 本文件位于 <root>/.workbuddy/skills/dispatch-agent/dispatcher.py，向上四级为仓库根
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

import pm_common
from pm_common import (
    E_AGENT_CRASH,
    E_AGENT_TIMEOUT,
    E_DIFF_REJECTED,
    E_OK,
    E_SCRIPT_NOT_FOUND,
    PMError,
    atomic_write_text,
    load_config,
    run_cli,
)
from git_ops import GitOps
from quota import QuotaService
import agent_provider as ap
from agent_provider import (
    AgentProvider,
    AgentResult,
    MockOpenCodeProvider,
    MockWorkBuddyProvider,
    OpenCodeProvider,
    TaskSpec,
    WorkBuddyProvider,
    worktree_rel_for,
)

PhaseHook = Callable[[str], None]


# ---------------------------------------------------------------------------
# 脚本路由
# ---------------------------------------------------------------------------

@dataclass
class Route:
    """script-index 命中路由。"""

    id: str = ""
    path: str = ""
    runner: str = "python"
    name: str = ""


class ScriptRouter:
    """script-index.json 关键词匹配（命中零 LLM）。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any]) -> None:
        self.root = Path(root)
        self.cfg = cfg
        self.index_path = self.root / cfg.get("paths", {}).get("script_index", "script-index.json")
        self.routes: list[Route] = []
        self.exclude: list[str] = []
        self.skipped_missing: list[str] = []
        self._load()

    def _load(self) -> None:
        import json

        if not self.index_path.exists():
            return
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.exclude = list(data.get("exclude", []))
        for r in data.get("routes", []):
            route = Route(
                id=r.get("id", ""),
                path=r.get("path", ""),
                runner=r.get("runner", "python"),
                name=r.get("name", ""),
            )
            if not Path(route.path).exists():
                # 路径不存在的路由跳过（不阻塞）
                self.skipped_missing.append(route.id)
                continue
            self.routes.append(route)

    def _is_excluded(self, route: Route) -> bool:
        """exclude 清单安全网：阿虎历史版 / 截图-CDP 旧版不得进入路由。"""
        basename = Path(route.path).name
        candidates = [route.id, route.name, basename]
        for pat in self.exclude:
            for cand in candidates:
                if fnmatch.fnmatch(cand, pat):
                    return True
                # 模式含 ~ 范围（如 截图-CDP-v014~v027）时按前缀做包含判断
                prefix = pat.split("~", 1)[0].rstrip("*")
                if prefix and cand.startswith(prefix):
                    return True
        return False

    def match(self, instruction: str) -> Route | None:
        """关键词命中返回 Route；未命中返回 None。"""
        text = (instruction or "").lower()
        for route in self.routes:
            if self._is_excluded(route):
                continue
            # 关键词从索引原文取（match 字段），这里重新读一次以避免 dataclass 丢失
            for kw in self._keywords_of(route):
                if kw and kw.lower() in text:
                    return route
        return None

    def _keywords_of(self, route: Route) -> list[str]:
        import json

        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        for r in data.get("routes", []):
            if r.get("id") == route.id:
                return list(r.get("match", []))
        return []


# ---------------------------------------------------------------------------
# 派发器
# ---------------------------------------------------------------------------

class Dispatcher:
    """三段式路由 + worktree 生命周期 + 重做循环。"""

    def __init__(
        self,
        root: Path | str,
        cfg: dict[str, Any],
        git: GitOps,
        demo: bool = False,
        demo_irreversible: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.git = git
        self.demo = demo or bool(cfg.get("demo", {}).get("enabled", False))
        self.demo_irreversible = demo_irreversible
        self.router = ScriptRouter(self.root, cfg)
        self.quota = QuotaService(cfg, demo=self.demo)
        self.max_rework = int(cfg.get("loop", {}).get("max_rework", 2))
        self.degrade_percent = float(cfg.get("quota", {}).get("degrade_percent", 90))
        self.last_route: str | None = None      # script / workbuddy / opencode
        self.last_diff: list[str] = []
        # v2.1 逆向路由状态（供测试与看板断言）
        self.reverse_loaded: bool = False        # 是否注入 leanrea 路由表
        self.reverse_copied_to: list[str] = []    # 复制到 worktree 副本的路径
        self.reverse_targets: list[str] = []      # 检测到的逆向目标
        self._reverse_prepared: bool = False      # 防重复复制/注入

    # ------------------------------------------------------------------

    def _provider(self, kind: str) -> AgentProvider:
        """按路由构造 provider（demo 换 Mock 实现）。"""
        if kind == "workbuddy":
            return (
                MockWorkBuddyProvider(self.root, self.cfg, self.git)
                if self.demo
                else WorkBuddyProvider(self.root, self.cfg, self.git)
            )
        if kind == "opencode":
            if self.demo:
                return MockOpenCodeProvider(
                    self.root, self.cfg, self.git, irreversible=self.demo_irreversible
                )
            return OpenCodeProvider(self.root, self.cfg, self.git)
        raise PMError(E_SCRIPT_NOT_FOUND, f"未知 provider: {kind}")

    def _handoff_path(self) -> Path:
        return self.root / self.cfg.get("paths", {}).get("task", "TASK.md")

    def _collect_diff(self, task: TaskSpec, result: AgentResult) -> list[str]:
        """按路由收集改动清单：script/opencode 走 task 分支 diff；workbuddy 走 status/artifacts。"""
        if self.last_route in ("script", "opencode"):
            return self.git.diff_names(self.git.default_branch, task.id)
        if self.demo:
            return list(result.artifacts)
        return self.git.status_paths()

    # ------------------------------------------------------------------
    # v2.1 逆向路由（LeanREA）
    # ------------------------------------------------------------------

    _REVERSE_EXTS = (".asar", ".apk", ".dll", ".elf", ".exe", ".bin", ".img",
                     ".dex", ".so", ".dylib", ".app", ".ipa", "firmware")
    _REVERSE_KEYWORDS = ("逆向", "反编译", "解包", "reverse", "decompile",
                         "electron", "固件", "binary analysis", "mach-o", "逆向工程")

    def _extract_path_tokens(self, instruction: str) -> list[str]:
        """从指令里按空白/引号切出疑似文件路径的 token（鲁棒，兼容 8.3 短路径）。"""
        out: list[str] = []
        for tok in re.split(r'[\s"\']+', instruction or ""):
            tok = tok.strip().strip(".,;:")
            if not tok:
                continue
            low = tok.lower()
            if any(low.endswith(e) for e in self._REVERSE_EXTS if e.startswith(".")):
                out.append(tok)
            elif re.match(r"^[A-Za-z]:[\\/]", tok) or tok.startswith("/"):
                out.append(tok)
        return out

    def _detect_reverse(self, instruction: str, reverse_target: str | None = None) -> tuple[bool, str]:
        """判定是否逆向任务；返回 (is_reverse, target_path)。"""
        text = (instruction or "").lower()
        paths = self._extract_path_tokens(instruction)
        path_hit = paths[0] if paths else None
        is_rev = (
            any(e in text for e in self._REVERSE_EXTS)
            or any(k in text for k in self._REVERSE_KEYWORDS)
            or bool(path_hit)
            or bool(reverse_target)
        )
        target = reverse_target or path_hit or ""
        return is_rev, target

    def _maybe_reverse(self, task: TaskSpec, instruction: str) -> str:
        """逆转路由处理：检测 -> 复制副本 -> 注入 leanrea 指令。

        仅执行一次（self._reverse_prepared 守卫）。返回（可能带逆向注入前缀的）instruction。
        """
        is_rev, target = self._detect_reverse(instruction, task.reverse_target)
        if not is_rev:
            return instruction
        self.reverse_loaded = True
        if target:
            self.reverse_targets = [target]
        if self._reverse_prepared:
            return instruction

        wt_rel = worktree_rel_for(task)
        wt = self.root / wt_rel
        if not wt.exists():
            wt = self.git.worktree_add(wt_rel, task.id, self.git.default_branch)
        copy_dir = wt / "_reverse" / "copy"
        copy_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        candidates = [target] if target else []
        for tok in self._extract_path_tokens(instruction):
            if tok not in candidates:
                candidates.append(tok)
        for cand in candidates:
            p = Path(cand)
            if p.exists() and p.is_file():
                dest = copy_dir / p.name
                shutil.copy2(p, dest)
                copied.append(str(dest.relative_to(self.root)))
        self.reverse_copied_to = copied
        self._reverse_prepared = True
        inj = (
            "请先读取 .opencode/skills/leanrea/SKILL.md，按其「路由表」为本次逆向目标选择首条命令；"
            "务必先把目标文件复制到 worktree 副本（worktrees/%s/_reverse/copy/）再对副本操作，"
            "绝不触碰原始路径。" % task.id
        )
        return f"{instruction}\n【逆向侦察】{inj}"

    def _run_script(self, route: Route, wt: Path, task: TaskSpec) -> AgentResult:
        """script 路由执行：cwd=worktree，环境变量 PM_OUTPUT_DIR=<wt>/work（Q6 约定）。"""
        work_dir = wt / self.cfg.get("paths", {}).get("work_dir", "work")
        work_dir.mkdir(parents=True, exist_ok=True)
        env = {"PM_OUTPUT_DIR": str(work_dir)}

        if self.demo:
            # demo mock：Git 真（worktree 内 commit）、执行假（写占位产物）
            out = work_dir / f"script-{route.id}-output.md"
            atomic_write_text(
                out,
                f"# 脚本路由产物（demo mock）\n\n- 路由：{route.id}（{route.name}）\n"
                f"- 指令：{task.instruction}\n- 说明：--demo 下不真实执行脚本，仅验证 worktree 闭环\n",
            )
            self.git._git(["add", "-A"], cwd=wt)
            self.git._git(
                ["commit", "-m", f"demo(script): {route.id} 产物"], cwd=wt, env=self.git._identity_env()
            )
            return AgentResult(
                status="success",
                agent="script",
                summary=f"脚本路由 {route.id}（demo mock）产物已提交",
                artifacts=[str(out.relative_to(self.root))],
                irreversible_ops=[],
                session_id=None,
                duration_ms=0,
            )

        # 真实执行
        runner = route.runner.lower()
        if runner == "python":
            argv = [self.cfg.get("python", sys.executable), route.path]
        elif runner == "node":
            argv = [self.cfg.get("node", "node"), route.path]
        else:
            return AgentResult(
                status="failed",
                agent="script",
                error_code=E_SCRIPT_NOT_FOUND,
                error_detail=f"未知 runner: {route.runner}",
            )
        try:
            proc = run_cli(argv, cwd=wt, timeout_s=600, env=env)
        except pm_common.CliTimeout:
            return AgentResult(
                status="failed", agent="script", error_code=E_AGENT_TIMEOUT,
                error_detail=f"脚本超时(600s)：{route.path}",
            )
        if proc.returncode != 0:
            return AgentResult(
                status="failed", agent="script", error_code=E_AGENT_CRASH,
                error_detail=(proc.stderr or "")[-500:],
            )
        # 脚本产物提交到 task 分支（保证 diff 可见、可 merge）
        self.git._git(["add", "-A"], cwd=wt)
        self.git._git(
            ["commit", "-m", f"script: {route.id} 产物"], cwd=wt, env=self.git._identity_env()
        )
        return AgentResult(
            status="success",
            agent="script",
            summary=f"脚本路由 {route.id} 执行完成",
            artifacts=[],
            irreversible_ops=ap.detect_irreversible_ops(proc.stdout or ""),
            session_id=None,
            duration_ms=0,
        )

    def _dispatch_once(
        self, task: TaskSpec, feedback: str, phase_hook: PhaseHook | None
    ) -> AgentResult:
        """单次派发（三段式）。"""
        instruction = task.instruction if not feedback else f"{task.instruction}\n【打回反馈】{feedback}"
        # v2.1 逆向路由：检测 + 复制到 worktree 副本 + 注入 leanrea 指令（仅一次）
        instruction = self._maybe_reverse(task, instruction)

        # ① 脚本优先（零 LLM）
        route = self.router.match(instruction)
        if route is not None:
            self.last_route = "script"
            if phase_hook:
                phase_hook("PREPARING_WORKTREE")
            wt = self.git.worktree_add(worktree_rel_for(task), task.id, self.git.default_branch)
            if phase_hook:
                phase_hook("EXECUTING")
            return self._run_script(route, wt, task)

        # ② 额度超限降级 WB
        quota = self.quota.fetch()
        if not quota.stale and quota.max_percent > self.degrade_percent:
            self.last_route = "workbuddy"
            if phase_hook:
                phase_hook("EXECUTING")
            provider = self._provider("workbuddy")
            return provider.run(task, self._handoff_path())

        # ③ OpenCode（worktree 隔离）
        self.last_route = "opencode"
        wt_rel = worktree_rel_for(task)
        wt = self.root / wt_rel
        if not wt.exists():
            if phase_hook:
                phase_hook("PREPARING_WORKTREE")
            wt = self.git.worktree_add(wt_rel, task.id, self.git.default_branch)
        else:
            # 逆向已预建 worktree（含 _reverse/copy 副本），不重建以免丢失副本
            if phase_hook:
                phase_hook("PREPARING_WORKTREE")
        if phase_hook:
            phase_hook("EXECUTING")
        provider = self._provider("opencode")
        return provider.run(task, self._handoff_path())

    # ------------------------------------------------------------------

    def dispatch(
        self,
        task: TaskSpec,
        reviewer: Callable[[TaskSpec], bool] | None = None,
        max_rework: int | None = None,
        phase_hook: PhaseHook | None = None,
    ) -> AgentResult:
        """派活主入口；reviewer 验收不过则携带反馈重做（≤ max_rework）。"""
        limit = self.max_rework if max_rework is None else int(max_rework)
        attempt = 0
        feedback = ""
        while True:
            attempt += 1
            result = self._dispatch_once(task, feedback, phase_hook)
            if result.status not in ("success", "executed_in_process"):
                return result
            if result.irreversible_ops:
                # 不可逆操作上交编排器人工审批，不在派发层执行
                return result
            self.last_diff = self._collect_diff(task, result)
            if not result.artifacts:
                result.artifacts = list(self.last_diff)
            if phase_hook:
                phase_hook("REVIEWING_DIFF")
            if reviewer is None or reviewer(task):
                return result
            if attempt > limit:
                result.status = "failed"
                result.error_code = E_DIFF_REJECTED
                result.error_detail = (
                    f"验收连续 {attempt - 1} 次未过（max_rework={limit}），放弃该 task"
                )
                return result
            feedback = "；".join(self.last_diff) or "改动不符合 allowed_paths/验收标准"
            if phase_hook:
                phase_hook("EXECUTING")  # 打回重做：重新进入执行相


# ---------------------------------------------------------------------------
# 自检（v2 §7 T04 验收标准）
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """三分支覆盖：命中路由零 LLM；mock 额度 95% -> WB；stale -> 放行 OC。

    全部走真实临时 git 仓库（Git 真、执行假），断言主工作区始终 main。
    """
    import shutil
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-dispatcher-selftest-"))
    try:
        cfg = load_config(ROOT / "pm.config.json")
        # 自检仓库复用真实 script-index.json（路由表来源）
        shutil.copyfile(ROOT / "script-index.json", tmp / "script-index.json")
        git = GitOps(tmp, cfg)
        git.init_repo(remote=None, demo=True)

        # --- 分支①：命中 script-index，零 LLM ---
        disp = Dispatcher(tmp, cfg, git, demo=True)
        spec = TaskSpec(id="task-101", instruction="更新进度看板：刷新本周进度", acceptance=["产物 ∈ work/"], allowed_paths=["work/"])
        route = disp.router.match(spec.instruction)
        checks.append(("命中 script-index 路由 update-board", route is not None and route.id == "update-board"))
        checks.append(
            ("排除清单安全网：无一条已加载路由被 exclude", all(not disp.router._is_excluded(r) for r in disp.router.routes))
        )
        result = disp.dispatch(spec)
        checks.append(("script 路由派发成功", result.status == "success" and result.agent == "script"))
        diff = disp.last_diff
        checks.append(("script 产物 diff ∈ work/", bool(diff) and all(p.startswith("work/") for p in diff)))
        checks.append(("script 路由零 LLM（未构造 OC/WB provider）", disp.last_route == "script"))
        merge = git.merge_task_branch("task-101", "selftest: merge task-101")
        checks.append(("script 产物可 merge 回 main", merge["ok"] is True))
        git.worktree_remove(worktree_rel_for(spec))
        git.delete_branch("task-101")

        # --- 分支②：mock 额度 95% -> WB ---
        disp2 = Dispatcher(tmp, cfg, git, demo=True)
        disp2.quota = _FakeQuota(max_percent=95, stale=False)
        spec2 = TaskSpec(id="task-102", instruction="演示任务：生成登录页")
        result2 = disp2.dispatch(spec2)
        checks.append(("额度 95% 路由到 WorkBuddy", disp2.last_route == "workbuddy"))
        checks.append(("WB demo 结果成功", result2.status == "success" and result2.agent == "workbuddy"))
        checks.append(
            ("WB 产物落在 main 的 work/", (tmp / "work" / "wb-demo-login.html").exists())
        )
        checks.append(
            ("WB 路径主工作区仍为 main", git.current_branch() == "main")
        )

        # --- 分支③：stale 放行 -> OC ---
        disp3 = Dispatcher(tmp, cfg, git, demo=True)
        disp3.quota = _FakeQuota(max_percent=0, stale=True)
        spec3 = TaskSpec(id="task-103", instruction="演示任务：继续生成组件")
        result3 = disp3.dispatch(spec3)
        checks.append(("stale 放行路由到 OpenCode", disp3.last_route == "opencode"))
        checks.append(("OC demo 全链路成功", result3.status == "success" and result3.agent == "opencode"))
        diff3 = disp3.last_diff
        checks.append(("OC diff_names 可见产物", bool(diff3) and all(p.startswith("work/") for p in diff3)))
        checks.append(("OC session_id 非空", bool(result3.session_id)))
        merge3 = git.merge_task_branch("task-103", "selftest: merge task-103")
        checks.append(("OC 产物可 merge 回 main", merge3["ok"] is True))
        git.worktree_remove(worktree_rel_for(spec3))
        git.delete_branch("task-103")

        # --- parse_opencode_json（T04 验收：DEMO_OPENCODE_JSON -> 非空 text + session_id） ---
        from demo_fixtures import DEMO_OPENCODE_JSON

        parsed = ap.parse_opencode_json(DEMO_OPENCODE_JSON)
        checks.append(("parse_opencode_json text 非空", bool(parsed.summary.strip())))
        checks.append(("parse_opencode_json sessionID 提取", parsed.session_id == "ses_demo_001"))
        raw_path = tmp / "reports" / "opencode-task-103-raw.json"
        checks.append(("raw 落盘 reports/opencode-<id>-raw.json", raw_path.exists()))

        # --- build_command dry-run（flags 集合与 v2 §6.2 一致） ---
        oc_provider = OpenCodeProvider(tmp, cfg, git)
        cmd = oc_provider.build_command(spec3, tmp / "worktrees" / "task-103", tmp / "TASK.md")
        flags = {a for a in cmd if a.startswith("--")}
        expected_flags = {"--format", "--auto", "--title", "--file"}
        checks.append(("build_command flags 超集覆盖 §6.2", expected_flags.issubset(flags)))
        checks.append(("build_command 以 json/auto 开头", cmd[1] == "run" and cmd[2] == "--format" and cmd[3] == "json"))
        checks.append(("build_command 末尾为 message+directory", cmd[-2] == spec3.instruction))
        cmd_cont = oc_provider.build_command(spec3, tmp / "wt", tmp / "TASK.md", session_id="ses_x", continue_last=True)
        checks.append(("build_command 支持 --session/--continue", "--session" in cmd_cont and "--continue" in cmd_cont))

        # --- 分支④：逆向路由（LeanREA）注入 + 复制副本 + 原文件不改 ---
        disp4 = Dispatcher(tmp, cfg, git, demo=True)
        target_apk = tmp / "dist" / "app.apk"
        target_apk.parent.mkdir(parents=True, exist_ok=True)
        target_apk.write_text("PK\x03\x04 fake apk bytes\n", encoding="utf-8")
        orig_mtime = target_apk.stat().st_mtime_ns
        spec4 = TaskSpec(
            id="task-104",
            instruction=f"逆向分析安装包 {target_apk} 并反编译其中的逻辑",
            acceptance=["file_exists:work/reverse-report.md"],
            allowed_paths=["work/"],
        )
        result4 = disp4.dispatch(spec4)
        checks.append(("逆向任务注入 leanrea（reverse_loaded=True）", disp4.reverse_loaded is True))
        checks.append(("逆向任务复制目标到 worktree 副本", len(disp4.reverse_copied_to) >= 1))
        copy_path = tmp / disp4.reverse_copied_to[0] if disp4.reverse_copied_to else None
        checks.append(("副本落在 worktrees/task-104/_reverse/copy", bool(copy_path) and "task-104/_reverse/copy" in str(copy_path)))
        checks.append(("原文件未被改动（mtime 不变）", target_apk.stat().st_mtime_ns == orig_mtime))
        checks.append(("逆向 OC 全链路成功", result4.status == "success"))
        git.worktree_remove(worktree_rel_for(spec4))
        git.delete_branch("task-104")

        # --- 分支⑤：普通任务不加载 leanrea ---
        disp5 = Dispatcher(tmp, cfg, git, demo=True)
        spec5 = TaskSpec(id="task-105", instruction="演示任务：生成登录页", acceptance=["file_exists:work/demo-login.html"], allowed_paths=["work/"])
        disp5.dispatch(spec5)
        checks.append(("普通任务不加载 leanrea（reverse_loaded=False）", disp5.reverse_loaded is False))

        checks.append(("全程主工作区保持 main", git.current_branch() == "main"))
        checks.append(("全程无 task worktree 残留", not (tmp / "worktrees" / "task-101").exists() and not (tmp / "worktrees" / "task-103").exists() and not (tmp / "worktrees" / "task-104").exists()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] dispatcher selftest：{failed} 项未过")
        return 1
    print("[OK] dispatcher selftest 全部通过（三分支 + worktree 闭环）")
    return E_OK


class _FakeQuota:
    """自检用额度假件（替换 QuotaService.fetch）。"""

    def __init__(self, max_percent: float, stale: bool) -> None:
        from quota import QuotaInfo

        self._info = QuotaInfo(
            agent="opencode",
            max_percent=max_percent,
            windows={"month": max_percent},
            source="selftest-mock",
            fetched_at=pm_common.utc_now_iso(),
            stale=stale,
        )

    def fetch(self, force: bool = False):
        return self._info


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="dispatch-agent：三段式路由 + worktree 闭环")
    parser.add_argument("--selftest", action="store_true", help="运行自检（临时仓库，Git 真）")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
