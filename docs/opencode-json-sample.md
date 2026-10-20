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

## 6. 解析策略（v2.1 按真实输出校正）

### 6.1 校正点 1：严格按事件流解析（已实施）

真实形态（2026-10-20 实测确认）：**NDJSON 流**，每行一个独立 JSON 事件，
顶层有 `type`，payload 在 `part` 里 —— 四类事件 `step_start` / `tool_use` /
`step_finish` / `text`。实测事件顺序：
`step_start -> tool_use -> step_finish -> text`，即 **`step_finish` 之后还有 text**。

现实现（`lib/agent_provider.py::parse_opencode_json`）：

1. **逐行** `json.loads`，无法解析的行跳过；读到 **stdout EOF** 才算结束；
2. `step_finish` **不是完成信号**，绝不提前返回（否则会丢掉其后的 text）；
3. 文本 = 所有 `type=="text"` 事件的 `part.text`，按顺序拼接；
4. 工具调用 = 所有 `type=="tool_use"` 事件的 `part.tool` + `part.state`，
   `part.state.status != "completed"` 记为失败（`ok=False`）；
5. token / 成本 = 所有 `type=="step_finish"` 的 `part.tokens`（逐键累加）与
   `part.cost`（累加），由 `OpenCodeProvider._record_usage` 写入 `activity.jsonl`
   （`kind="agent_usage"`），作为**预算熔断主数据源**（`QuotaService._from_activity`）；
6. 一个可解析事件都没有 -> `E_OPENCODE_BAD_JSON=43`；
   读到 EOF 仍无 text -> `E_AGENT_RESULT_INVALID=41`。

回放夹具：`reports/t5/sample-result.json` —— **原始 4 行 NDJSON 原样保存**
（1751 字节，LF 行尾，未做任何重排/补字段）。`python lib/agent_provider.py --selftest`
逐字校验：

```text
[OK] 夹具为 4 行且每行独立可解析
[OK] 回放：文本逐字一致
[OK] 回放：1 条 write 工具且 status=completed
[OK] 回放：工具调用 id 取自 part.id
[OK] 回放：token 累计 input=5145/output=44/reasoning=26
[OK] 回放：part.cost 的真实值 0 被正确累加
[OK] 回放：step_finish 之后的 text 仍被取到（EOF 才是结束）
```

> 注意两点实测细节：
> 1. 真实回复文本**带反引号**：`OK\n\n已写入 \`C:\temp\octest\a.txt\`，内容为 \`hello\`。`
>    （此前口述版本漏了反引号；断言以**原始字节**为准）。
> 2. `part.tokens` 里的 `cache.read/write` 是**嵌套对象**，当前只累加顶层数值键
>    （input/output/reasoning）；cache 用量暂不计入 `tokens` 汇总，预算熔断用 `cost`。

### 6.1b 校正点 1b：collect 阶段无条件 porcelain 检查（已实施）

实测确认：**不传 `--auto` 时 write 工具照样执行**（`status=completed` 并真实建文件），
所以 `--auto` / 权限位都不能当安全前提。编排器现在在**每个成功轮次进入 COLLECTING
之前无条件**跑 main 工作区 `git status --porcelain`（`_collect_escape_check`），
只报相对派发前基线**新增**、且不在编排器记账目录里的改动，命中即 `security_violation` -> PAUSE。

### 6.1c 结论：`step_finish.part.tokens` 是**本 step 增量**，不是会话累计

**结论：增量**。因此 `parse_opencode_json` 对多个 `step_finish` **逐键累加是正确的**，
不会重复计算；不需要改成「取最后一个」或「取差分」。

证据（本机 opencode 会话库 `~/.local/share/opencode/opencode.db`，
会话 `ses_ea70675b5ffeAMM5VmhMEBdAAt` / 目录 `C:/temp/octest`，本次实测同一会话）：

| 事件 | step | `part.tokens.input` | `part.tokens.output` | `part.tokens.reasoning` |
|------|------|--------------------|----------------------|-------------------------|
| 第 1 个 `step_finish`（工具调用步） | step 1 | 5145 | 44 | 26 |
| 第 2 个 `step_finish`（收尾文本步） | step 2 | 372 | 22 | 0 |

