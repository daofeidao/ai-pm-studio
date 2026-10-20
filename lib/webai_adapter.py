"""webai_adapter —— 网页 AI 适配器抽象（v2 §4.6 / P2-2 / P3）。

- WebAIAdapter（ABC）：send_prompt / get_context_usage
- DeepSeekAdapter：真实路径，CDP 五步（status -> open -> type/click -> wait -> text），
  选择器/URL/超时常量集中在常量区（Q3：页面改版只改一处）
- MockWebAIAdapter：demo 路径，返回 demo_fixtures 假回答，零浏览器进程
- ChatGPTPlaceholderAdapter：P3 占位，send_prompt 抛 E_CAPABILITY_GAP

浏览器只走 browser-automation-cdp 的 scripts/cdp.py（绝对路径见 pm.config.json web_ai.cdp_py），
严禁加载 browser-agent / playwright-browser-agent，不下载 Chromium。
"""
from __future__ import annotations

import json
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pm_common
from pm_common import (
    E_CDP_UNREACHABLE,
    E_CAPABILITY_GAP,
    E_WEB_AI_NOT_LOGGED_IN,
    E_WEB_AI_SELECTOR,
    E_WEB_AI_TIMEOUT,
    PMError,
    atomic_write_text,
    run_cli,
    utc_now_iso,
)
from demo_fixtures import DEMO_WEB_REPLY, demo_reply_for_round

# ---------------------------------------------------------------------------
# 主路径选择器常量区（v2.1：页面改版只改这里；Q3 实测后固化）
# ---------------------------------------------------------------------------

#: DeepSeek 主路径选择器（CDP 五步用；2026-10-20 实测页面固化）
#: 实测要点：
#:   - 输入框是 `textarea`，**id 为空**，靠 placeholder「给 DeepSeek 发送消息」定位；
#:   - 发送钮是 `div[role=button]`（不是 <button>，也没有 aria-label），
#:     位于输入框右侧 `div.bf38813a` 容器内；选择器漂移时由「回车兜底」接管；
#:   - 回答容器必须排除「深度思考」块：思考块是 `ds-think-content`，
#:     正式回答是 `div.ds-message div.ds-markdown`。
DEEPSEEK_SELECTORS: dict[str, str] = {
    "input": "textarea[placeholder*='发送消息']",
    "send_button": "div.bf38813a div[role=button]",
    "answer_container": "div.ds-message div.ds-markdown",
}

#: 兜底后端模块名（缺失选择器时尝试 import；提供 aiAct 动作）
DRISSIONPAGE_AI_MODULE = "drissionpage_ai"

#: 兜底后端提供的动作名（drissionpage-ai 的 AI 辅助点击/输入）
DRISSIONPAGE_AI_ACTION = "aiAct"


def drissionpage_ai_available() -> bool:
    """兜底后端 drissionpage-ai 是否可 import（仅探测，不加载重依赖）。

    不可用时调用方应 raise E_WEB_AI_SELECTOR（转人审），不静默降级。
    """
    import importlib.util

    try:
        return importlib.util.find_spec(DRISSIONPAGE_AI_MODULE) is not None
    except (ImportError, ValueError):
        return False


def resolve_web_ai_selector(
    selectors: dict[str, str] | None = None,
) -> tuple[str, str]:
    """解析主路径选择器；缺失时走兜底，否则 raise E_WEB_AI_SELECTOR。

    返回 (模式, 选择器)：模式为 "primary"（走 CDP 主路径）或 "aiAct"（兜底）。

    规则（v2.1 T2）：
      1. 主路径三个选择器齐备 -> ("primary", input 选择器)；
      2. 选择器缺失且 drissionpage-ai 可 import -> ("aiAct", "")；
      3. 否则 raise PMError(E_WEB_AI_SELECTOR) —— 由编排器转 AWAITING_APPROVAL。
    """
    sel = dict(DEEPSEEK_SELECTORS if selectors is None else selectors)
    required = ("input", "send_button", "answer_container")
    missing = [k for k in required if not str(sel.get(k, "")).strip()]
    if not missing:
        return ("primary", str(sel["input"]).strip())
    if drissionpage_ai_available():
        return ("aiAct", "")
    raise PMError(
        E_WEB_AI_SELECTOR,
        "网页 AI 主路径选择器缺失（"
        + ",".join(missing)
        + f"）且兜底 {DRISSIONPAGE_AI_MODULE} 不可用；需人工提供选择器或安装兜底后端",
    )


# ---------------------------------------------------------------------------
# 数据结构（v2 §4.2 类图）
# ---------------------------------------------------------------------------

@dataclass
class ContextUsage:
    """上下文用量（按字符数，不引 tokenizer）。"""

    chars_used: int = 0
    chars_limit: int = 0


