"""demo_fixtures —— 离线 demo 固定数据（v2 §8：Git 全真、外部依赖全假）。

所有 demo 路径共用本模块的假数据：
  - DEMO_WEB_REPLY        ：假 DeepSeek 回答（含【动作清单】与【验收标准】两段）
  - DEMO_AGENT_RESULT     ：假 AgentResult（JSON 可序列化字典）
  - DEMO_QUOTA            ：假额度（max_percent=22，正常不降级）
  - DEMO_OPENCODE_JSON    ：假 `opencode run --format json` 输出（NDJSON，含 sessionID 与 text）
  - demo_reply_for_round  ：第 n 轮回复；第 2 轮含「已完成」语义用于收尾

注意：本模块不 import 其他 lib 模块，避免循环依赖。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 假 DeepSeek 回答（第 1 轮）：包含动作清单与验收标准两段，供 PARSING 解析
# ---------------------------------------------------------------------------
DEMO_WEB_REPLY: str = """【分析】演示任务：生成登录页。建议按以下动作清单执行，产物统一落在 work/ 下。

【动作清单】
1. 创建 work/demo-login.html，包含登录表单静态结构与基础样式
2. 补充表单前端校验逻辑并自测
3. 输出改动清单与自测结论

【验收标准】
- work/demo-login.html 存在且包含 <form> 表单
- 全部改动落在 work/ 目录下
- 自测结论记录在轮次报告中

【结论】完成以上三项即视为本轮回合取得实质进展，可进入下一轮复查。
"""

# ---------------------------------------------------------------------------
# 假 AgentResult（demo 默认，无不可逆操作）
# ---------------------------------------------------------------------------
DEMO_AGENT_RESULT: dict = {
    "status": "success",
    "agent": "opencode",
    "summary": "已生成 work/demo-login.html 并提交到 task 分支",
    "artifacts": ["work/demo-login.html"],
    "irreversible_ops": [],
    "error_code": None,
    "error_detail": None,
    "session_id": "ses_demo_001",
    "duration_ms": 1234,
}

#: 演示人工审批链路的假结果：含不可逆操作（git push），进入 HUMAN_REVIEW
DEMO_AGENT_RESULT_IRREVERSIBLE: dict = {
    "status": "success",
    "agent": "opencode",
    "summary": "已生成 work/demo-login.html；检测到不可逆操作需人工审批",
    "artifacts": ["work/demo-login.html"],
    "irreversible_ops": ["git_push"],
    "error_code": None,
    "error_detail": None,
    "session_id": "ses_demo_002",
    "duration_ms": 1234,
}

# ---------------------------------------------------------------------------
# 假额度（v2 §8.2：demo 直接返回 DEMO_QUOTA，max_percent=22）
# ---------------------------------------------------------------------------
DEMO_QUOTA: dict = {
    "agent": "opencode",
    "max_percent": 22,
    "windows": {"5h": 10, "week": 22, "month": 22},
    "source": "opencode-stats-cli",
    "fetched_at": "2026-10-20T11:34:40Z",
    "stale": False,
}

#: 演示「额度超限降级 WorkBuddy」的假额度（max_percent=95 > 90）
DEMO_QUOTA_EXCEEDED: dict = {
    "agent": "opencode",
    "max_percent": 95,
    "windows": {"5h": 95, "week": 95, "month": 95},
    "source": "opencode-stats-cli",
    "fetched_at": "2026-10-20T11:34:40Z",
    "stale": False,
}

#: 演示「两源都失败 stale 放行」的假额度
DEMO_QUOTA_STALE: dict = {
    "agent": "opencode",
    "max_percent": 0,
    "windows": {},
    "source": "none",
    "fetched_at": "2026-10-20T11:34:40Z",
    "stale": True,
}

# ---------------------------------------------------------------------------
# 假 `opencode run --format json` 输出（NDJSON：整体不可解析，按行取最后一个对象）
# ---------------------------------------------------------------------------
DEMO_OPENCODE_JSON: str = """{"type":"step_start","sessionID":"ses_demo_001","timestamp":"2026-10-20T11:35:00Z"}
{"type":"text","sessionID":"ses_demo_001","text":"已创建 work/demo-login.html 并补充表单校验"}
{"type":"step_finish","sessionID":"ses_demo_001","tokens":{"input":1200,"output":300},"cost_usd":0.0042}
"""

# ---------------------------------------------------------------------------
# 第 n 轮假回复：第 1 轮正常派活；第 >=2 轮含「已完成」语义，编排器据此收尾
# ---------------------------------------------------------------------------
DEMO_WEB_REPLY_ROUND2: str = """【分析】第 2 轮复查：登录页已生成且表单校验通过，符合第 1 轮验收标准。

【动作清单】
1. 复查 work/demo-login.html 产物完整性
2. 确认验收标准全部满足后收尾

【验收标准】
- 产物完整且全部改动落在 work/ 目录下
- 无需进一步改动

【结论】任务已完成，无需继续派活，可以进入持久化与交接。
"""


def demo_reply_for_round(n: int) -> str:
    """返回第 n 轮的假 DeepSeek 回复（n>=2 均为收尾语义）。"""
    if n <= 1:
        return DEMO_WEB_REPLY
    return DEMO_WEB_REPLY_ROUND2


#: MockOpenCodeProvider 在 worktree 内写出的演示产物（相对 worktree 根）
DEMO_WORKTREE_FILES: dict[str, str] = {
    "work/demo-login.html": """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>演示登录页</title>
</head>
<body>
  <h1>演示登录页（pm-orchestrator demo 产物）</h1>
  <form id="login-form" action="/login" method="post">
    <label>用户名 <input type="text" name="username" required></label>
    <label>密码 <input type="password" name="password" required minlength="6"></label>
    <button type="submit">登录</button>
  </form>
  <script>
    // demo：基础前端校验（非空 + 密码长度）
    document.getElementById('login-form').addEventListener('submit', function (e) {
      var u = this.username.value.trim();
      var p = this.password.value;
      if (!u || p.length < 6) { e.preventDefault(); alert('请填写用户名且密码至少 6 位'); }
    });
  </script>
</body>
</html>
""",
    "work/demo-login.css": """/* demo 产物：登录页基础样式 */
body { font-family: system-ui, sans-serif; margin: 2rem; }
form { display: grid; gap: 0.75rem; max-width: 20rem; }
label { display: grid; gap: 0.25rem; }
""",
}
