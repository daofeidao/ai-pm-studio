"""capability_probe —— DSH/OpenClaw/CodeBuddy 能力探测（v2 §7 T05 / P1-5）。

规则（v2 §10.12 能力缺口不阻塞）：
  - workbuddy 恒 true（同进程执行器，宿主即 WB）
  - opencode 用 `opencode --version` 探活（本地 CLI，零网络）
  - dsh / openclaw / codebuddy 保持 enabled=null，记 capability_gaps[]
  - 任何探测异常都吞掉转 gaps，**绝不 raise 断主流程**
  - 结果原子写 capabilities.json（入库）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pm_common import E_OK, atomic_write_json, load_config, local_request, run_cli, utc_now_iso

#: 已知外部代理（探测接口未确认，enabled=null）
EXTERNAL_AGENTS: tuple[str, ...] = ("dsh", "openclaw", "codebuddy")

#: 探测不到时的默认原因
GAP_REASON = "探测接口未确认（enabled=null，不阻塞主流程）"

#: v2.1 逆向工具链探测（缺失记 capability_gap，不阻塞，降级通用 OpenCode）
REVERSE_TOOLCHAIN: tuple[str, ...] = (
    "asar", "jadx", "apktool", "otool", "nm", "go", "rg", "ghidra",
)


def _tool_present(exe: str) -> bool:
    """探测某 CLI 工具是否在 PATH（零网络；异常吞掉）。"""
    try:
        proc = run_cli([exe, "--version"], timeout_s=20)
        return proc.returncode == 0
    except Exception:
        # 退一步：仅查 which/where
        try:
            probe = run_cli(["where", exe] if sys.platform == "win32" else ["which", exe], timeout_s=20)
            return probe.returncode == 0
        except Exception:
            return False


def _probe_magpie_gateway(base_url: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    """探测 Magpie 网关：GET {base_url}/models，超时 timeout_s 秒。

    **走 pm_common.local_request**：显式绕过 HTTP_PROXY/HTTPS_PROXY，避免本机
    127.0.0.1 请求被系统代理吞掉而误报「网关不可达」。

    返回 (是否可达, 原因)。任何异常都吞掉转 (False, 原因)，**绝不 raise**。
    """
    url = base_url.rstrip("/") + "/models"
    try:
        _status, body = local_request(url, timeout_s=timeout_s)
        try:
            data = json.loads(body)
            models = data.get("data") if isinstance(data, dict) else None
            n = len(models) if isinstance(models, list) else 0
            return True, f"网关可达（{url}，{n} 个模型）"
        except json.JSONDecodeError:
            return True, f"网关可达（{url}，响应非 JSON 但连接成功）"
    except Exception as e:  # noqa: BLE001 —— 探测异常一律转 gap
        return False, f"网关不可达({url})：{e}"


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

        # v2.1 T7：Magpie 网关探测（GET {base_url}/models，超时 3s）
        magpie = (self.cfg.get("agents") or {}).get("magpie") or {}
        magpie_enabled = bool(magpie.get("enabled", False))
        magpie_base = str(magpie.get("base_url", "") or "")
        magpie_model = str(magpie.get("model", "") or "")
        magpie_timeout = float(magpie.get("timeout_s", 3) or 3)
        if magpie_enabled and magpie_base:
            ok, reason = _probe_magpie_gateway(magpie_base, magpie_timeout)
            caps["agents"]["magpie"] = {
                "enabled": True,
                "kind": "gateway",
                "base_url": magpie_base,
                "model": magpie_model,
                "reachable": ok,
            }
            if not ok:
                caps["capability_gaps"].append(
                    {"agent": "magpie", "reason": f"{reason}（回退直连，不阻塞循环）", "at": utc_now_iso()}
                )
        else:
            caps["agents"]["magpie"] = {
                "enabled": magpie_enabled,
                "kind": "gateway",
                "base_url": magpie_base,
                "model": magpie_model,
                "reachable": None,
            }

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

        # v2.1 逆向工具链探测（不阻塞，缺失记 capability_gap，降级通用 OpenCode）
        rev: dict[str, Any] = {}
        rev_gaps: list[str] = []
        for tool in REVERSE_TOOLCHAIN:
            present = _tool_present(tool)
            rev[tool] = {"present": present}
            if not present:
                rev_gaps.append(tool)
        caps["reverse_toolchain"] = rev
        if rev_gaps:
            caps["capability_gaps"].append(
                {"agent": "reverse_toolchain", "reason": f"缺失工具: {', '.join(rev_gaps)}（降级通用 OpenCode）", "at": utc_now_iso()}
            )

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
            ("reverse_toolchain 已探测", isinstance(caps.get("reverse_toolchain"), dict)
             and set(caps["reverse_toolchain"].keys()) == set(REVERSE_TOOLCHAIN))
        )
        checks.append(
            ("capabilities.json 已写回", (tmp / "capabilities.json").exists())
        )

        # 接线自测：HTTP_PROXY 指向死端口时，_probe_magpie_gateway 仍能探到本机网关
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _ModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b'{"data":[{"id":"group/workbuddy-3x"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "no_proxy")
        saved = {k: os.environ.get(k) for k in proxy_keys}
        try:
            dead = "http://127.0.0.1:9"
            for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
                os.environ[k] = dead
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            ok_gw, reason_gw = _probe_magpie_gateway(
                f"http://127.0.0.1:{srv.server_address[1]}/v1", 5.0
            )
            checks.append(
                ("HTTP_PROXY 生效时本机网关仍可达（local_request 接线）",
                 ok_gw is True and "1 个模型" in reason_gw)
            )
        finally:
            srv.shutdown()
            srv.server_close()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
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
