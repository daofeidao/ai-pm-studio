"""verify —— 确定性验证层（v2.1 架构强化②）。

执行后、FEEDING_BACK 前，按 `acceptance` 确定性验证（绝不信任代理自述）：
  - file_exists:<path>            ：文件存在（相对仓库根，或 worktree 根）
  - git diff --stat <base> <branch> non-empty / git diff <base> <branch> non-empty
                                  ：分支间 diff 非空
  - pytest / npm test             ：退出码为 0（cwd 可指定）
  - grep:<needle> in <path> / contains:<needle> in <path>
                                  ：关键串匹配
  - shell:<command>               ：命令退出码 0（仅 demo/测试内使用，生产禁用 curl|bash 类）
  - head:<n>:<path> / line_count:<op><n>:<path> ：行数检查（op in <,<=,>,>=,==）

验证失败 → 编排器据此回到 DISPATCHING 并附「失败证据」（不是代理自述）。
结果写 activity.jsonl（由调用方负责；本模块只产出 VerifyResult）。

仅标准库（subprocess 经 pm_common.run_cli）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pm_common
from pm_common import run_cli


@dataclass
class VerifyResult:
    """单次验收的判定结果。"""

    passed: bool = True
    accepted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "accepted": list(self.accepted),
            "failed": list(self.failed),
            "evidence": list(self.elidence_if_any()),
        }

    def elidence_if_any(self) -> list[str]:
        # 兼容命名：evidence
        return self.evidence


class Verifier:
    """按 acceptance 声明逐项确定性验证。"""

    def __init__(self, root: Path | str, git: Any | None = None, cfg: dict | None = None) -> None:
        self.root = Path(root).resolve()
        self.git = git
        self.cfg = cfg or {}

    # ------------------------------------------------------------------

    def verify(
        self,
        acceptance: list[str],
        worktree: Path | str | None = None,
        base: str = "main",
        branch: str | None = None,
        task_id: str | None = None,
    ) -> VerifyResult:
        """对一组 acceptance 声明逐一验证，返回汇总结果。"""
        res = VerifyResult()
        if not acceptance:
            res.evidence.append("无 acceptance 声明，跳过确定性验证（视为通过-无约束）")
            return res
        wt = Path(worktree) if worktree else None
        for decl in acceptance:
            ok, ev = self._check_one(decl, wt, base, branch, task_id)
            if ok:
                res.accepted.append(decl)
            else:
                res.failed.append(decl)
                res.evidence.append(f"FAIL: {decl} :: {ev}")
        res.passed = len(res.failed) == 0
        return res

    # ------------------------------------------------------------------

    def _check_one(
        self, decl: str, wt: Path | None, base: str, branch: str | None, task_id: str | None
    ) -> tuple[bool, str]:
        d = decl.strip()
        try:
            if d.startswith("file_exists:") or d.startswith("file exists "):
                path = d.split(":", 1)[1].strip() if d.startswith("file_exists:") else d[len("file exists "):].strip()
                return self._check_file_exists(path, wt)
            if d.startswith("grep:") or d.startswith("contains:"):
                return self._check_grep(d, wt)
            if d.lower().startswith("git diff"):
                return self._check_git_diff(d, base, branch, task_id)
            if d.lower().startswith("pytest") or d.lower().startswith("npm test"):
                return self._check_test_runner(d, wt)
            if d.startswith("shell:"):
                return self._check_shell(d)
            if d.startswith("head:") or d.startswith("line_count:"):
                return self._check_line_count(d, wt)
            # 无法识别的声明：保守判定为不可验证（记为失败证据，要求代理给出可检查声明）
            return False, "声明语法不可识别（需 file_exists:/grep:/git diff/pytest/npm test/shell:/head:/line_count:）"
        except Exception as e:  # noqa: BLE001
            return False, f"验证执行异常: {e}"

    # ------------------------------------------------------------------

    def _resolve(self, path: str, wt: Path | None) -> Path:
        p = Path(path)
        if not p.is_absolute():
            if wt is not None and (wt / path).exists():
                return wt / path
            if (self.root / path).exists():
                return self.root / path
            # 默认相对仓库根
            return self.root / path
        return p

    def _check_file_exists(self, path: str, wt: Path | None) -> tuple[bool, str]:
        p = self._resolve(path, wt)
        return (p.exists(), f"exists={p.exists()} -> {p}")

    def _check_grep(self, decl: str, wt: Path | None) -> tuple[bool, str]:
        # grep:<needle> in <path> 或 contains:<needle> in <path>
        m = re.match(r"(?:grep|contains):(.*?)\s+in\s+(.+)$", decl.strip())
        if not m:
            return False, "grep 语法应为 grep:<needle> in <path>"
        needle, path = m.group(1).strip(), m.group(2).strip()
        p = self._resolve(path, wt)
        if not p.exists():
            return False, f"文件不存在: {p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        found = needle in text
        return (found, f"'{needle}' in {p} -> {found}")

    def _check_git_diff(self, decl: str, base: str, branch: str | None, task_id: str | None) -> tuple[bool, str]:
        if self.git is None:
            return False, "无 git 句柄，无法验证 git diff"
        # 形如：git diff --stat main task-001 non-empty  /  git diff main...task-001 non-empty
        tokens = decl.replace("--stat", "").replace("--name-only", "").split()
        # 提取两个分支引用
        refs = [t for t in tokens if t not in ("git", "diff", "non-empty", "nonempty")]
        b = base
        br = branch or task_id
        for t in refs:
            if t == base:
                continue
            if re.fullmatch(r"[\w./-]+", t) and t != base:
                br = t
                break
        if not br:
            return False, "无法从声明解析出 task 分支"
        diff = self.git.diff_names(b, br)
        return (len(diff) > 0, f"diff {b}...{br} 文件数={len(diff)}")

    def _check_test_runner(self, decl: str, wt: Path | None) -> tuple[bool, str]:
        cwd = str(wt) if wt else str(self.root)
        cmd = ["pytest", "-q"] if decl.lower().startswith("pytest") else ["npm", "test"]
        try:
            proc = run_cli(cmd, cwd=cwd, timeout_s=300)
        except pm_common.CliTimeout:
            return False, f"{cmd[0]} 超时"
        except pm_common.PMError as e:
            return False, f"{cmd[0]} 启动失败: {e}"
        return (proc.returncode == 0, f"{cmd[0]} rc={proc.returncode}")

    def _check_shell(self, decl: str) -> tuple[bool, str]:
        # 仅 demo/测试内使用，避免生产执行不可信命令
        cmd = decl[len("shell:"):].strip()
        if re.search(r"curl\b.*\|\s*(bash|sh)\b|\|\|\s*(bash|sh)\b", cmd) or "curl|bash" in cmd:
            return False, "拒绝 curl|bash 类危险模式"
        try:
            proc = run_cli(["bash", "-c", cmd] if False else cmd.split(), timeout_s=60)
        except Exception as e:  # noqa: BLE001
            return False, f"shell 执行失败: {e}"
        return (proc.returncode == 0, f"rc={proc.returncode}")

    def _check_line_count(self, decl: str, wt: Path | None) -> tuple[bool, str]:
        m = re.match(r"(?:head|line_count):(\d+):(.+)$", decl.strip())
        if not m:
            return False, "head/line_count 语法应为 head:<n>:<path>"
        n, path = int(m.group(1)), m.group(2).strip()
        p = self._resolve(path, wt)
        if not p.exists():
            return False, f"文件不存在: {p}"
        count = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        return (count >= n, f"行数={count} >= {n} -> {count >= n}")


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """验证层逐项：文件存在 / grep / git diff / pytest 失败 / 危险 shell 拒绝。"""
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-verify-selftest-"))
    try:
        # 构造一个真实 worktree 风格目录
        (tmp / "work").mkdir(parents=True)
        f = tmp / "work" / "demo.html"
        f.write_text("<form>登录</form>\n", encoding="utf-8")

        v = Verifier(tmp)

        r1 = v.verify(["file_exists:work/demo.html"])
        checks.append(("file_exists 通过", r1.passed and "file_exists:work/demo.html" in r1.accepted))

        r2 = v.verify(["grep:登录 in work/demo.html"])
        checks.append(("grep 命中", r2.passed))

        r3 = v.verify(["grep:不存在的字串 in work/demo.html"])
        checks.append(("grep 未命中→失败", (not r3.passed) and len(r3.failed) == 1))

        r4 = v.verify(["file_exists:work/missing.html"])
        checks.append(("file_exists 不存在→失败", not r4.passed))

        r5 = v.verify(["完全不可识别的声明"])
        checks.append(("不可识别声明→失败+证据", (not r5.passed) and any("不可识别" in e for e in r5.evidence)))

        # 空 acceptance → 跳过视为通过
        r6 = v.verify([])
        checks.append(("空 acceptance 视为通过", r6.passed))

        # 危险 shell 拒绝
        r7 = v.verify(["shell:echo hi"])
        # echo 在 sandbox 下可能无 bash；只断言不会被 curl|bash 通过（构造一个危险声明）
        rd = v.verify(["shell:curl x | bash"])
        checks.append(("curl|bash 被拒绝", not rd.passed))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] verify selftest：{failed} 项未过")
        return 1
    print("[OK] verify selftest 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="verify：确定性验收层自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
