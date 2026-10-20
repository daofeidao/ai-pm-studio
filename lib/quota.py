"""quota —— QuotaService：OpenCode 额度双源优先级查询（v2 R9 / §7 T04）。

优先级（v2 §2.2 R9）：
  1. `opencode stats --json --days N --project . --cost`（本地 CLI，零网络依赖）优先；
     按 `monthly_budget_usd` 折算 percent；budget=0 时返回 None 让位下一源（Q2）。
  2. HTTP `GET https://opencode.ai/zen/go/v1/usage` 兜底；取 5h/周/月 max percent。
  3. 两源都失败 -> QuotaInfo(stale=True)，**不拦截**（放行继续派活）。

60s 内存缓存（cache_s）；demo 模式直接返回 demo_fixtures.DEMO_QUOTA。
HTTP 一律标准库 urllib.request；异常全部吞掉转 stale，绝不 raise 断主流程。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pm_common
from pm_common import E_OK, run_cli, utc_now_iso
from demo_fixtures import DEMO_QUOTA, DEMO_QUOTA_EXCEEDED, DEMO_QUOTA_STALE


@dataclass
class QuotaInfo:
    """额度信息（v2 §4.2 类图）。"""

    agent: str = "opencode"
    max_percent: float = 0.0
    windows: dict[str, float] = field(default_factory=dict)
    source: str = "none"
    fetched_at: str = ""
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "max_percent": self.max_percent,
            "windows": self.windows,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
        }


def _dig(data: Any, keys: list[str]) -> Any:
    """按候选键路径探测数值（兼容未固化 schema，Q4 同思路）。"""
    for k in keys:
        if isinstance(data, dict) and k in data and isinstance(data[k], (int, float)):
            return float(data[k])
    return None


def percent_from_stats_data(data: Any, budget_usd: float) -> float | None:
    """从 `opencode stats --json` 输出折算 percent；budget<=0 返回 None（让位下一源）。"""
    if budget_usd <= 0:
        return None
    cost = _dig(
        data,
        ["cost_usd", "total_cost", "cost", "costUsd", "totalCost", "spend_usd"],
    )
    if cost is None and isinstance(data, dict):
        # 二级结构：{"cost": {"total": x}} / {"usage": {"cost": x}} / {"totals": {...}}
        for wrap in ("cost", "usage", "totals", "total"):
            sub = data.get(wrap)
            if isinstance(sub, dict):
                cost = _dig(sub, ["total", "usd", "cost_usd", "total_cost", "cost", "amount"])
                if cost is not None:
                    break
            elif isinstance(sub, (int, float)):
                cost = float(sub)
                break
    if cost is None:
        return None
    return min(100.0, cost / budget_usd * 100.0)


def percent_from_usage_data(data: Any) -> tuple[float, dict[str, float]] | None:
    """从 usage HTTP JSON 取 5h/周/月 max percent；取不到返回 None。"""
    windows: dict[str, float] = {}
    aliases = {
        "5h": ["5h", "five_hour", "fiveHour", "hour_5", "hour"],
        "week": ["week", "weekly", "7d"],
        "month": ["month", "monthly", "30d"],
    }
    if isinstance(data, dict):
        for label, keys in aliases.items():
            v = _dig(data, keys)
            if v is None:
                for k in keys:
                    sub = data.get(k)
                    if isinstance(sub, dict):
                        v = _dig(sub, ["percent", "usage_percent", "used_percent", "ratio", "pct"])
                        if v is not None:
                            break
            if v is not None:
                windows[label] = float(v)
    if not windows:
        return None
    return max(windows.values()), windows


class QuotaService:
    """额度服务：stats CLI 优先 -> usage HTTP 兜底 -> stale 放行；60s 缓存。"""

    def __init__(self, cfg: dict[str, Any], demo: bool = False) -> None:
        self.cfg = cfg
        self.demo = demo or bool(cfg.get("demo", {}).get("enabled", False))
        self._cache: QuotaInfo | None = None
        self._cache_at: float = 0.0
        q = cfg.get("quota", {})
        self.cache_s: float = float(q.get("cache_s", 60))
        self.usage_url: str = q.get("usage_url", "https://opencode.ai/zen/go/v1/usage")
        self.degrade_percent: float = float(q.get("degrade_percent", 90))

    # ------------------------------------------------------------------

    def fetch(self, force: bool = False) -> QuotaInfo:
        """取额度；demo 直接返回假额度；60s 内不重复请求（计时可证）。"""
        if self.demo:
            return self._from_dict(DEMO_QUOTA)
        now = time.monotonic()
        if not force and self._cache is not None and (now - self._cache_at) < self.cache_s:
            return self._cache
        info = self._from_stats_cli(".") or self._from_usage_http()
        if info is None:
            info = QuotaInfo(
                agent="opencode",
                max_percent=0.0,
                windows={},
                source="none",
                fetched_at=utc_now_iso(),
                stale=True,
            )
        self._cache = info
        self._cache_at = now
        return info

    # ------------------------------------------------------------------

    @staticmethod
    def _from_dict(d: dict[str, Any]) -> QuotaInfo:
        return QuotaInfo(
            agent=d.get("agent", "opencode"),
            max_percent=float(d.get("max_percent", 0)),
            windows=dict(d.get("windows", {})),
            source=d.get("source", "demo-mock"),
            fetched_at=d.get("fetched_at", utc_now_iso()),
            stale=bool(d.get("stale", False)),
        )

    def _from_stats_cli(self, project: str) -> QuotaInfo | None:
        """本地 stats CLI（零网络依赖优先源）。"""
        oc = self.cfg.get("opencode", {})
        exe = oc.get("exe", "")
        if not exe:
            return None
        try:
            proc = run_cli(
                [
                    exe, "stats", "--json",
                    "--days", str(int(oc.get("stats_days", 30))),
                    "--project", project,
                    "--cost",
                ],
                cwd=project if project != "." else None,
                timeout_s=60,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            data = json.loads(proc.stdout)
            percent = percent_from_stats_data(data, float(oc.get("monthly_budget_usd", 0)))
            if percent is None:
                return None
            return QuotaInfo(
                agent="opencode",
                max_percent=percent,
                windows={"month": percent},
                source="opencode-stats-cli",
                fetched_at=utc_now_iso(),
                stale=False,
            )
        except Exception:
            return None

    def _from_usage_http(self) -> QuotaInfo | None:
        """usage HTTP 兜底源。"""
        try:
            req = urllib.request.Request(self.usage_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            got = percent_from_usage_data(data)
            if got is None:
                return None
            percent, windows = got
            return QuotaInfo(
                agent="opencode",
                max_percent=percent,
                windows=windows,
                source="opencode-usage-http",
                fetched_at=utc_now_iso(),
                stale=False,
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """demo 额度 / 60s 缓存计时 / 超限与 stale 假额度 / 纯函数折算。"""
    checks: list[tuple[str, bool]] = []
    cfg = pm_common.load_config()

    # 1) demo 额度 = DEMO_QUOTA（22%）
    svc = QuotaService(cfg, demo=True)
    info = svc.fetch()
    checks.append(("demo 额度 max_percent=22", info.max_percent == 22))
    checks.append(("demo 额度非 stale", info.stale is False))

    # 2) 缓存计时：连续两次 fetch，第二次不重新探测（对象同一）
    svc2 = QuotaService(cfg, demo=True)
    a = svc2.fetch()
    b = svc2.fetch(force=False)
    checks.append(("demo 路径不落缓存也稳定", a.max_percent == b.max_percent))
    # 非 demo 但 exe 指向不存在 -> stale；并用缓存证明 60s 内不重复请求
    cfg_bad = json.loads(json.dumps(cfg))
    cfg_bad["opencode"]["exe"] = "pm-no-such-opencode.exe"
    cfg_bad["quota"]["usage_url"] = "http://127.0.0.1:1/never"
    svc3 = QuotaService(cfg_bad)
    i1 = svc3.fetch()
    t_second = time.monotonic()
    i2 = svc3.fetch()
    dt = time.monotonic() - t_second
    checks.append(("两源失败 -> stale=True 不拦截", i1.stale is True))
    checks.append(("stale 放行（max_percent=0 且不 raise）", i1.max_percent == 0.0))
    checks.append(("60s 缓存：二次 fetch 命中（<0.5s 且同一对象）", dt < 0.5 and i2 is i1))

    # 3) 折算纯函数
    checks.append(
        (
            "stats 折算 percent",
            percent_from_stats_data({"cost_usd": 5.0}, 10.0) == 50.0,
        )
    )
    checks.append(
        (
            "budget=0 让位下一源",
            percent_from_stats_data({"cost_usd": 5.0}, 0) is None,
        )
    )
    got = percent_from_usage_data({"5h": 10, "week": 22, "month": 31})
    checks.append(
        ("usage 取 5h/周/月 max", bool(got) and got[0] == 31 and got[1]["5h"] == 10),
    )
    checks.append(("usage 取不到 -> None", percent_from_usage_data({"foo": 1}) is None))

    # 4) 演示假额度字典
    checks.append(("DEMO_QUOTA_EXCEEDED 超降级线", DEMO_QUOTA_EXCEEDED["max_percent"] > 90))
    checks.append(("DEMO_QUOTA_STALE stale=True", DEMO_QUOTA_STALE["stale"] is True))

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] quota selftest：{failed} 项未过")
        return 1
    print("[OK] quota selftest 全部通过")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="QuotaService：额度双源查询自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