@dataclass
class WebAIReply:
    """一次提问的回答。"""

    text: str = ""
    context_usage: ContextUsage | None = None
    raw_path: str | None = None


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class WebAIAdapter(ABC):
    """网页 AI 适配器接口。"""

    @abstractmethod
    def send_prompt(self, prompt: str, memory_pack: str | None = None) -> WebAIReply:
        """发送提问并返回回答。"""

    @abstractmethod
    def get_context_usage(self) -> ContextUsage:
        """当前上下文用量（字符数）。"""


# ---------------------------------------------------------------------------
# DeepSeek 真实适配器（CDP 五步）
# ---------------------------------------------------------------------------

class DeepSeekAdapter(WebAIAdapter):
    """DeepSeek 网页适配器：经 cdp.py 驱动已登录的调试 Edge。

    五步（v2 §7 T03 要点 2）：
      ① status 探测（不可达 -> E_CDP_UNREACHABLE）
      ② open 导航到 CHAT_URL（落登录页 -> E_WEB_AI_NOT_LOGGED_IN）
      ③ type 输入 + click 发送钮提交
      ④ wait 轮询回答容器（超时 -> E_WEB_AI_TIMEOUT）
      ⑤ text 抽取回答
    """

    # ---- 常量区（选择器变更只改这里；Q3 实测后固化） ----
    CHAT_URL = "https://chat.deepseek.com"
    INPUT_SELECTOR = DEEPSEEK_SELECTORS["input"]                # 输入框
    SEND_BUTTON_SELECTOR = DEEPSEEK_SELECTORS["send_button"]    # 发送钮
    ANSWER_CONTAINER_SELECTOR = DEEPSEEK_SELECTORS["answer_container"]  # 回答容器
    LOGIN_PAGE_MARKERS = ("登录", "login", "sign in", "扫码")
    TIMEOUT_S = 120
    POLL_S = 3
    # cdp.py 退出码：0 成功；1 一般失败；2 CDP 不可达
    CDP_RC_UNREACHABLE = 2

    def __init__(self, root: Path | str, cfg: dict[str, Any]) -> None:
        self.root = Path(root)
        self.cfg = cfg
        wa = cfg.get("web_ai", {})
        self.cdp_py: str = wa.get(
            "cdp_py",
            "C:/Users/Administrator/.workbuddy/skills/browser-automation-cdp/scripts/cdp.py",
        )
        self.python: str = cfg.get(
            "python", "C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
        )
        self.chat_url: str = wa.get("chat_url", self.CHAT_URL)
        self.timeout_s: int = int(wa.get("timeout_s", self.TIMEOUT_S))
        self.poll_s: float = float(wa.get("poll_s", self.POLL_S))
        # 主路径选择器：常量区默认值 + 配置覆盖（配置可置空以演练兜底/缺失）
        configured = wa.get("selectors")
        self.selectors: dict[str, str] = dict(DEEPSEEK_SELECTORS)
        if isinstance(configured, dict):
            for k in ("input", "send_button", "answer_container"):
                if k in configured:
                    self.selectors[k] = str(configured[k] or "").strip()

    # ---- CDP 调用封装 ----

    def _cdp(self, args: list[str], timeout_s: float | None = None):
        """调用 cdp.py 子命令；CDP 不可达（rc=2）映射 E_CDP_UNREACHABLE。"""
        proc = run_cli(
            [self.python, self.cdp_py] + args,
            cwd=self.root,
            timeout_s=timeout_s or 60,
        )
        if proc.returncode == self.CDP_RC_UNREACHABLE:
            raise PMError(
                E_CDP_UNREACHABLE,
                "CDP 不可达：请以调试模式启动 Edge（--remote-debugging-port=9222）后重试",
            )
        return proc

    # ---- JS 取值封装（回答容器 / 输入框状态） ----

    def _eval(self, js: str, timeout_s: float = 30) -> str:
        """执行 JS 并返回字符串结果（cdp.py eval 的 stdout）。"""
        proc = self._cdp(["eval", js], timeout_s=timeout_s)
        return (proc.stdout or "").strip()

    def _answer_count(self) -> int:
        """当前页面已有的正式回答数（不含「深度思考」块）。"""
        js = f"document.querySelectorAll({json.dumps(self.ANSWER_CONTAINER_SELECTOR)}).length"
        try:
            return int(self._eval(js))
        except ValueError:
            return 0

    def _input_value(self) -> str:
        """输入框当前内容（空串表示已提交/未输入）。"""
        js = (
            f"(document.querySelector({json.dumps(self.INPUT_SELECTOR)})"
            " || {}).value || ''"
        )
        out = self._eval(js)
        return "" if out in ("undefined", "null") else out

    def _last_answer(self) -> str:
        """取最后一个正式回答的纯文本。"""
        js = (
            f"((Array.from(document.querySelectorAll({json.dumps(self.ANSWER_CONTAINER_SELECTOR)})).pop())"
            " || {}).innerText || ''"
        )
        return self._eval(js).strip()

    def _send_by_enter(self) -> None:
        """发送钮点击无效时的兜底：向输入框派发 Enter 键事件（实测可提交）。"""
        js = (
            "(function(){var ta=document.querySelector(%s);"
            "if(!ta){return 'NOT_FOUND';}ta.focus();"
            "['keydown','keypress','keyup'].forEach(function(t){"
            "ta.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',"
            "keyCode:13,which:13,bubbles:true,cancelable:true}));});"
            "return 'OK';})()" % json.dumps(self.INPUT_SELECTOR)
        )
        self._eval(js)

    def build_steps(self, prompt: str) -> list[list[str]]:
        """CDP 五步命令清单（dry-run / 自检用，不执行）。"""
        sel = json.dumps(self.ANSWER_CONTAINER_SELECTOR)
        count_js = f"document.querySelectorAll({sel}).length"
        last_js = (
            f"((Array.from(document.querySelectorAll({sel})).pop())||{{}}).innerText||''"
        )
        return [
            ["status"],
            ["open", self.chat_url],
            ["type", self.INPUT_SELECTOR, prompt],
            ["click", self.SEND_BUTTON_SELECTOR],
            ["wait", f"{count_js} > 0", "--timeout", str(self.timeout_s)],
            ["eval", last_js],
        ]

    # ---- WebAIAdapter 接口 ----

    def send_prompt(self, prompt: str, memory_pack: str | None = None) -> WebAIReply:
        full_prompt = prompt if not memory_pack else f"{memory_pack}\n\n{prompt}"
        # ⓪ 选择器解析（v2.1 T2）：主路径缺失 -> 兜底 aiAct -> 否则 E_WEB_AI_SELECTOR
        mode, _selector = resolve_web_ai_selector(self.selectors)
        if mode == "aiAct":
            return self._send_prompt_ai_act(full_prompt)
        # ① 探测
        self._cdp(["status"], timeout_s=30)
        # ② 导航
        proc = self._cdp(["open", self.chat_url], timeout_s=self.timeout_s)
        page_text = proc.stdout or ""
        if any(marker in page_text for marker in self.LOGIN_PAGE_MARKERS):
            # 登录页特征词命中且无回答容器 -> 未登录
            raise PMError(
                E_WEB_AI_NOT_LOGGED_IN,
                "DeepSeek 未登录：请在调试 Edge 中完成登录并保持会话后重试",
            )
        # ③ 输入 + 记录发送前的回答数（用于只认“新回答”，不复读旧回答）
        self._cdp(["type", self.INPUT_SELECTOR, full_prompt], timeout_s=60)
        before = self._answer_count()
        # ④ 提交：优先点发送钮；无效（选择器漂移/按钮禁用）时回车兜底
        self._cdp(["click", self.SEND_BUTTON_SELECTOR], timeout_s=60)
        time.sleep(0.6)
        if self._input_value():
            self._send_by_enter()
        # ⑤ 轮询新回答容器（排除 ds-think-content 的思考块）
        answer = ""
        deadline_loops = max(1, int(self.timeout_s / max(self.poll_s, 0.5)))
        for _ in range(deadline_loops):
            if self._answer_count() > before:
                answer = self._last_answer()
                if answer:
                    break
            time.sleep(self.poll_s)
        if not answer:
            raise PMError(E_WEB_AI_TIMEOUT, f"等待 DeepSeek 回答超时（{self.timeout_s}s）")
        # ⑤ 抽取（text 已取到）；落盘原始回答备查
        raw_path = self.root / "reports" / f"webai-deepseek-{pm_common.compact_ts()}.md"
        atomic_write_text(raw_path, f"# Q\n{full_prompt}\n\n# A\n{answer}\n")
        return WebAIReply(
            text=answer,
            context_usage=self.get_context_usage(),
            raw_path=str(raw_path.relative_to(self.root)),
        )

    def get_context_usage(self) -> ContextUsage:
        """假用量基准：阈值的 70%（未超阈值不注入记忆包）。"""
        threshold = int(self.cfg.get("memory", {}).get("threshold_chars", 24000))
        return ContextUsage(chars_used=int(threshold * 0.7), chars_limit=threshold)

    def _send_prompt_ai_act(self, full_prompt: str) -> WebAIReply:
        """兜底路径：用 drissionpage-ai 的 aiAct 动作完成输入+提交+取答。

        仅在主路径选择器缺失、但 drissionpage-ai 可 import 时才可能走到
        （resolve_web_ai_selector 已把关）。后端不可用时抛 E_WEB_AI_SELECTOR。
        """
        import importlib

        try:
            mod = importlib.import_module(DRISSIONPAGE_AI_MODULE)
        except ImportError as e:
            raise PMError(
                E_WEB_AI_SELECTOR,
                f"兜底后端 {DRISSIONPAGE_AI_MODULE} 导入失败：{e}",
            ) from e
        action = getattr(mod, DRISSIONPAGE_AI_ACTION, None)
        if action is None:
            raise PMError(
                E_WEB_AI_SELECTOR,
                f"兜底后端 {DRISSIONPAGE_AI_MODULE} 未提供 {DRISSIONPAGE_AI_ACTION} 动作",
            )
        try:
            answer = action(full_prompt)
        except Exception as e:  # noqa: BLE001 —— 兜底失败按选择器缺失转人审
            raise PMError(
                E_WEB_AI_SELECTOR,
                f"兜底 {DRISSIONPAGE_AI_MODULE}.{DRISSIONPAGE_AI_ACTION} 执行失败：{e}",
            ) from e
        answer = str(answer or "").strip()
        if not answer:
            raise PMError(E_WEB_AI_SELECTOR, "兜底 aiAct 未取到回答")
        raw_path = self.root / "reports" / f"webai-aiact-{pm_common.compact_ts()}.md"
        atomic_write_text(raw_path, f"# Q\n{full_prompt}\n\n# A\n{answer}\n")
        return WebAIReply(
            text=answer,
            context_usage=self.get_context_usage(),
            raw_path=str(raw_path.relative_to(self.root)),
        )


