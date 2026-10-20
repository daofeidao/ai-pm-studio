# E2E 闭环演练 001

- 开始：2026-10-20T13:10:54Z
- 结束：2026-10-20T13:11:08Z（总耗时 13.5s）
- 结论：**PASS**（9/9 通过）
- 目标：写一个 hello.py，运行后打印 hello
- 任务 id：`task-e2e-001`

## 流转总览

| 跳 | 动作 | 实现 | 耗时 |
|----|------|------|------|
| ① | 网页 AI 拆指令 | 真实 DeepSeek（CDP） | 6.2s |
| ② | 派发 + worktree | 真实 dispatch-agent / git（执行 mock） | 0.6s |
| ③ | 结果 + 记忆包回传再问 | 真实 DeepSeek（CDP） | 6.3s |

## 1. 前置检查

```text
[preflight] CDP 可用（WS 探活 OK，port=9222）
```

## 2. 第一跳：网页 AI 规划

### 提问

```text
你是项目经理。请把下面的任务拆成一条严格 JSON 指令。
只输出 JSON，不要解释，不要 markdown 代码围栏。字段：
{"id":"task-e2e-001","instruction":"给执行端的完整指令","acceptance":["可验证的验收点"],"allowed_paths":["work/"]}
任务：写一个 hello.py，运行后打印 hello
```

### 回答（原文）

```text
{"id":"task-e2e-001","instruction":"在 work/ 目录下创建 hello.py，文件内容应使脚本运行时打印 hello；完成后运行 python work/hello.py 验证输出。","acceptance":["work/hello.py 文件存在","运行 python work/hello.py 后标准输出为 hello","仅修改 work/ 目录下的文件"],"allowed_paths":["work/"]}
```

### 解析出的指令

```json
{
  "id": "task-e2e-001",
  "instruction": "在 work/ 目录下创建 hello.py，文件内容应使脚本运行时打印 hello；完成后运行 python work/hello.py 验证输出。",
  "acceptance": [
    "work/hello.py 文件存在",
    "运行 python work/hello.py 后标准输出为 hello",
    "仅修改 work/ 目录下的文件"
  ],
  "allowed_paths": [
    "work/"
  ]
}
```

## 3. 第二跳：dispatch-agent 派发

- 阶段回调：`PREPARING_WORKTREE -> EXECUTING -> REVIEWING_DIFF`
- 返回状态：`success` / agent=`opencode`
- worktree：`C:\Users\ADMINI~1\AppData\Local\Temp\pm-e2e-loop-eztfqvro\worktrees\task-e2e-001`
- 任务分支：`task-e2e-001`（相对 main 新增 1 个提交）
- 产物：`work/hello.py`
- 运行验证：`python work/hello.py` -> `hello`

## 4. 记忆压缩与回传

- 触发判定：usage=159/100 -> should_inject=`True`
- transcript 归档：`memory/transcripts/round-1-20261020T131101Z.md`、`memory/transcripts/round-2-20261020T131108Z.md`

### 记忆包（注入内容，固定 6 行）

```text
【项目记忆·自动生成】
目标：写一个 hello.py，运行后打印 hello
已完成：worktree 已生成，分支 task-e2e-001；执行端产出 work/hello.py，运行输出 hello
阻塞：无
【当前问题】
执行结果已回传：已生成 work/hello.py 并提交到 task-e2e-001 分支；产物 work/hello.py；运行 `python work/hello.py` 输出：hello。请判断任务是否完成，并用不超过三行给出：1) 结论 2) 证据 3) 下一步建议。
```

### 第二问（记忆包 + 结果）

```text
执行结果已回传：已生成 work/hello.py 并提交到 task-e2e-001 分支；产物 work/hello.py；运行 `python work/hello.py` 输出：hello。请判断任务是否完成，并用不超过三行给出：1) 结论 2) 证据 3) 下一步建议。
```

### 回答（原文）

```text
结论：任务已完成。

证据：work/hello.py 已提交至 task-e2e-001 分支，运行 python work/hello.py 输出 hello。

下一步建议：验收后合并 task-e2e-001 并归档/关闭任务。
```

## 5. 验收点

| 点 | 断言 | 结果 |
|----|------|------|
| a | ask-web-ai 发出提问并取回非空回答 | PASS |
| a | 回答可解析为 JSON 指令（含 instruction） | PASS |
| b | dispatch 返回 success | PASS |
| b | worktree 目录真实生成 | PASS |
| b | 任务分支有提交（diff 非空） | PASS |
| b | 产物 work/hello.py 落盘且可运行 | PASS |
| c | 上下文达阈值触发记忆压缩 | PASS |
| c | 记忆包固定 6 行且含目标/已完成 | PASS |
| c | 回传后再次提问并取回非空回答 | PASS |

## 6. 备注与边界

- 沙箱仓库：`C:\Users\ADMINI~1\AppData\Local\Temp\pm-e2e-loop-eztfqvro`（临时目录，真实 git；报告生成后可删）
- 编码环节为 mock（HelloMockProvider）：只替换“写什么代码”，worktree 创建、任务分支 commit、diff 全为真实 git 操作
- 记忆压缩阈值在沙箱内临时降到 100 字符（默认 24000），以便在短会话里触发注入
- 报告生成时间：2026-10-20T13:11:08Z
