# opencode `--format json` 真实样本（T5）

> 目的：把**真实运行** `opencode run --format json` 得到的 stdout 贴进本文档，
> 并据此校正 dispatcher / `agent_provider.parse_opencode_json` 的解析策略。
> 采集脚本：`tools/run_opencode_sample.py`（可复现）。
> 采集日期：2026-10-20　主机：Windows（Git Bash + Python 3.13.12）

---

## 1. 采集命令

```bash
"C:/Users/Administrator/AppData/Local/Programs/@opencodedesktop/resources/opencode-cli.exe" \
  run --format json "echo smoke" --standalone --auto
```

等价于（脚本默认参数）：

```bash
python tools/run_opencode_sample.py --timeout 180
```

脚本用 `subprocess` + `communicate(timeout=)` 精确控时，超时经
`pm_common.kill_process_tree` 杀**整棵进程树**（Windows `taskkill /T /F`）——
`opencode-cli.exe` 会拉起 `OpenCode.exe` 子进程，只杀父进程会留下孤儿进程。

---

## 2. 真实 stdout（原样粘贴）

```text
```

**stdout 长度：0 字节（空）**

## 3. 真实 stderr（原样粘贴）

```text
```

**stderr 长度：0 字节（空）**

---

## 4. 采集结果元数据

`reports/t5/sample-result.json`（由脚本自动写入，原样）：

```json
{
  "ok": false,
  "reason": "超时（已 kill 进程树）",
  "argv": [
    "C:/Users/Administrator/AppData/Local/Programs/@opencodedesktop/resources/opencode-cli.exe",
    "run",
    "--format",
    "json",
    "echo smoke",
    "--standalone",
    "--auto"
  ],
  "started_at": "2026-10-20T08:20:57Z",
  "finished_at": "2026-10-20T08:23:57Z",
  "returncode": 1,
  "timed_out": true,
  "stdout_bytes": 0,
  "stderr_bytes": 0,
  "stdout_path": "reports/t5/opencode-sample-stdout.txt",
  "stderr_path": "reports/t5/opencode-sample-stderr.txt",
  "stdout_preview": "",
  "stderr_preview": ""
}
```

即：**跑满 180 秒被强制 kill，stdout/stderr 全程 0 字节，进程未自行退出。**

---

## 5. 根因定位（旁证命令与输出）

### 5.1 CLI 本身可正常工作

```bash
$ opencode-cli.exe --version
opencode v2.0.29
```

### 5.2 `serve` 子进程可正常启动（且**输出为单行 JSON**）

`run` 的实现是「spawn 一个 `serve --stdio --port 0` 子进程再与之通信」。
单独跑 `serve` 立刻拿到一行 JSON：

```bash
$ opencode-cli.exe serve --stdio --port 0
{"url":"http://127.0.0.1:61566"}
```

> **这是本次采集到的唯一一份「opencode 自己的 JSON 输出」**：
> 单行 JSON 对象、字段为 `url`。说明 opencode 系列的 JSON 输出**至少支持
> 「单行 JSON 对象」形态**。

### 5.3 缺少 provider 凭证（决定性证据）

```bash
$ opencode-cli.exe auth list
No authenticated integrations
```

### 5.4 opencode 自身日志（`~/.local/share/opencode/log/opencode.log`）

```text
timestamp=2026-10-20T08:20:57.907Z level=INFO run=dc9687c1 message="cli starting" version=2.0.29 channel=latest local=false args="[\"run\",\"--format\",\"json\",\"echo smoke\",\"--standalone\",\"--auto\"]" role=cli
timestamp=2026-10-20T08:20:57.931Z level=INFO run=dc9687c1 message="spawning process" command="...\\opencode-cli.exe" args="[\"serve\",\"--stdio\",\"--port\",\"0\"]" cwd="...\\pm-software" role=cli
```

日志止步于「spawning process」——CLI 拉起 serve 后**一直等不到 provider 响应**，
既不报错也不退出。这与 5.3 的「无已认证集成」一致。

### 5.5 结论

> **本次真实采集未能拿到 `run --format json` 的有效 stdout。**
> 原因是**环境缺少已认证的 provider 集成**（`auth list` 为空），
> CLI 因此无限等待而非快速失败。
> 这是**环境/凭证问题，不是解析问题**；但它暴露了一个真实缺陷：
> **`run` 在无凭证时不快速失败，而是静默挂起**——编排器只能靠超时兜底。

---

## 6. 解析策略（据此校正）

### 6.1 校正点 1：按行/流式解析，**累加** text 片段（已实施）

原实现：整串失败后按行解析，取**最后一个**含 `text` 的对象。
问题：流式输出会把一段回答拆成**多个** `{"type":"text",...}` 行，
只取最后一行会**丢内容**。

现实现（`lib/agent_provider.py::parse_opencode_json`）：