# ---------------------------------------------------------------------------
# Mock 适配器（demo：零浏览器进程）
# ---------------------------------------------------------------------------

class MockWebAIAdapter(WebAIAdapter):
    """demo 用假网页 AI：返回 demo_fixtures 固定回答，不启 CDP/浏览器。

    `selector_missing=True` 时模拟「主路径选择器缺失且兜底不可用」：
    send_prompt 直接 raise E_WEB_AI_SELECTOR（编排器据此转 AWAITING_APPROVAL）。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        round_no: int = 1,
        selector_missing: bool = False,
    ) -> None:
        self.cfg = cfg
        self.round_no = round_no
        self.selector_missing = selector_missing

    def send_prompt(self, prompt: str, memory_pack: str | None = None) -> WebAIReply:
        if self.selector_missing:
            # 走真实的选择器解析逻辑：缺选择器 + 无兜底 -> E_WEB_AI_SELECTOR
            resolve_web_ai_selector({"input": "", "send_button": "", "answer_container": ""})
        text = demo_reply_for_round(self.round_no)
        return WebAIReply(text=text, context_usage=self.get_context_usage(), raw_path=None)

    def get_context_usage(self) -> ContextUsage:
        threshold = int(self.cfg.get("memory", {}).get("threshold_chars", 24000))
        return ContextUsage(chars_used=int(threshold * 0.7), chars_limit=threshold)


# ---------------------------------------------------------------------------
# ChatGPT 占位（P3）
# ---------------------------------------------------------------------------

class ChatGPTPlaceholderAdapter(WebAIAdapter):
    """P3 占位：接口就绪，实现未做；调用即报 E_CAPABILITY_GAP（记录不阻塞）。"""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or {}

    def send_prompt(self, prompt: str, memory_pack: str | None = None) -> WebAIReply:
        raise PMError(E_CAPABILITY_GAP, "ChatGPT 适配器为 P3 占位，尚未实现（provider=chatgpt）")

    def get_context_usage(self) -> ContextUsage:
        return ContextUsage(chars_used=0, chars_limit=0)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_adapter(
    root: Path | str,
    cfg: dict[str, Any],
    demo: bool = False,
    round_no: int = 1,
    provider: str | None = None,
    selector_missing: bool = False,
) -> WebAIAdapter:
    """按配置/demo 标志构造适配器（P3 新增 provider 不改编排代码）。

    `selector_missing=True` 仅用于 demo 演练 T2：主路径选择器缺失场景。
    """
    if demo:
        return MockWebAIAdapter(cfg, round_no=round_no, selector_missing=selector_missing)
    name = (provider or cfg.get("web_ai", {}).get("provider", "deepseek")).lower()
    if name == "deepseek":
        return DeepSeekAdapter(root, cfg)
    if name == "chatgpt":
        return ChatGPTPlaceholderAdapter(cfg)
    raise PMError(E_CAPABILITY_GAP, f"未知 web_ai provider: {name}")