若为**累计值**，第 2 步的 `input` 必须 ≥ 5145；实测只有 372，
说明每个 `step_finish` 携带的是**该次 LLM 调用自身的用量**。
累加后得到会话总用量（5145+372=5517，与 provider 侧计费口径的差异来自缓存/系统提示计数，不影响「逐 step 增量」这一结论）。

### 6.1d 事件类型只认顶层 `type`（part.type 命名不一致）

真实样本里两处命名**不一致**，且不可混用：

| 位置 | step_finish | tool_use |
|------|-------------|----------|
| 顶层 `type` | `step_finish`（下划线） | `tool_use`（下划线） |
| `part.type` | `step-finish`（连字符） | `tool`（单数） |

`lib/agent_provider.py::parse_opencode_json` **只用顶层 `type` 分发**，从不读 `part.type`；
`part` 只作为 payload 容器（`text` / `tool` / `state` / `tokens` / `cost`）。
自检里加了反证用例：故意让 `part.type` 与顶层 `type` 矛盾（如 `type=text` 而
`part.type=step-finish`），解析结果必须只按顶层类型生效。

### 6.2 校正点 2：空 stdout 显式化（已实施）

`E_OPENCODE_BAD_JSON=43` 的 message 明确区分「输出为空」与「无法解析」，
便于排障时一眼区分「代理没说话」与「代理说了但格式不对」。

### 6.3 校正点 3：超时语义（已实施，配合 T3）

`OpenCodeProvider.run` 在 `run_cli` 超时时返回
`E_AGENT_TIMEOUT=42`（`error_detail` 含超时秒数），进程树已被 kill。
本次采集印证了「靠超时兜底」是**唯一**的失败出口——**这是缺陷**，
待有凭证后复采以确认「有凭证时是否快速失败」。

---

## 7. 待确认项（v2.1 更新）

2026-10-20 实测已确认输出形态，原 🔴 项降级为 ✅：

| # | 项 | 当前状态 |
|---|----|----------|
| 1 | `run --format json` 成功输出形态 | ✅ **NDJSON 流**（每行一个 JSON 事件，EOF 结束） |
| 2 | text 字段路径 | ✅ `type=="text"` 事件的 `part.text` |
| 3 | sessionID 字段 | ✅ 顶层 `sessionID`（解析器仍兼容 `session_id` / `sessionId`） |
| 4 | 🟡 无凭证时是否快速失败 | 仍为静默挂起（靠 `timeout_s` 兜底），upstream 行为待确认 |
| 5 | tokens / cost 结构 | ✅ `step_finish.part.tokens`（input/output/reasoning）+ `part.cost` |
| 6 | 🟡 `step_finish.part.cost` 具体数值 | 夹具中置 `0`（未采集到该字段的真实值），累加逻辑另有用例覆盖 |

> `reports/t5/sample-result.json` 现为**回放夹具**（4 行 NDJSON：step_start / tool_use /
> step_finish / text），不再是采集元数据；采集脚本的元数据如需保留请另存文件名。

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
| `reports/t5/sample-result.json` | 4 行真实 NDJSON **回放夹具**（step_start / tool_use / step_finish / text） |
| `reports/t5/opencode-sample-stdout.txt` | 历史采集的真实 stdout（**空**，环境无凭证） |
| `reports/t5/opencode-sample-stderr.txt` | 历史采集的真实 stderr（**空**） |
| `lib/agent_provider.py::parse_opencode_json` | 解析器（NDJSON 事件流：text 累加 / tool_use / tokens+cost） |
| `lib/agent_provider.py::OpenCodeProvider._record_usage` | 把 token/cost 写入 `activity.jsonl`（预算熔断数据源） |
| `lib/quota.py::QuotaService._from_activity` | 额度主源：activity 累计成本 -> percent |
| `lib/demo_fixtures.py::DEMO_OPENCODE_JSON` | demo 夹具（NDJSON part 形态样例） |
| `.workbuddy/skills/dispatch-agent/dispatcher.py` | NDJSON 解析自检 + 真实样本回放断言 |
