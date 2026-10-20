"""capability_probe —— DSH/OpenClaw/CodeBuddy 能力探测（v2 §7 T05 / P1-5）。

规则（v2 §10.12 能力缺口不阻塞）：
  - workbuddy 恒 true（同进程执行器，宿主即 WB）
  - opencode 用 `opencode --version` 探活（本地 CLI，零网络）
  - dsh / openclaw / codebuddy 保持 enabled=null，记 capability_gaps[]
  - 任何探测异常都吞掉转 gaps，**绝不 raise 断主流程**
  - 结果原子写 capabilities.json（入库）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pm_common import E_OK, atomic_write_json, load_config, run_cli, utc_now_iso

#: 已知外部代理（探测接口未确认，enabled=null）
EXTERNAL_AGENTS: tuple[str, ...] = ("dsh", "openclaw", "codebuddy")

#: 探测不到时的默认原因
GAP_REASON = "探测接口未确认（enabled=null，不阻塞主流程）"


class CapabilityProbe:
    """能力探测：写 capabilities.json，失败只记 gaps 不 raise。"""

    def __init__(self, root: Path | str, cfg: dict[str, Any] | None = None) -> None:
        self.root = Path(root).resolve()
        self.cfg = cfg or load_config(self.root / "pm.config.json")
        self.out_path = self.root / self.cfg.get("paths", {}).get(
            "capabilities", "capabilities.json"
        )

    def probe_all(self) -> dict[str, Any]:
        """探测全部代理并写 capabilities.json；返回结果 dict。"""
        caps: dict[str, Any] = {
            "schema_version": "2.0",
            "agents": {},
            "capability_gaps": [],
        }

        # workbuddy：同进程执行器，恒可用
        caps["agents"]["workbuddy"] = {"enabled": True, "kind": "in_process"}

        # opencode：--version 探活
        exe = self.cfg.get("opencode", {}).get("exe", "")
        try:
            proc = run_cli([exe, "--version"], timeout_s=30)
            ok = proc.returncode == 0
            caps["agents"]["opencode"] = {
                "enabled": ok,
                "kind": "cli",
                "version": (proc.stdout or proc.stderr or "").strip().splitlines()[0][:80]
                if (proc.stdout or proc.stderr or "").strip()
                else "",
            }
            if not ok:
                caps["capability_gaps"].append(
                    {"agent": "opencode", "reason": "--version 非零退出", "at": utc_now_iso()}
                )
        except Exception as e:  # 探测异常不 raise（v2 §10.12）
            caps["agents"]["opencode"] = {"enabled": False, "kind": "cli"}
            caps["capability_gaps"].append(
                {"agent": "opencode", "reason": f"探测异常: {e}", "at": utc_now_iso()}
            )

        # 外部代理：enabled=null + gaps 记录
        for name in EXTERNAL_AGENTS:
            caps["agents"][name] = {"enabled": None, "kind": "external"}
            caps["capability_gaps"].append({"agent": name, "reason": GAP_REASON, "at": utc_now_iso()})

        atomic_write_json(self.out_path, caps)
        return caps


def _selftest() -> int:
    """探测不 raise；gaps 有记录；capabilities.json 可写回。"""
    import tempfile

    checks: list[tuple[str, bool]] = []
    tmp = Path(tempfile.mkdtemp(prefix="pm-capprobe-selftest-"))
    try:
        cfg = load_config()
        probe = CapabilityProbe(tmp, cfg)
        try:
            caps = probe.probe_all()
            ok = True
        except Exception as e:  # noqa: BLE001
            caps = {}
            ok = False
            print(f"  probe_all 异常: {e}")
        checks.append(("probe_all 不 raise", ok))
        checks.append(("workbuddy 恒 true", caps.get("agents", {}).get("workbuddy", {}).get("enabled") is True))
        checks.append(
            ("dsh/openclaw/codebuddy enabled=null", all(
                caps.get("agents", {}).get(a, {}).get("enabled") is None for a in EXTERNAL_AGENTS
            ))
        )
        checks.append(
            ("capability_gaps 有记录", len(caps.get("capability_gaps", [])) >= len(EXTERNAL_AGENTS))
        )
        checks.append(
            ("capabilities.json 已写回", (tmp / "capabilities.json").exists())
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok2 in checks:
        print(f"  [{'OK' if ok2 else 'FAIL'}] {name}")
        if not ok2:
            failed += 1
    if failed:
        print(f"[FAIL] capability_probe selftest：{failed} 项未过")
        return 1
    print("[OK] capability_probe selftest 全部通过")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="CapabilityProbe：代理能力探测自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
