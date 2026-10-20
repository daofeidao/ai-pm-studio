"""web_ai —— ask-web-ai skill 入口：封装 browser-automation-cdp 向 DeepSeek 提问取答。

用法：
    python .workbuddy/skills/ask-web-ai/web_ai.py ask "问题" [--demo]
    python .workbuddy/skills/ask-web-ai/web_ai.py --selftest

纪律（SKILL.md 写死条款）：
  - 只允许调 browser-automation-cdp 的 scripts/cdp.py（绝对路径见 pm.config.json web_ai.cdp_py）
  - 严禁加载 browser-agent / playwright-browser-agent；不下载/不启动任何 Chromium/Playwright
  - --demo 走 MockWebAIAdapter，零浏览器进程，保证离线可复现
"""
from __future__ import annotations

import sys
from pathlib import Path

# 本文件位于 <root>/.workbuddy/skills/ask-web-ai/web_ai.py，向上四级为仓库根
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

import pm_common
from pm_common import E_CAPABILITY_GAP, E_OK, PMError, load_config
import webai_adapter as wa
from webai_adapter import MockWebAIAdapter, build_adapter


def cmd_ask(question: str, demo: bool, provider: str | None = None) -> int:
    """提问并打印回答；返回进程退出码。"""
    cfg = load_config(ROOT / "pm.config.json")
    try:
        adapter = build_adapter(ROOT, cfg, demo=demo, provider=provider)
        reply = adapter.send_prompt(question)
    except PMError as e:
        print(f"[PM] ask 失败: {e}")
        return e.code
    for line in getattr(adapter, "preflight_log", []):
        print(line)
    usage = reply.context_usage
    if usage is not None:
        print(f"[context] chars_used={usage.chars_used}/{usage.chars_limit}")
    print("[answer]")
    print(reply.text)
    if reply.raw_path:
        print(f"[raw] {reply.raw_path}")
    return E_OK


def _selftest() -> int:
    """自检：demo 路径全过 + 真实路径 dry-run（不执行 CDP）+ ChatGPT 占位报错。"""
    checks: list[tuple[str, bool]] = []
    cfg = load_config(ROOT / "pm.config.json")

    # 1) demo：MockWebAIAdapter 第 1/2 轮回答非空且零浏览器进程
    for rnd in (1, 2):
        adapter = MockWebAIAdapter(cfg, round_no=rnd)
        reply = adapter.send_prompt("演示问题")
        checks.append((f"demo round{rnd} 回答非空", bool(reply.text.strip())))
    mock = MockWebAIAdapter(cfg)
    usage = mock.get_context_usage()
    checks.append(
        ("demo 用量在阈值附近（70%）", 0 < usage.chars_used < usage.chars_limit)
    )

    # 2) 真实路径 dry-run：五步命令清单可构造（不执行，不启浏览器）
    ds = wa.DeepSeekAdapter(ROOT, cfg)
    steps = ds.build_steps("你好")
    checks.append(("DeepSeek 五步命令清单非空", len(steps) == 6 and steps[0] == ["eval", "1+1"]))
    checks.append(
        (
            "DeepSeek 常量区齐全",
            bool(ds.CHAT_URL) and bool(ds.INPUT_SELECTOR) and bool(ds.ANSWER_CONTAINER_SELECTOR),
        )
    )
    checks.append(
        (
            "Edge 自愈参数齐全（端口/Profile/超时）",
            ds.debug_port == 9222 and bool(ds.edge_profile) and ds.restart_timeout_s > 0,
        )
    )

    # 3) ChatGPT 占位：send_prompt 报 E_CAPABILITY_GAP
    try:
        wa.ChatGPTPlaceholderAdapter(cfg).send_prompt("hi")
        checks.append(("ChatGPT 占位 raise E_CAPABILITY_GAP", False))
    except PMError as e:
        checks.append(("ChatGPT 占位 raise E_CAPABILITY_GAP", e.code == E_CAPABILITY_GAP))

    # 4) build_adapter demo 分支
    checks.append(
        ("build_adapter(demo=True) 返回 Mock", isinstance(build_adapter(ROOT, cfg, demo=True), MockWebAIAdapter))
    )

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] web_ai selftest：{failed} 项未过")
        return 1
    print("[OK] web_ai selftest 全部通过（demo 路径零浏览器进程）")
    return E_OK


def main(argv: list[str] | None = None) -> int:
    """argparse 入口：ask 子命令 / --selftest，全局 --demo。"""
    import argparse

    parser = argparse.ArgumentParser(description="ask-web-ai：封装 CDP 向 DeepSeek 提问取答")
    parser.add_argument("--demo", action="store_true", help="demo 模式：Mock 适配器，零浏览器进程")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    parser.add_argument("--provider", default=None, help="覆盖 web_ai.provider（deepseek/chatgpt）")
    sub = parser.add_subparsers(dest="cmd")
    p_ask = sub.add_parser("ask", help="提问")
    p_ask.add_argument("question", help="问题全文")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.cmd == "ask":
        return cmd_ask(args.question, demo=args.demo, provider=args.provider)
    parser.print_help()
    return E_OK


if __name__ == "__main__":
    sys.exit(main())
