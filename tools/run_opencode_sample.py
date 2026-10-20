"""run_opencode_sample —— T5：采集真实 `opencode run --format json` stdout 样本。

用途
----
架构 v2.1 T5 要求「实际运行一次真实 opencode CLI，把**真实 stdout** 贴进
`docs/opencode-json-sample.md`」，并据此校正 dispatcher 的解析策略
（若真实输出是 NDJSON 流式，必须按行/流式解析）。

实现要点
--------
- 用 `subprocess.Popen` + `communicate(timeout=...)` 精确控时；
- 超时用 `pm_common.kill_process_tree`（Windows 下 `taskkill /T /F`）**杀整棵进程树**
  （opencode-cli.exe 会拉起 OpenCode.exe 子进程，单纯 kill 父进程会留下孤儿）；
- stdout / stderr 分别落盘，供人工比对；
- 退出码与超时都如实记录，**不做任何美化**。

用法::

    python tools/run_opencode_sample.py [--timeout 180] [--message "echo smoke"]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pm_common
from pm_common import kill_process_tree, load_config

#: 默认采样提示词（最小可复现）
DEFAULT_MESSAGE = "echo smoke"

#: 输出目录（相对仓库根）
OUT_DIR = "reports/t5"


def capture(
    exe: str,
    message: str,
    timeout_s: float,
    out_dir: Path,
) -> dict:
    """跑一次 opencode 并把 stdout/stderr 落盘；返回采样结论 dict。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [exe, "run", "--format", "json", message, "--standalone", "--auto"]
    print(f"[T5] 执行：{' '.join(argv)}")
    print(f"[T5] 超时上限：{timeout_s}s")

    started = pm_common.utc_now_iso()
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        result = {
            "ok": False,
            "reason": f"可执行文件不存在：{e}",
            "argv": argv,
            "started_at": started,
            "finished_at": pm_common.utc_now_iso(),
            "returncode": None,
            "timed_out": False,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_path": "",
            "stderr_path": "",
        }
        (out_dir / "sample-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 —— 超时后尽力回收，失败即放弃
            stdout, stderr = "", ""
        returncode = proc.returncode

    stdout_path = out_dir / "opencode-sample-stdout.txt"
    stderr_path = out_dir / "opencode-sample-stderr.txt"
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")

    result = {
        "ok": returncode == 0 and not timed_out and bool((stdout or "").strip()),
        "reason": (
            "超时（已 kill 进程树）"
            if timed_out
            else f"退出码 {returncode}"
        ),
        "argv": argv,
        "started_at": started,
        "finished_at": pm_common.utc_now_iso(),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_bytes": len((stdout or "").encode("utf-8")),
        "stderr_bytes": len((stderr or "").encode("utf-8")),
        "stdout_path": str(stdout_path.relative_to(ROOT)).replace("\\", "/"),
        "stderr_path": str(stderr_path.relative_to(ROOT)).replace("\\", "/"),
        "stdout_preview": (stdout or "")[:4000],
        "stderr_preview": (stderr or "")[:2000],
    }
    (out_dir / "sample-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T5：采集真实 opencode --format json 样本")
    parser.add_argument("--timeout", type=float, default=180.0, help="超时秒数（默认 180）")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="采样提示词")
    args = parser.parse_args(argv)

    cfg = load_config(ROOT / "pm.config.json")
    exe = cfg.get("opencode", {}).get("exe", "")
    if not exe:
        print("[T5] pm.config.json 未配置 opencode.exe")
        return 2

    result = capture(exe, args.message, args.timeout, ROOT / OUT_DIR)
    print(f"[T5] ok={result['ok']} reason={result['reason']}")
    print(f"[T5] stdout={result['stdout_bytes']}B stderr={result['stderr_bytes']}B")
    print(f"[T5] 结果：{OUT_DIR}/sample-result.json")
    if result["stdout_preview"]:
        print("--- stdout 前 2000 字符 ---")
        print(result["stdout_preview"][:2000])
    if result["stderr_preview"]:
        print("--- stderr 前 1000 字符 ---")
        print(result["stderr_preview"][:1000])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
