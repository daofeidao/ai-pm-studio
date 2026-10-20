# ask-web-ai（项目级 skill）

> 封装 `browser-automation-cdp`，复用已登录的调试 Edge 向 DeepSeek 网页提问并取回回答。
> 对应架构：ARCHITECTURE-v2 §7 T03；P1 主链路第一跳。

## 调用约定

```bash
# 演示（零浏览器进程，离线可复现）
python .workbuddy/skills/ask-web-ai/web_ai.py ask "你好" --demo

# 真实（需 Edge 以 --remote-debugging-port=9222 启动且已登录 DeepSeek）
python .workbuddy/skills/ask-web-ai/web_ai.py ask "如何设计登录页"

# 自检
python .workbuddy/skills/ask-web-ai/web_ai.py --selftest
```

Python 解释器固定 managed 3.13.12（`pm.config.json` 的 `python` 字段）。

## CDP 五步（真实路径）

| 步 | cdp.py 子命令 | 失败错误码 |
|----|--------------|-----------|
| ① 探测 | `status` | 不可达 → `E_CDP_UNREACHABLE=32`（提示启动调试 Edge） |
| ② 导航 | `open https://chat.deepseek.com` | 落登录页 → `E_WEB_AI_NOT_LOGGED_IN=31` |
| ③ 输入提交 | `type <输入框> <问题>` + `click <发送钮>` | — |
| ④ 轮询等待 | `wait "<JS条件>" --timeout 120` | 超时 → `E_WEB_AI_TIMEOUT=30` |
| ⑤ 抽取 | `text --max 8000` | 空回答 → `E_WEB_AI_TIMEOUT=30` |

选择器/URL/超时常量集中在 `lib/webai_adapter.py` 的 `DeepSeekAdapter` 常量区（页面改版只改一处，Q3）。

## 写死纪律（违反即返工）

1. **严禁加载 `browser-agent` / `playwright-browser-agent` skill**；本 skill 只允许调
   `browser-automation-cdp` 的 `scripts/cdp.py`，绝对路径：
   `C:/Users/Administrator/.workbuddy/skills/browser-automation-cdp/scripts/cdp.py`
   （同时记录在 `pm.config.json` 的 `web_ai.cdp_py`）。
2. **不下载、不启动任何 Chromium / Playwright**；浏览器只复用已开 Edge 实例。
3. HTTP 一律标准库 `urllib.request`（本 skill 无 HTTP 调用；额度查询见 dispatch-agent）。
4. demo（`--demo`）内部切 `MockWebAIAdapter`，返回 `demo_fixtures` 假回答，**零浏览器进程**。

## 错误码

`E_WEB_AI_TIMEOUT=30` / `E_WEB_AI_NOT_LOGGED_IN=31` / `E_CDP_UNREACHABLE=32` / `E_WEB_AI_SELECTOR=33`（主路径选择器缺失且兜底 `drissionpage-ai` 不可用 → 编排器转 AWAITING_APPROVAL 人审）；
主流程记 `last_error` 不崩溃（v2 §10.8）。

## 扩展（P3）

`ChatGPTPlaceholderAdapter` 已就位（`--provider chatgpt`），调用报 `E_CAPABILITY_GAP=70` 不阻塞。