1. 整串 `json.loads` 成功 -> 直接用（兼容单 JSON 对象，形态同 5.2 的 `serve` 输出）；
2. 否则**逐行** `json.loads`，**按顺序累加**所有含 text 的片段，
   并取最后一个出现的 `sessionID`；
3. 全行不可解析 -> `E_OPENCODE_BAD_JSON=43`；
4. 可解析但取不到 text -> `E_AGENT_RESULT_INVALID=41`；
5. **stdout 为空** -> `E_OPENCODE_BAD_JSON=43`（此前会走到「整串与按行均失败」，
   现在给出更准确的「输出为空」语义）。

对应自检（`dispatcher.py --selftest`，已全绿）：

```text
[OK] NDJSON 流式解析：多段 text 全部累加
[OK] NDJSON 流式解析：sessionID 提取
[OK] 单 JSON 对象解析正常
[OK] 空 stdout -> E_OPENCODE_BAD_JSON
```

### 6.2 校正点 2：空 stdout 显式化（已实施）

`E_OPENCODE_BAD_JSON=43` 的 message 现在明确区分「输出为空」与「无法解析」，
便于排障时一眼区分「代理没说话」与「代理说了但格式不对」。

### 6.3 校正点 3：超时语义（已实施，配合 T3）

`OpenCodeProvider.run` 在 `run_cli` 超时时返回
`E_AGENT_TIMEOUT=42`（`error_detail` 含超时秒数），进程树已被 kill。
本次采集印证了「靠超时兜底」是**唯一**的失败出口——**这是缺陷**，
待有凭证后复采以确认「有凭证时是否快速失败」。

---

## 7. 🔴 待确认（标红项）

以下结论**尚无真实样本支撑**，属推测，需在**具备已认证 provider** 的环境复采后确认：

| # | 待确认项 | 当前状态 | 复采方法 |
|---|----------|----------|----------|
| 1 | 🔴 `run --format json` 的**成功**输出形态（单 JSON 对象 or NDJSON 流） | **未观测到**（stdout 为空） | 登录 provider 后重跑 `tools/run_opencode_sample.py` |
| 2 | 🔴 成功输出中 `text` 字段的**确切路径**（顶层 `text` / `message.content` / `parts[]`） | **未观测到**；现按多候选探测（`text` / `message.content` / `content` / `parts`） | 同上 |
| 3 | 🔴 `sessionID` 字段名与位置 | **未观测到**；现按 `sessionID` / `session_id` / `sessionId` 三候选 | 同上 |
| 4 | 🔴 无凭证时是否**应当**快速失败（而非静默挂起 180s） | 观测到的是**静默挂起**；是否 upstream 缺陷待确认 | 复采 + 查 opencode issue |
| 5 | 🟡 tokens / cost 字段结构 | 仅依据 v2 架构文档与 demo 夹具，**无真实样本** | 同上 |

> **在 #1–#3 复采确认前**，`demo_fixtures.DEMO_OPENCODE_JSON` 仍是唯一的
> 格式依据（它描述的是 NDJSON 流式形态）。
> 解析器已按**两种形态都兼容**的方式实现（整串 + 逐行累加），
> 因此无论真实形态是哪一种，都不会解析失败——但**字段提取的准确路径未经验证**。

---

## 8. 复采指引（给后续执行者）

```bash
# 1) 确认有凭证（必须先登录 provider 集成）
opencode-cli.exe auth list          # 期望：列出已认证集成，而非 "No authenticated integrations"

# 2) 采集真实样本
python tools/run_opencode_sample.py --timeout 180

# 3) 看结论
cat reports/t5/sample-result.json
cat reports/t5/opencode-sample-stdout.txt
```

采集到非空 stdout 后，请：
1. 把真实 stdout 贴进本文档 §2；
2. 按真实形态核对 §7 的 #1–#3，**把 🔴 改为 ✅**；
3. 若真实形态与现解析器假设不符，修正 `lib/agent_provider.py::parse_opencode_json`
   并同步 `dispatcher.py --selftest` 的 NDJSON 断言。

---

## 9. 相关文件

| 文件 | 作用 |
|------|------|
| `tools/run_opencode_sample.py` | 采集脚本（超时精确控时 + 杀进程树 + 元数据落盘） |
| `reports/t5/sample-result.json` | 本次采集的元数据（自动生成） |
| `reports/t5/opencode-sample-stdout.txt` | 本次采集的真实 stdout（**空**） |
| `reports/t5/opencode-sample-stderr.txt` | 本次采集的真实 stderr（**空**） |
| `lib/agent_provider.py::parse_opencode_json` | 解析器（整串 + 逐行累加） |
| `lib/demo_fixtures.py::DEMO_OPENCODE_JSON` | demo 夹具（NDJSON 流式形态样例） |
| `.workbuddy/skills/dispatch-agent/dispatcher.py` | NDJSON 解析自检 |
