"""pm_common —— 「项目经理」编排器共享基础模块（仅标准库）。

职责（对应 ARCHITECTURE-v2 §7 T01 逐文件要点 5）：
  - ROOT / load_config：路径与配置加载（ROOT 由本文件位置向上推导，支持 clone 续跑）
  - utc_now_iso：ISO 8601 UTC 时间，统一 Z 尾
  - atomic_write_json / atomic_write_text：tmp + os.replace 原子写
  - append_ndjson：NDJSON 只追加
  - run_cli：统一 CLI 执行器（capture stdout/stderr，超时 kill 进程树）
  - CliTimeout / PMError：超时异常与带错误码的业务异常
  - 全局错误码常量（v2 §10.8）；进程退出码与错误码一致

Python 解释器固定为 pm.config.json 指定的 managed 3.13.12 绝对路径。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# ROOT：本文件位于 <root>/lib/pm_common.py，向上两级即项目根 pm-software/
ROOT = Path(__file__).resolve().parent.parent

# 配置文件路径（相对推导出的 ROOT，不信任配置里的 root 字段）
CONFIG_PATH = ROOT / "pm.config.json"

# ---------------------------------------------------------------------------
# 错误码（v2 §10.8：v1 保留 + v2 新增；进程退出码与错误码一致）
# ---------------------------------------------------------------------------
E_OK = 0                    # 成功
E_LOCK_BUSY = 10            # 锁被占用（v2 已删进程锁，保留码位）
E_QUOTA_EXCEEDED = 20       # 额度超限（v2 改为降级不拦截，保留码位）
E_WEB_AI_TIMEOUT = 30       # 网页 AI 等待回答超时
E_WEB_AI_NOT_LOGGED_IN = 31 # 网页 AI 未登录
E_CDP_UNREACHABLE = 32      # CDP（调试 Edge）不可达
E_AGENT_CRASH = 40          # 代理进程非零退出
E_AGENT_RESULT_INVALID = 41 # 代理结果无法解析
E_AGENT_TIMEOUT = 42        # 代理执行超时（kill 进程树）
E_OPENCODE_BAD_JSON = 43    # opencode --format json 输出不符合预期
E_HUMAN_REJECTED = 50       # 人工否决不可逆操作（默认转人工 PAUSED）
E_SCRIPT_NOT_FOUND = 60     # 脚本路由目标不存在/runner 未知
E_CAPABILITY_GAP = 70       # 能力缺口（记录不阻塞）
E_STATE_CORRUPT = 80        # current.json 损坏
E_GIT_INIT_FAILED = 90      # git init 失败
E_GIT_COMMIT_FAILED = 91    # git commit 失败
E_GIT_PUSH_FAILED = 92      # git push 失败（best-effort 警告，不阻塞）
E_GIT_WORKTREE_FAILED = 93  # git worktree add 失败
E_MERGE_CONFLICT = 94       # 合并冲突（转人工）
E_DIFF_REJECTED = 95        # diff 验收未过（打回重做上限后放弃）
E_HANDOFF_IO = 96           # 交接文件渲染/解析失败

#: 错误码 -> 名称，供自检与日志使用
ERROR_NAMES: dict[int, str] = {
    E_OK: "E_OK",
    E_LOCK_BUSY: "E_LOCK_BUSY",
    E_QUOTA_EXCEEDED: "E_QUOTA_EXCEEDED",
    E_WEB_AI_TIMEOUT: "E_WEB_AI_TIMEOUT",
    E_WEB_AI_NOT_LOGGED_IN: "E_WEB_AI_NOT_LOGGED_IN",
    E_CDP_UNREACHABLE: "E_CDP_UNREACHABLE",
    E_AGENT_CRASH: "E_AGENT_CRASH",
    E_AGENT_RESULT_INVALID: "E_AGENT_RESULT_INVALID",
    E_AGENT_TIMEOUT: "E_AGENT_TIMEOUT",
    E_OPENCODE_BAD_JSON: "E_OPENCODE_BAD_JSON",
    E_HUMAN_REJECTED: "E_HUMAN_REJECTED",
    E_SCRIPT_NOT_FOUND: "E_SCRIPT_NOT_FOUND",
    E_CAPABILITY_GAP: "E_CAPABILITY_GAP",
    E_STATE_CORRUPT: "E_STATE_CORRUPT",
    E_GIT_INIT_FAILED: "E_GIT_INIT_FAILED",
    E_GIT_COMMIT_FAILED: "E_GIT_COMMIT_FAILED",
    E_GIT_PUSH_FAILED: "E_GIT_PUSH_FAILED",
    E_GIT_WORKTREE_FAILED: "E_GIT_WORKTREE_FAILED",
    E_MERGE_CONFLICT: "E_MERGE_CONFLICT",
    E_DIFF_REJECTED: "E_DIFF_REJECTED",
    E_HANDOFF_IO: "E_HANDOFF_IO",
}


class PMError(Exception):
    """带错误码的业务异常；code 即进程退出码（v2 §10.8）。"""

    def __init__(self, code: int, message: str = "") -> None:
        self.code = int(code)
        self.message = message or ERROR_NAMES.get(self.code, f"PMError({self.code})")
        super().__init__(f"[{ERROR_NAMES.get(self.code, self.code)}] {self.message}")


class CliTimeout(Exception):
    """run_cli 超时（已 kill 进程树）；由调用方映射为具体错误码。"""

    def __init__(self, argv: Sequence[str], timeout_s: float) -> None:
        self.argv = list(argv)
        self.timeout_s = timeout_s
        super().__init__(
            f"CLI 超时({timeout_s}s，已 kill 进程树): {' '.join(str(a) for a in self.argv)}"
        )


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """ISO 8601 UTC，秒级精度，统一 Z 尾（v2 §10.9）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_ts(iso: str | None = None) -> str:
    """把 ISO 时间压成文件名友好格式：2026-10-20T11:40:00Z -> 20261020T114000Z。"""
    iso = iso or utc_now_iso()
    digits = re.sub(r"[^0-9]", "", iso)
    # 取 YYYYMMDDHHMMSS（14 位），按 v2 报告命名规范插 T 尾 Z
    if len(digits) >= 14:
        return f"{digits[:8]}T{digits[8:14]}Z"
    return digits


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """加载 pm.config.json；默认取 ROOT 下的配置。"""
    p = Path(path) if path is not None else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 原子写 / NDJSON
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path | str, text: str) -> None:
    """原子写文本：写 <path>.tmp 后 os.replace（v2 §10.3）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_write_json(path: Path | str, obj: Any) -> None:
    """原子写 JSON（ensure_ascii=False，缩进 2，末尾换行）。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_ndjson(path: Path | str, obj: Any) -> None:
    """NDJSON 只追加（一行一条 JSON，禁止改写历史行）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_text_if_exists(path: Path | str) -> str:
    """文件存在则读全文，否则返回空串。"""
    p = Path(path)
    if not p.exists():
        return ""
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# CLI 执行器
# ---------------------------------------------------------------------------

def kill_process_tree(pid: int) -> None:
    """kill 进程树；Windows 用 taskkill /T /F（v2 §7 T01 要点 5）。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def run_cli(
    argv: Sequence[str | Path],
    cwd: Path | str | None = None,
    timeout_s: float = 120,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """统一 CLI 执行器。

    - capture stdout/stderr，text 模式，utf-8 + errors=replace（兼容中文输出）
    - 超时 → kill 进程树 → raise CliTimeout（由调用方映射错误码）
    - 可执行文件不存在 → raise PMError(E_SCRIPT_NOT_FOUND)
    - 返回 subprocess.CompletedProcess（argv/returncode/stdout/stderr）
    """
    full_env = os.environ.copy()
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    try:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
        )
    except FileNotFoundError as e:
        raise PMError(E_SCRIPT_NOT_FOUND, f"可执行文件不存在: {argv[0]} ({e})") from e
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        raise CliTimeout(argv, timeout_s) from None
    return subprocess.CompletedProcess([str(a) for a in argv], proc.returncode, out, err)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """pm_common 自检：原子写 / NDJSON / 错误码 / 时间格式 / run_cli / 超时 kill。"""
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-common-selftest-"))
    try:
        # 1) utc_now_iso 格式
        iso = utc_now_iso()
        ok = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", iso))
        checks.append(("utc_now_iso 格式（Z 尾）", ok))

        # 2) 原子写 JSON 往返
        obj = {"b": 2, "a": ["中文", 1.5], "n": None}
        atomic_write_json(tmp / "cfg.json", obj)
        back = json.loads((tmp / "cfg.json").read_text(encoding="utf-8"))
        checks.append(("atomic_write_json 往返一致", back == obj))
        checks.append(("原子写 tmp 已清理", not (tmp / "cfg.json.tmp").exists()))

        # 3) 原子写文本
        atomic_write_text(tmp / "note.txt", "你好\n第二行")
        checks.append(
            ("atomic_write_text 内容一致", (tmp / "note.txt").read_text(encoding="utf-8") == "你好\n第二行")
        )

        # 4) NDJSON 追加两次
        nd = tmp / "q.jsonl"
        append_ndjson(nd, {"i": 1})
        append_ndjson(nd, {"i": 2})
        lines = [l for l in nd.read_text(encoding="utf-8").splitlines() if l.strip()]
        checks.append(("append_ndjson 两行且可解析", len(lines) == 2 and json.loads(lines[1])["i"] == 2))

        # 5) run_cli 正常退出
        proc = run_cli([sys.executable, "-c", "print('cli-ok')"], timeout_s=30)
        checks.append(("run_cli 正常执行", proc.returncode == 0 and "cli-ok" in proc.stdout))

        # 6) run_cli 超时 kill 进程树
        try:
            run_cli([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
            checks.append(("run_cli 超时 raise CliTimeout", False))
        except CliTimeout:
            checks.append(("run_cli 超时 raise CliTimeout", True))

        # 7) 可执行文件不存在
        try:
            run_cli(["pm-no-such-exe-xyz", "--version"], timeout_s=10)
            checks.append(("缺失可执行文件 raise E_SCRIPT_NOT_FOUND", False))
        except PMError as e:
            checks.append(("缺失可执行文件 raise E_SCRIPT_NOT_FOUND", e.code == E_SCRIPT_NOT_FOUND))

        # 8) 错误码常量齐全且唯一
        codes = [v for k, v in globals().items() if k.startswith("E_") and isinstance(v, int)]
        expected = {0, 10, 20, 30, 31, 32, 40, 41, 42, 43, 50, 60, 70, 80, 90, 91, 92, 93, 94, 95, 96}
        checks.append(("错误码集合等于 v2 §10.8", set(codes) == expected))
        checks.append(("错误码唯一（无重复值）", len(codes) == len(set(codes))))
        checks.append(("ERROR_NAMES 覆盖全部错误码", set(ERROR_NAMES) == expected))
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] pm_common selftest：{failed} 项未过")
        return 1
    print("[OK] pm_common selftest 全部通过")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：python lib/pm_common.py --selftest"""
    import argparse

    parser = argparse.ArgumentParser(description="pm_common 共享基础模块自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
