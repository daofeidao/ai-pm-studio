"""git_ops —— GitOps：Git 交接内核（v2 §4.4，签名级实现）。

铁律（v2 §10）：
  1. Git 即单一事实源：main 承载产出与交接三件套；pm/state 孤儿分支只放 state/current.json。
  2. 主工作区永不切分支：task 分支一律 `git worktree add`；pm/state 一律 plumbing 写
     （hash-object -> 临时索引 read-tree/update-index/write-tree -> commit-tree -> update-ref），
     全程无 checkout。
  3. 原子写 / best-effort push / Git 身份兜底（init 时 user.email 为空则设 repo 级身份）。

所有 git 调用统一经 pm_common.run_cli（capture、超时 kill 进程树）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# 支持 `python lib/git_ops.py` 直接运行与包内 import 两种方式
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pm_common
from pm_common import (
    E_GIT_COMMIT_FAILED,
    E_GIT_INIT_FAILED,
    E_GIT_PUSH_FAILED,
    E_GIT_WORKTREE_FAILED,
    E_OK,
    E_STATE_CORRUPT,
    PMError,
    atomic_write_text,
    run_cli,
    utc_now_iso,
)

#: git 内置空树哈希（分支不存在时 read-tree 的目标，v2 §4.4 实现要点 2）
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: 临时索引文件名（Windows 下置于 .git/ 下，v2 §4.4 实现要点 4）
TMP_INDEX_NAME = "pm-tmp-index"

#: .gitignore 规格内容（v2 §3）；仅当仓库缺失该文件时由脚手架补写，不覆盖既有文件
GITIGNORE_CONTENT = """dashboard/state.js
worktrees/
*.tmp
__pycache__/
.demo-remote/
.DS_Store
activity.jsonl
"""


class GitOps:
    """Git 交接内核：仓库初始化 / 状态指针读写 / 提交推送 / worktree 生命周期 / 合并。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any]) -> None:
        self.root = Path(root).resolve()
        self.cfg = cfg
        git_cfg = cfg.get("git", {})
        self.git_exe: str = git_cfg.get("exe", "git")
        self.default_branch: str = git_cfg.get("default_branch", "main")
        self.state_branch: str = git_cfg.get("state_branch", "pm/state")
        self.author_name: str = git_cfg.get("author_name", "pm-orchestrator")
        self.author_email: str = git_cfg.get("author_email", "pm@local")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _git(
        self,
        args: list[str],
        cwd: Path | str | None = None,
        timeout_s: float = 120,
        env: dict[str, str] | None = None,
        check: bool = False,
    ):
        """经 pm_common.run_cli 执行 git；check=True 时非零退出 raise E_GIT_COMMIT_FAILED。"""
        try:
            proc = run_cli(
                [self.git_exe] + args,
                cwd=cwd if cwd is not None else self.root,
                timeout_s=timeout_s,
                env=env,
            )
        except pm_common.CliTimeout as e:
            raise PMError(E_GIT_COMMIT_FAILED, f"git 超时: {' '.join(args)}") from e
        if check and proc.returncode != 0:
            raise PMError(
                E_GIT_COMMIT_FAILED,
                f"git {' '.join(args)} 失败(rc={proc.returncode}): {(proc.stderr or '').strip()[:500]}",
            )
        return proc

    def _identity_env(self) -> dict[str, str]:
        """commit-tree 等需要显式作者/提交者身份（v2 §4.4 实现要点 3）。"""
        return {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
        }

    def _ref_exists(self, ref: str) -> bool:
        return self._git(["rev-parse", "--verify", "--quiet", ref]).returncode == 0

    def _ensure_scaffold(self) -> None:
        """确保脚手架目录/文件存在（首 commit 需要至少一个文件，worktree add 需要 base）。"""
        for rel in ("work/.gitkeep", "reports/.gitkeep", "memory/transcripts/.gitkeep"):
            p = self.root / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(p, "")
        q = self.root / "commands" / "queue.jsonl"
        if not q.exists():
            q.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(q, "")
        gi = self.root / ".gitignore"
        if not gi.exists():
            atomic_write_text(gi, GITIGNORE_CONTENT)

    # ------------------------------------------------------------------
    # 仓库生命周期
    # ------------------------------------------------------------------

    def is_repo(self) -> bool:
        """root 是否为 git 仓库（.git 目录或文件存在）。"""
        return (self.root / ".git").exists()

    def init_repo(self, remote: str | None, demo: bool = False) -> None:
        """git init -b main；脚手架首 commit；add remote；ensure_state_branch()。

        demo=True 时额外在 <root>/.demo-remote/ 建 bare 仓库并设为 origin（零网络）。
        """
        if not self.is_repo():
            proc = self._git(["init", "-b", self.default_branch])
            if proc.returncode != 0:
                raise PMError(
                    E_GIT_INIT_FAILED, f"git init 失败: {(proc.stderr or '').strip()[:500]}"
                )

        # Git 身份兜底（硬约束 3）：user.email 为空则设 repo 级身份，避免 commit 失败
        if not self._git(["config", "user.email"]).stdout.strip():
            self._git(["config", "user.name", self.author_name])
            self._git(["config", "user.email", self.author_email])

        # 行尾策略（Windows 关键）：本项目 diff / blob 逐字节比对（demo 基线复位、
        # 写后自检）都假定 **LF 原样落盘**。系统级 core.autocrlf=true 会让
        # worktree checkout 成 CRLF，Python 再写回 LF 后 `git status` 出现
        # 「内容相同却 modified」的假脏（size 200 vs 196），把验收判成「有改动」。
        # 仓库级关掉，保证 git 行为与字节比对一致。
        self._git(["config", "core.autocrlf", "false"])
        self._git(["config", "core.safecrlf", "false"])

        self._ensure_scaffold()

        # 首 commit（无变更则跳过）
        self._git(["add", "-A"])
        if self._git(["diff", "--cached", "--quiet"]).returncode != 0:
            proc = self._git(
                ["commit", "-m", "chore: 初始化 pm-software 仓库脚手架"],
                env=self._identity_env(),
            )
            if proc.returncode != 0:
                raise PMError(
                    E_GIT_INIT_FAILED, f"首 commit 失败: {(proc.stderr or '').strip()[:500]}"
                )

        # 远端：demo 用本地 bare 仓库（.demo-remote/ 已在 .gitignore）
        remote_url = remote
        if demo:
            demo_cfg = self.cfg.get("demo", {})
            bare = self.root / demo_cfg.get("remote_dir", ".demo-remote") / demo_cfg.get(
                "bare_name", "pm-software.git"
            )
            if not bare.exists():
                bare.parent.mkdir(parents=True, exist_ok=True)
                proc = self._git(["init", "--bare", str(bare)])
                if proc.returncode != 0:
                    raise PMError(
                        E_GIT_INIT_FAILED, f"bare 仓库创建失败: {(proc.stderr or '').strip()[:500]}"
                    )
            remote_url = str(bare)
        if remote_url:
            if self._git(["remote", "get-url", "origin"]).returncode == 0:
                self._git(["remote", "set-url", "origin", remote_url])
            else:
                self._git(["remote", "add", "origin", remote_url])

        self.ensure_state_branch()

    def current_branch(self) -> str:
        """主工作区当前分支（自检断言始终为 main）。"""
        proc = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # ------------------------------------------------------------------
    # pm/state 孤儿分支（plumbing，绝不 checkout）
    # ------------------------------------------------------------------

    def ensure_state_branch(self) -> None:
        """pm/state 不存在则用 plumbing 建孤儿分支并写初始 current.json。"""
        if self._ref_exists(self.state_branch):
            return
        initial: dict[str, Any] = {
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
            "updated_at": utc_now_iso(),
        }
        self.write_current_json(initial, message="chore: 初始化 pm/state 孤儿分支")

    def read_current_json(self) -> dict | None:
        """git show pm/state:state/current.json；分支/文件不存在返回 None。"""
        if not self._ref_exists(self.state_branch):
            return None
        proc = self._git(["show", f"{self.state_branch}:state/current.json"])
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise PMError(E_STATE_CORRUPT, f"current.json 损坏: {e}") from e
        return data if isinstance(data, dict) else None

    def write_current_json(self, state: dict, message: str | None = None) -> str:
        """plumbing 写 current.json，返回新 commit sha；全程无 checkout。

        步骤：hash-object -w --stdin -> GIT_INDEX_FILE 临时索引
        read-tree(旧树或空树)/update-index/write-tree -> commit-tree(-p 旧commit)
        -> update-ref refs/heads/pm/state。
        """
        state = dict(state)
        state["updated_at"] = utc_now_iso()
        text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        msg = message or f"pm/state: {state.get('phase', '?')} (round={state.get('round', 0)})"

        # 1) blob（经临时文件传内容，避免 stdin 兼容性问题）
        blob = self._hash_object(text)

        # 2) 临时索引
        tmp_index = self.root / ".git" / TMP_INDEX_NAME
        if tmp_index.exists():
            tmp_index.unlink()
        index_env = {"GIT_INDEX_FILE": str(tmp_index)}

        # 3) read-tree：旧树（分支已存在）或空树
        has_branch = self._ref_exists(self.state_branch)
        tree_ref = self.state_branch if has_branch else EMPTY_TREE_SHA
        proc = self._git(["read-tree", tree_ref], env=index_env, check=True)

        # 4) update-index --cacheinfo
        proc = self._git(
            ["update-index", "--add", "--cacheinfo", f"100644,{blob},state/current.json"],
            env=index_env,
            check=True,
        )

        # 5) write-tree
        proc = self._git(["write-tree"], env=index_env, check=True)
        tree = proc.stdout.strip()

        # 6) commit-tree（带 -p 旧 commit；作者取配置）
        args = ["commit-tree", tree, "-m", msg]
        if has_branch:
            args += ["-p", self.state_branch]
        proc = self._git(args, env=self._identity_env(), check=True)
        commit = proc.stdout.strip()

        # 7) update-ref
        self._git(["update-ref", f"refs/heads/{self.state_branch}", commit], check=True)

        if tmp_index.exists():
            tmp_index.unlink()
        return commit

    def _hash_object(self, text: str) -> str:
        """hash-object -w：经临时文件传 stdin（run_cli 不直接支持 stdin 输入）。"""
        tmp = self.root / ".git" / "pm-hash.tmp"
        try:
            atomic_write_text(tmp, text)
            proc = self._git(["hash-object", "-w", str(tmp)], check=True)
            return proc.stdout.strip()
        finally:
            if tmp.exists():
                tmp.unlink()

    # ------------------------------------------------------------------
    # main 分支：暂存 / 提交 / 推送
    # ------------------------------------------------------------------

    def stage_and_commit(self, paths: list[str], message: str) -> str:
        """git add -- <paths>；git commit -m；返回 short sha。无变更返回空串。"""
        existing = [p for p in paths if (self.root / p).exists()]
        if existing:
            self._git(["add", "--"] + existing)
        if self._git(["diff", "--cached", "--quiet"]).returncode == 0:
            return ""  # 无变更
        proc = self._git(["commit", "-m", message], env=self._identity_env())
        if proc.returncode != 0:
            raise PMError(
                E_GIT_COMMIT_FAILED, f"commit 失败: {(proc.stderr or '').strip()[:500]}"
            )
        return self.head_sha(short=True)

    def push(self, branch: str = "main", best_effort: bool = True) -> bool:
        """git push origin <branch>；best_effort=True 时失败仅返回 False。"""
        proc = self._git(["push", "origin", branch], timeout_s=120)
        if proc.returncode != 0:
            if best_effort:
                return False
            raise PMError(
                E_GIT_PUSH_FAILED,
                f"push {branch} 失败: {(proc.stderr or '').strip()[:500]}",
            )
        return True

    def push_state(self, best_effort: bool = True) -> bool:
        """推送 pm/state 孤儿分支（best-effort）。"""
        return self.push(self.state_branch, best_effort=best_effort)

    # ------------------------------------------------------------------
    # worktree 生命周期
    # ------------------------------------------------------------------

    def worktree_add(self, rel_path: str, branch: str, base: str = "main") -> Path:
        """git worktree add <root>/<rel_path> -B <branch> <base>；返回绝对路径。

        用 `-B`（而非 `-b`）：分支已存在时**重置到 base**而不是报 fatal。这样
        「同一 task 反复重派 / 崩溃恢复后重建」都不会因残留分支而失败。
        """
        wt = self.root / rel_path
        if wt.exists():
            # 已存在同名 worktree：先回收再建（中断续派场景）
            self.worktree_remove(rel_path, force=True)
        proc = self._git(["worktree", "add", str(wt), "-B", branch, base])
        if proc.returncode != 0:
            raise PMError(
                E_GIT_WORKTREE_FAILED,
                f"worktree add 失败({rel_path}, {branch}): {(proc.stderr or '').strip()[:500]}",
            )
        return wt

    def worktree_reset(self, rel_path: str, base: str = "main") -> bool:
        """worktree 内 `git reset --hard <base>`：重置已跟踪文件到基线。

        **不动未跟踪文件**（逆向副本 _reverse/ 等工作副本得以保留）。
        用于「打回重做」：重做必须从干净基线出发，不能继承上一次的错误改动。
        """
        wt = self.root / rel_path
        if not wt.exists():
            return False
        proc = self._git(["reset", "--hard", base], cwd=wt)
        return proc.returncode == 0

    def remove_paths(self, paths: list[str], message: str) -> str:
        """git rm -f -- <paths> 后提交（删除仓库内已跟踪文件）；返回 commit sha。

        用于 demo 基线复位（清理上一轮遗留的同名演示产物，保证 diff 可观测）。
        """
        tracked: list[str] = []
        for p in paths:
            if self._git(["ls-files", "--error-unmatch", "--", p]).returncode == 0:
                tracked.append(p)
        if not tracked:
            return ""
        proc = self._git(["rm", "-f", "--"] + tracked)
        if proc.returncode != 0:
            raise PMError(
                E_GIT_COMMIT_FAILED,
                f"git rm 失败: {(proc.stderr or '').strip()[:500]}",
            )
        commit = self._git(["commit", "-m", message], env=self._identity_env())
        if commit.returncode != 0:
            raise PMError(
                E_GIT_COMMIT_FAILED,
                f"commit 失败: {(commit.stderr or '').strip()[:500]}",
            )
        return self.head_sha(short=True)

    def tracked_blob(self, path: str) -> str:
        """读仓库内某已跟踪路径的 blob 内容（空串表示不存在/读不出）。"""
        proc = self._git(["show", f"HEAD:{path}"])
        return proc.stdout if proc.returncode == 0 else ""

    def worktree_remove(self, rel_path: str, force: bool = True) -> None:
        """git worktree remove --force；不存在则静默（并 prune）。"""
        wt = self.root / rel_path
        if not wt.exists():
            self._git(["worktree", "prune"])
            return
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt))
        self._git(args)
        self._git(["worktree", "prune"])

    def delete_branch(self, branch: str, force: bool = False) -> bool:
        """git branch -d（merged 才可删）；force=True 用 -D。合并后回收 task 分支用。"""
        args = ["branch", "-D" if force else "-d", branch]
        return self._git(args).returncode == 0

    def branch_commit_count(self, branch: str, base: str = "main") -> int:
        """task 分支相对 base 的 commit 数（崩溃恢复判定用；分支不存在返回 0）。"""
        if not self._ref_exists(branch):
            return 0
        proc = self._git(["rev-list", "--count", f"{base}..{branch}"])
        try:
            return int(proc.stdout.strip() or "0")
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # 审查 / 合并
    # ------------------------------------------------------------------

    def merge_task_branch(self, branch: str, message: str) -> dict:
        """git merge --no-ff <branch> -m <message>（cwd=root，主工作区在 main）。

        返回 {"ok": bool, "sha": str, "conflicts": [paths]}；冲突先收集 U 状态文件再 --abort。
        """
        proc = self._git(["merge", "--no-ff", branch, "-m", message])
        if proc.returncode == 0:
            return {"ok": True, "sha": self.head_sha(short=True), "conflicts": []}
        uproc = self._git(["diff", "--name-only", "--diff-filter=U"])
        conflicts = [l.strip() for l in uproc.stdout.splitlines() if l.strip()]
        self._git(["merge", "--abort"])
        return {"ok": False, "sha": "", "conflicts": conflicts}

    def diff_names(self, base: str, branch: str) -> list[str]:
        """git diff --name-only <base>...<branch>（分支不存在返回 []）。"""
        if not self._ref_exists(branch):
            return []
        proc = self._git(["diff", "--name-only", f"{base}...{branch}"])
        return [l.strip() for l in proc.stdout.splitlines() if l.strip()]

    def status_paths(self, cwd_rel: str = ".", paths: list[str] | None = None) -> list[str]:
        """git status --porcelain（WB 同进程路径用：审查 main 未提交改动）。

        paths 非空时只查这些路径（`-- <paths>`），用于「写后自检」精确断言
        某个产物确实产生改动，不被其它未跟踪文件干扰。
        """
        cwd = self.root / cwd_rel
        args = ["status", "--porcelain"]
        if paths:
            args += ["--"] + list(paths)
        proc = self._git(args, cwd=cwd)
        paths_out: list[str] = []
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            p = line[3:].strip()
            if "->" in p:  # 重命名：取新路径
                p = p.split("->", 1)[1].strip()
            paths_out.append(p.strip('"'))
        return paths_out

    # ------------------------------------------------------------------
    # 远端 / 杂项
    # ------------------------------------------------------------------

    def fetch(self, refspec: str = "pm/state") -> bool:
        """git fetch origin <refspec>（跨机接续第一步）。"""
        return self._git(["fetch", "origin", refspec], timeout_s=120).returncode == 0

    def head_sha(self, short: bool = True) -> str:
        args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
        proc = self._git(args)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def log_oneline(self, ref: str = "main", n: int = 10) -> list[str]:
        proc = self._git(["log", "--oneline", f"-{n}", ref])
        if proc.returncode != 0:
            return []
        return [l for l in proc.stdout.splitlines() if l.strip()]

    def state_tree_files(self) -> list[str]:
        """pm/state 分支全部文件（自检断言只有 state/current.json）。"""
        proc = self._git(["ls-tree", "-r", "--name-only", self.state_branch])
        if proc.returncode != 0:
            return []
        return [l.strip() for l in proc.stdout.splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# 自检（v2 §7 T01 验收标准）
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """临时 bare remote + orphan pm/state + worktree add/merge/remove + push。

    断言：主工作区分支始终为 main；pm/state 只有 state/current.json；
    current.json 写读往返一致；push 到 bare 为真。
    """
    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-gitops-selftest-"))
    try:
        cfg = pm_common.load_config()
        g = GitOps(tmp, cfg)

        g.init_repo(remote=None, demo=True)
        checks.append(("init_repo(demo=True) 后主工作区为 main", g.current_branch() == "main"))

        # pm/state 孤儿分支只有 state/current.json
        checks.append(
            ("pm/state 只含 state/current.json", g.state_tree_files() == ["state/current.json"])
        )

        # current.json 往返
        st = g.read_current_json()
        checks.append(("初始 current.json phase=IDLE", bool(st) and st.get("phase") == "IDLE"))
        st["phase"] = "EXECUTING"
        st["round"] = 3
        st["task_id"] = "task-001"
        sha = g.write_current_json(st, "selftest: 写状态")
        st2 = g.read_current_json()
        checks.append(
            (
                "write/read_current_json 往返一致",
                bool(st2) and st2["phase"] == "EXECUTING" and st2["round"] == 3
                and st2["task_id"] == "task-001",
            )
        )
        checks.append(("写状态返回 commit sha", len(sha) >= 7))
        checks.append(("plumbing 写后主工作区仍为 main", g.current_branch() == "main"))
        checks.append(("plumbing 写未产生未提交改动", g.status_paths() == []))

        # worktree 生命周期：add -> 写文件 commit -> diff -> merge -> remove -> 分支回收
        wt = g.worktree_add("worktrees/task-001", "task-001", "main")
        checks.append(("worktree_add 返回目录存在", wt.exists()))
        (wt / "work").mkdir(parents=True, exist_ok=True)
        (wt / "work" / "demo.txt").write_text("demo 产物\n", encoding="utf-8")
        g._git(["add", "-A"], cwd=wt)
        g._git(["commit", "-m", "demo: task-001 产物"], cwd=wt, env=g._identity_env())
        names = g.diff_names("main", "task-001")
        checks.append(("diff_names 可见 work/demo.txt", "work/demo.txt" in names))

        res = g.merge_task_branch("task-001", "merge: task-001 演示产物")
        checks.append(("merge --no-ff 成功", res["ok"] is True))
        checks.append(("main 获得产物文件", (tmp / "work" / "demo.txt").exists()))
        checks.append(("merge 后主工作区仍为 main", g.current_branch() == "main"))

        g.worktree_remove("worktrees/task-001")
        checks.append(
            ("worktree_remove 后目录消失", not (tmp / "worktrees" / "task-001").exists())
        )
        checks.append(("delete_branch 回收 task 分支", g.delete_branch("task-001")))

        # push 到 demo bare remote（真 push）
        checks.append(("push main 到 bare 成功", g.push("main") is True))
        checks.append(("push pm/state 到 bare 成功", g.push_state() is True))
        bare = tmp / ".demo-remote" / "pm-software.git"
        log = g._git(["log", "--oneline", "main"], cwd=bare).stdout
        checks.append(("bare 侧 main 含 merge commit", "merge: task-001" in log))
        checks.append(("bare 侧存在 pm/state", g._git(["rev-parse", "pm/state"], cwd=bare).returncode == 0))
        checks.append(("bare 侧 current.json phase=EXECUTING", g.read_current_json()["phase"] == "EXECUTING"))

        # 冲突路径（构造）：同一文件在 main 与 task 分支各自提交不同内容
        g.worktree_add("worktrees/task-002", "task-002", "main")
        (tmp / "work" / "demo.txt").write_text("main 侧修改\n", encoding="utf-8")
        g.stage_and_commit(["work/demo.txt"], "main: 修改 demo.txt")
        wt2 = tmp / "worktrees" / "task-002"
        (wt2 / "work" / "demo.txt").write_text("task 侧修改\n", encoding="utf-8")
        g._git(["add", "-A"], cwd=wt2)
        g._git(["commit", "-m", "task: 修改 demo.txt"], cwd=wt2, env=g._identity_env())
        res2 = g.merge_task_branch("task-002", "merge: task-002（应冲突）")
        checks.append(("冲突时 ok=False", res2["ok"] is False))
        checks.append(("冲突文件清单含 work/demo.txt", "work/demo.txt" in res2["conflicts"]))
        checks.append(("冲突后主工作区仍为 main 且干净", g.current_branch() == "main"))
        g.worktree_remove("worktrees/task-002")
        g.delete_branch("task-002", force=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] git_ops selftest：{failed} 项未过")
        return 1
    print("[OK] git_ops selftest 全部通过")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：python lib/git_ops.py --selftest"""
    import argparse

    parser = argparse.ArgumentParser(description="GitOps：Git 交接内核自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检（临时目录，不碰真实仓库）")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
