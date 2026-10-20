# ask-web-ai（项目级 skill）

> 封装 `browser-automation-cdp`，复用已登录的调试 Edge 向 DeepSeek 网页提问并取回回答。
> 对应架构：ARCHITECTURE-v2 §7 T03；P1 主链路第一跳。

## 调用约定

```bash
# 演示（零浏览器进程，离线可复现）
python .workbuddy/skills/ask-web-ai/web_ai.py ask "你好" --demo

# 真实（需 Edge 以 --remote-debugging-port=9222 --remote-allow-origins=* 启动且已登录 DeepSeek）
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
| ③ 输入提交 | `type <输入框> <问题>` → `click <发送钮>`；点不动则派发 Enter 兜底 | — |
| ④ 轮询等待 | `eval` 统计回答容器数，超过发送前基线才收 | 超时 → `E_WEB_AI_TIMEOUT=30` |
| ⑤ 抽取 | `eval` 取**最后一个** `div.ds-message div.ds-markdown` 的 innerText | 空回答 → `E_WEB_AI_TIMEOUT=30` |

选择器/URL/超时常量集中在 `lib/webai_adapter.py` 的 `DeepSeekAdapter` 常量区，
且 `pm.config.json` 的 `web_ai.selectors` 会覆盖常量（**两处都要改**，页面改版只改这一对）。

### 2026-10-20 实测固化

| 项 | 实测值 | 备注 |
|----|--------|------|
| 输入框 | `textarea[placeholder*='发送消息']` | `id` 为空；旧的 `textarea#chat-input` 不存在 |
| 发送钮 | `div.bf38813a div[role=button]` | 不是 `<button>`，无 `aria-label`；点击无效时回车兜底 |
| 回答容器 | `div.ds-message div.ds-markdown` | 必须排除思考块 `ds-think-content`，否则会把推理过程当回答 |
| 页面全文本 | 不可用于取答 | 侧边栏历史会话会被一起抓进来，须按容器抽取 |

### 启动参数（踩过）

必须带 `--remote-allow-origins=*`：

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --remote-debugging-port=9222 --remote-allow-origins=* `
  --user-data-dir=C:\temp\edge-pm https://chat.deepseek.com
```

`cdp.py` 的 WebSocket 握手会带 `Origin: http://127.0.0.1:9222`，
Edge 111+ 默认拒绝（握手 403），表现为「status 正常但 type/click/wait 全部无效果」，
最终只会看到 `E_WEB_AI_TIMEOUT`。加上该参数后 403 消失。

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
