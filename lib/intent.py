"""intent —— 意图契约 schema 与校验（v2.1 架构强化①）。

网页 AI / 编排内核确定性分离：网页 AI 可返回结构化「意图」JSON，必须经
`validate_intent` 校验通过才能进入下一状态（DISPATCHING / 收尾）；校验失败则
回到 ASKING_WEB_AI 让网页 AI 重生成（不得进入下一状态）。

意图 schema（确定性内核的对外契约）：
    {
      "analysis": str,                               # 网页 AI 对任务的分析
      "next_action": {
        "type": "execute_script" | "execute_agent" | "request_info" | "done",
        "target": str | null,                        # 脚本 id / 代理名 / null
        "instruction": str,                          # 交给执行器的指令
        "acceptance": [str, ...]                     # 机器可检查声明（必填，done 除外）
      }
    }

`acceptance` 必须是机器可检查声明（见 lib/verify.py 支持的语法），例如：
    "file_exists:work/demo-login.html"
    "git diff --stat main task-001 non-empty"
    "grep:登录 in work/demo-login.html"
本模块只负责 schema 合法性；具体执行由 verify.py 负责。

仅标准库。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

#: 合法的 next_action.type 取值
NEXT_ACTION_TYPES: tuple[str, ...] = (
    "execute_script",
    "execute_agent",
    "request_info",
    "done",
)

#: 意图块在 markdown 回复中的围栏标记（网页 AI 可把意图放在 ```json ... ``` 里）
INTENT_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


@dataclass
class Intent:
    """解析后的结构化意图。"""

    analysis: str = ""
    next_action_type: str = "done"
    target: str | None = None
    instruction: str = ""
    acceptance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis": self.analysis,
            "next_action": {
                "type": self.next_action_type,
                "target": self.target,
                "instruction": self.instruction,
                "acceptance": list(self.acceptance),
            },
        }


def parse_intent_from_reply(text: str) -> Intent | None:
    """从网页 AI 回复中提取结构化意图。

    - 优先匹配 ```json ... ``` 围栏块，尝试解析为意图；
    - 否则扫描整段文本，看是否有 ``"next_action"`` 的 JSON；
    - 都没有返回 None（走既有 markdown 解析路径）。
    """
    candidates: list[str] = []
    for m in INTENT_FENCE_RE.finditer(text or ""):
        candidates.append(m.group(1))
    candidates.append(text or "")
    for cand in candidates:
        obj = _maybe_json(cand)
        intent = _intent_from_obj(obj)
        if intent is not None:
            return intent
    return None


def _maybe_json(s: str) -> Any:
    """尝试把字符串解析为 JSON 对象；失败返回 None。"""
    if not s or not s.strip():
        return None
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _intent_from_obj(obj: Any) -> Intent | None:
    """把 dict 转为 Intent（仅当结构像意图时返回，否则 None）。"""
    if not isinstance(obj, dict):
        return None
    na = obj.get("next_action")
    if not isinstance(na, dict):
        # 允许顶层直接给字段（宽松兼容）
        if "analysis" not in obj and "next_action_type" not in obj and "acceptance" not in obj:
            return None
        na = {
            "type": obj.get("next_action_type", "done"),
            "target": obj.get("target"),
            "instruction": obj.get("instruction", ""),
            "acceptance": obj.get("acceptance", []),
        }
    if "type" not in na:
        return None
    return Intent(
        analysis=str(obj.get("analysis", "")),
        next_action_type=str(na.get("type", "done")),
        target=na.get("target"),
        instruction=str(na.get("instruction", "")),
        acceptance=list(na.get("acceptance", [])) if na.get("acceptance") else [],
    )


def validate_intent(intent: Intent) -> tuple[bool, list[str]]:
    """校验意图合同；返回 (是否通过, 错误列表)。

    规则：
    1. next_action.type 必须在合法集合内；
    2. 除 done / request_info 外，acceptance 必须非空且为机器可检查声明；
    3. execute_script / execute_agent 必须有 instruction；
    4. acceptance 每项应为字符串且符合可检查语法（宽松：允许自定义但需非空）。
    """
    errors: list[str] = []
    if intent.next_action_type not in NEXT_ACTION_TYPES:
        errors.append(
            f"next_action.type 非法：{intent.next_action_type!r}，"
            f"必须为 {NEXT_ACTION_TYPES}"
        )

    is_done = intent.next_action_type == "done"
    is_request = intent.next_action_type == "request_info"

    if intent.next_action_type in ("execute_script", "execute_agent"):
        if not intent.instruction or not intent.instruction.strip():
            errors.append("execute_script/execute_agent 必须提供 instruction")

    if not is_done and not is_request:
        if not intent.acceptance:
            errors.append(
                "acceptance 必须非空（机器可检查声明），除非 type=done/request_info"
            )
        for i, acc in enumerate(intent.acceptance):
            if not isinstance(acc, str) or not acc.strip():
                errors.append(f"acceptance[{i}] 必须是非空字符串")
                continue
            if not _looks_machine_checkable(acc):
                errors.append(
                    f"acceptance[{i}] 不是机器可检查声明：{acc!r}"
                )

    return (len(errors) == 0, errors)


def _looks_machine_checkable(decl: str) -> bool:
    """宽松判断声明是否像机器可检查（避免代理塞入主观断言）。

    支持：`file_exists:` / `grep:` / `git diff` / `pytest` / `npm test` /
    `exit_code` / `contains` / `shell:` 前缀；其他视为不可检查。
    """
    d = decl.strip().lower()
    recognizable = (
        d.startswith("file_exists:")
        or d.startswith("file exists")
        or d.startswith("grep:")
        or d.startswith("contains:")
        or d.startswith("git diff")
        or d.startswith("shell:")
        or d.startswith("exit_code")
        or d.startswith("pytest")
        or d.startswith("npm test")
        or d.startswith("head:")
        or d.startswith("line_count:")
    )
    return recognizable


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """意图解析 + 校验往返。"""
    checks: list[tuple[str, bool]] = []

    # 合法意图（围栏块）
    good = (
        "分析：需要生成登录页。\n```json\n"
        '{"analysis":"生成登录页","next_action":{'
        '"type":"execute_agent","target":"opencode",'
        '"instruction":"生成 work/demo-login.html",'
        '"acceptance":["file_exists:work/demo-login.html"]}}\n```'
    )
    it = parse_intent_from_reply(good)
    checks.append(("围栏块解析出意图", it is not None))
    if it:
        ok, errs = validate_intent(it)
        checks.append(("合法意图校验通过", ok and not errs))

    # 非法：acceptance 不可检查 + type 非法
    bad = Intent(
        analysis="x",
        next_action_type="execute_agent",
        instruction="do it",
        acceptance=["页面看起来很棒"],
    )
    ok, errs = validate_intent(bad)
    checks.append(("不可检查 acceptance 校验失败", (not ok) and len(errs) >= 1
                   and any("机器可检查" in e for e in errs)))

    # done 允许空 acceptance
    done_intent = Intent(next_action_type="done")
    ok2, _ = validate_intent(done_intent)
    checks.append(("done 允许空 acceptance", ok2))

    # request_info 允许空 acceptance
    req = Intent(next_action_type="request_info", instruction="请问登录接口？")
    ok3, _ = validate_intent(req)
    checks.append(("request_info 允许空 acceptance", ok3))

    # 无意图块返回 None
    checks.append(("纯 markdown 返回 None", parse_intent_from_reply("【动作清单】\n1. a") is None))

    # execute_agent 缺 instruction 校验失败
    no_ins = Intent(next_action_type="execute_agent", acceptance=["file_exists:x"])
    ok4, errs4 = validate_intent(no_ins)
    checks.append(("execute_agent 缺 instruction 失败", (not ok4) and any("instruction" in e for e in errs4)))

    failed = 0
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    if failed:
        print(f"[FAIL] intent selftest：{failed} 项未过")
        return 1
    print("[OK] intent selftest 全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="intent：意图契约校验自检")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
