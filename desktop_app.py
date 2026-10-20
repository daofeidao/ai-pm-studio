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
        <input id="task-input" type="text" placeholder="输入你想让项目经理做的任务（例如：写一个贪吃蛇脚本）...">
        <button id="open-btn" style="background:#3a3a3a;">查看产物</button>
        <button id="start-btn">开始执行</button>
    </div>
    <script>
        document.getElementById('open-btn').onclick = function() {
            if(window.pywebview && window.pywebview.api) window.pywebview.api.open_output();
        };
        document.getElementById('start-btn').onclick = function() {
            var val = document.getElementById('task-input').value;
            if(!val) return;
            if(window.pywebview && window.pywebview.api) {
                document.getElementById('log-output').innerText = '任务已下发，正在请求网页 AI...';
                window.pywebview.api.start_task(val);
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

class Api:
    def start_task(self, goal):
        threading.Thread(target=self._run_task, args=(goal,), daemon=True).start()
        return True

    def open_output(self):
        """打开产物：有 html 就丢给默认浏览器，否则打开 work 目录。"""
        work_dir = os.path.join(os.path.expanduser("~"), ".pm-studio", "work")
        os.makedirs(work_dir, exist_ok=True)
        try:
            htmls = sorted(
                (p for p in os.listdir(work_dir) if p.lower().endswith((".html", ".htm"))),
                key=lambda n: os.path.getmtime(os.path.join(work_dir, n)),
                reverse=True,
            )
            if htmls:
                webbrowser.open("file:///" + os.path.join(work_dir, htmls[0]).replace("\\", "/"))
            else:
                os.startfile(work_dir)
        except Exception:
            pass
        return True

    def open_workdir(self):
        """打开产物目录。"""
        work_dir = os.path.join(os.path.expanduser("~"), ".pm-studio", "work")
        os.makedirs(work_dir, exist_ok=True)
        try:
            os.startfile(work_dir)
        except Exception:
            pass
        return True

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

    def _run_task(self, goal):
        state_file = os.path.join(get_base_path(), 'dashboard_app', 'state.json')
        work_dir = os.path.join(os.path.expanduser("~"), ".pm-studio", "work")
        os.makedirs(work_dir, exist_ok=True)
        
        def update_state(state, msg, code=None):
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            data = {"current_state": state, "target": goal, "message": msg, "code": code}
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        update_state("ASKING_WEB_AI", "正在请求网页版 DeepSeek 生成代码...")

        if not HAS_ADAPTER:
            update_state("FAILED", "未找到网页AI适配器模块，请检查 lib 目录是否完整。")
            return

        try:
            cfg = load_config(os.path.join(base_dir, 'pm.config.json'))
            adapter = DeepSeekAdapter(base_dir, cfg)
            prompt = f"你是一个本地自动化助手。请根据用户任务编写一段Python脚本，必须用 ```python 和 ``` 包裹。要求：完整可运行，禁止用 ... 或「省略」缩写任何部分，不要输出多余解释。任务：{goal}"
            reply = adapter.send_prompt(prompt)
            reply_text = getattr(reply, 'text', reply)

            code_match = re.search(r'```python\s*\n(.*?)\n```', reply_text, re.DOTALL)
            if code_match:
                code = code_match.group(1)
            else:
                # 网页渲染后的 innerText 不含 ``` 围栏，改从 <pre> 里取代码
                code = adapter.last_code() if hasattr(adapter, 'last_code') else ''
                if not code:
                    update_state("FAILED", "DeepSeek 没有返回代码，或页面结构已改版。", reply_text)
                    return
            
            update_state("EXECUTING", "代码生成成功，正在本地沙盒执行...", code)
            script_path = os.path.join(work_dir, "script.py")
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(code)
            
            result = subprocess.run([self._find_python(), script_path], capture_output=True, text=True, timeout=30, cwd=work_dir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            warn = ""
            if re.search(r'^\s*\.\.\.\s*$', code, re.MULTILINE):
                warn = "\n\n[警告] 代码里含省略号 ...，说明 AI 偷懒没写全，产物可能不完整。重试一次或把任务拆小。"
            update_state("DONE", "执行完成！", code + "\n\n=== 运行结果 ===\n" + output + f"\n\n产物目录：{work_dir}" + warn)
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
