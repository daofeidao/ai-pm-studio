import webview
import threading
import os
import sys
import time
import subprocess
import json
import re
import socketserver
import webbrowser
from http.server import SimpleHTTPRequestHandler

# 确保能找到 lib 目录
if getattr(sys, 'frozen', False):
    base_dir = sys._MEIPASS
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

try:
    from lib.webai_adapter import DeepSeekAdapter
    from lib.pm_common import load_config
    HAS_ADAPTER = True
except ImportError as e:
    print(f"导入 webai_adapter 失败: {e}")
    HAS_ADAPTER = False

def get_base_path():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

# 绝对干净、无乱码的 HTML 源码
HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>AI 项目经理 Studio</title>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; background-color: #1e1e1e; color: #fff; font-family: sans-serif; overflow: hidden; display: flex; flex-direction: column; }
        .header { padding: 15px; background: #252526; border-bottom: 1px solid #333; font-size: 18px; font-weight: bold; }
        .main { flex: 1; padding: 20px; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; }
        .box { background: #252526; border-radius: 5px; border: 1px solid #333; padding: 15px; }
        .title { color: #888; font-size: 13px; margin-bottom: 10px; }
        #log-output { white-space: pre-wrap; font-family: monospace; color: #4af626; font-size: 13px; }
        .footer { padding: 15px; background: #252526; border-top: 1px solid #333; display: flex; gap: 10px; }
        input { flex: 1; padding: 12px; border-radius: 5px; border: 1px solid #444; background: #1e1e1e; color: #fff; outline: none; font-size: 14px; }
        button { padding: 12px 24px; background: #0078D4; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; }
        button:hover { background: #005A9E; }
    </style>
</head>
<body>
    <div class="header">AI 项目经理编排器 v1.0</div>
    <div class="main">
        <div class="box">
            <div class="title">当前状态 STATE</div>
            <div id="state-text">等待任务...</div>
        </div>
        <div class="box" style="flex: 1;">
            <div class="title">执行日志 / 代码输出 LOG</div>
            <div id="log-output"></div>
        </div>
    </div>
    <div class="footer">
        <select id="engine" style="padding:10px;border-radius:5px;border:1px solid #444;background:#1e1e1e;color:#fff;">
            <option value="web">网页 AI 直驱（短脚本）</option>
            <option value="oc">本地 OpenCode（长代码）</option>
        </select>
        <input id="task-input" type="text" placeholder="输入你想让项目经理做的任务（例如：做一个单文件网页版贪吃蛇）...">
        <button id="open-btn" style="background:#3a3a3a;">产物目录</button>
        <button id="start-btn">开始执行</button>
    </div>
    <script>
        document.getElementById('open-btn').onclick = function() {
            if(window.pywebview && window.pywebview.api) window.pywebview.api.open_output();
        };
        document.getElementById('start-btn').onclick = function() {
            var val = document.getElementById('task-input').value;
            if(!val) return;
            var engine = document.getElementById('engine').value;
            if(window.pywebview && window.pywebview.api) {
                document.getElementById('log-output').innerText = engine === 'oc' ? '任务已下发，正在交给本地 OpenCode...' : '任务已下发，正在请求网页 AI...';
                window.pywebview.api.start_task(val, engine);
                setInterval(fetchState, 2000);
            }
        };
        function fetchState() {
            fetch('/state.json?t=' + Date.now()).then(r => r.json()).then(data => {
                document.getElementById('state-text').innerText = `[${data.current_state}] ${data.message || ''}`;
                document.getElementById('log-output').innerText = data.code || '';
            }).catch(e => {});
        }
        setInterval(fetchState, 2000);
    </script>
</body>
</html>"""

PROMPT_TMPL = """你是一个顶级的全栈工程师。请根据用户任务，直接输出一个完整的、无任何省略的、可独立运行的单文件代码。

【技术栈规范】
如果是网页游戏、可视化界面或可交互工具，请使用纯 HTML + CSS + JavaScript，并包裹在 ```html 和 ``` 中。
如果是纯计算或系统自动化脚本，请使用 Python，并包裹在 ```python 和 ``` 中。

【绝对红线】
1. 禁止使用任何省略号（如 ... 或 省略）！即使代码很长，也必须完整输出每一行，不要跳步。
2. 如果是 HTML，必须包含完整的 <!DOCTYPE html> 到 </html> 结构，双击即可在浏览器直接玩，无需任何外部依赖。

用户的任务：{goal}"""


class Truncated(Exception):
    """AI 输出残缺（省略号 / 结构不完整 / 太短）。"""


def extract_code(text):
    """从回答里取代码，返回 (kind, code)；kind ∈ {'html','python'}。

    网页渲染后的 innerText 没有 ``` 围栏，所以先试围栏，再按内容猜类型。
    """
    m = re.search(r'```(html|python)\s*\n(.*?)\n```', text or "", re.DOTALL)
    if m:
        return m.group(1), m.group(2)
    body = (text or "").strip()
    low = body.lower()
    if "<!doctype" in low or low.startswith("<html"):
        return "html", body
    if re.search(r'^(import |from |def |print\()', body, re.MULTILINE):
        return "python", body
    return None, None


def validate_code(kind, code):
    """拦截残缺输出。返回空串表示通过，否则返回原因。"""
    body = (code or "").strip()
    if not body:
        return "代码为空"
    if len(body) < 100:
        return f"代码只有 {len(body)} 字节，明显被截断"
    if "…" in body or re.search(r'^\s*(\.\.\.|· · ·)\s*$', body, re.MULTILINE):
        return "代码里含省略号（... / …），AI 把中间部分偷掉了"
    low = body.lower()
    if kind == "html":
        if not low.startswith("<!doctype") and not low.startswith("<html"):
            return "HTML 开头缺 <!DOCTYPE html>"
        if "</html>" not in low:
            return "HTML 结尾缺 </html>（输出被截断）"
    elif kind == "python":
        try:
            compile(body, "<ai-generated>", "exec")
        except SyntaxError as e:
            return f"Python 语法不完整（第 {e.lineno} 行）：{e.msg}"
    return ""


class Api:
    def start_task(self, goal, engine='web'):
        threading.Thread(target=self._run_task, args=(goal, engine), daemon=True).start()
        return True

    def open_output(self):
        """打开产物目录（不直接运行产物）。"""
        work_dir = os.path.join(os.path.expanduser("~"), ".pm-studio", "work")
        os.makedirs(work_dir, exist_ok=True)
        try:
            os.startfile(work_dir)
        except Exception:
            pass
        return True

    def open_workdir(self):
        return self.open_output()

    def _find_python(self):
        """找真正能执行脚本的解释器。

        打包成 exe 后 `sys.executable` 指向 PM-Studio.exe 本身，直接拿来跑生成代码
        等于让程序再开一个自己（无限弹窗）。优先用 pm.config.json 里配的 python；
        其次才是 PATH 里的 python，但跳过 WindowsApps 的微软商店占位壳（exit=9009）。
        """
        try:
            cfg_py = load_config(os.path.join(base_dir, 'pm.config.json')).get('python') or ''
        except Exception:
            cfg_py = ''
        if cfg_py and os.path.isfile(cfg_py):
            return cfg_py
        if 'python' in os.path.basename(sys.executable).lower():
            return sys.executable
        import shutil
        cand = shutil.which('python')
        if cand and 'windowsapps' not in cand.lower():
            return cand
        return 'python'

    def _ask_web(self, goal, update_state):
        cfg = load_config(os.path.join(base_dir, 'pm.config.json'))
        adapter = DeepSeekAdapter(base_dir, cfg)
        update_state("ASKING_WEB_AI", "正在请求网页版 DeepSeek 生成代码...")
        reply = adapter.send_prompt(PROMPT_TMPL.format(goal=goal))
        text = getattr(reply, 'text', reply) or ''
        kind, code = extract_code(text)
        if not kind:
            # 网页渲染后没有围栏，从 <pre> 里取；类型按内容判断
            code = adapter.last_code() if hasattr(adapter, 'last_code') else ''
            kind, code = extract_code(code)
        return kind, code, text

    def _ask_opencode(self, goal, work_dir, update_state):
        cfg = load_config(os.path.join(base_dir, 'pm.config.json'))
        exe = (cfg.get('opencode') or {}).get('exe') or 'opencode'
        if not os.path.isfile(exe):
            raise RuntimeError(f"找不到 OpenCode CLI：{exe}")
        update_state("ASKING_OPENCODE", "正在交给本地 OpenCode 写代码（长代码不受网页字数限制）...")
        started = time.time()
        cmd = [exe, 'run', '--auto', '--format', 'json', PROMPT_TMPL.format(goal=goal)]
        proc = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = proc.stdout or ''
        texts = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('type') == 'text':
                part = obj.get('part') or obj
                if isinstance(part, dict) and part.get('text'):
                    texts.append(part['text'])
        text = "\n".join(texts)
        kind, code = extract_code(text)

        # OpenCode 是 agent，可能直接落盘而不吐代码块：那就认它写出来的文件
        if not kind:
            arts = []
            for name in os.listdir(work_dir):
                p = os.path.join(work_dir, name)
                if os.path.isfile(p) and name.lower().endswith(('.html', '.htm', '.py')):
                    if os.path.getmtime(p) >= started - 2:
                        arts.append((os.path.getmtime(p), p))
            if arts:
                arts.sort(reverse=True)
                latest = arts[0][1]
                kind = 'html' if latest.lower().endswith(('.html', '.htm')) else 'python'
                with open(latest, encoding='utf-8', errors='replace') as f:
                    code = f.read()
                text = (text or '') + f"\n\n[artifact] {latest}"
        if not text and not kind:
            raise RuntimeError(f"OpenCode 没有返回文本（exit={proc.returncode}）：{(proc.stderr or '')[-300:]}")
        return kind, code, text

    def _run_task(self, goal, engine='web'):
        state_file = os.path.join(get_base_path(), 'dashboard_app', 'state.json')
        work_dir = os.path.join(os.path.expanduser("~"), ".pm-studio", "work")
        os.makedirs(work_dir, exist_ok=True)

        def update_state(state, msg, code=None):
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            data = {"current_state": state, "target": goal, "message": msg, "code": code}
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        try:
            if engine == 'oc':
                kind, code, text = self._ask_opencode(goal, work_dir, update_state)
            else:
                if not HAS_ADAPTER:
                    update_state("FAILED", "未找到网页AI适配器模块，请检查 lib 目录是否完整。")
                    return
                kind, code, text = self._ask_web(goal, update_state)

            if not kind:
                update_state("FAILED", "没识别出代码块（既不是 ```html 也不是 ```python）。", text)
                return

            # 强制拦截：残缺一律不落盘、不打开
            reason = validate_code(kind, code)
            if reason:
                update_state(
                    "FAILED",
                    "AI 输出被截断，已拦截（未保存、未执行）。"
                    "请把任务拆小，或把执行引擎切到「本地 OpenCode」。\n原因：" + reason,
                    text,
                )
                return

            if kind == 'html':
                out_path = os.path.join(work_dir, "app.html")
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(code)
                update_state("DONE", "HTML 生成完成，已在浏览器打开。", code + f"\n\n产物：{out_path}")
                try:
                    webbrowser.open("file:///" + out_path.replace("\\", "/"))
                except Exception:
                    pass
                return

            update_state("EXECUTING", "代码校验通过，正在本地执行...", code)
            script_path = os.path.join(work_dir, "script.py")
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(code)

            result = subprocess.run(
                [self._find_python(), script_path], capture_output=True, text=True,
                timeout=30, cwd=work_dir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            update_state(
                "DONE", "执行完成！",
                code + "\n\n=== 运行结果 ===\n" + output + f"\n\n产物目录：{work_dir}",
            )
        except Truncated as e:
            update_state("FAILED", f"AI 输出被截断，已拦截：{e}")
        except Exception as e:
            update_state("FAILED", f"执行发生异常: {str(e)}")

api = Api()

def start_server():
    dashboard_dir = os.path.join(get_base_path(), 'dashboard_app')
    os.makedirs(dashboard_dir, exist_ok=True)
    
    with open(os.path.join(dashboard_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(HTML_CONTENT)

    class DashboardHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=dashboard_dir, **kwargs)
        def log_message(self, format, *args): pass

    PORT = 8080
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("", PORT), DashboardHandler)
        httpd.serve_forever()
    except OSError:
        pass

def main():
    threading.Thread(target=start_server, daemon=True).start()
    time.sleep(0.5)
    window = webview.create_window(
        title='AI 项目经理 Studio',
        url='http://127.0.0.1:8080/index.html',
        width=1000, height=700,
        background_color='#1e1e1e',
        js_api=api
    )
    webview.start()

if __name__ == '__main__':
    main()
