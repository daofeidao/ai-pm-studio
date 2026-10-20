# TASK · task-002

> 由编排器据 HANDOFF.md + SESSION.md 最后 ↪ Return 生成于 2026-10-20T08:17:34Z

## O — Outcome（预期结果）
演示任务

## D — Data（输入）
- 交接摘要：HANDOFF.md（目标/约束/关键文件/待办/阻塞）
- 执行史：SESSION.md 最后 ↪ Return（2026-10-20T08:17:34Z · pm-orchestrator）
- 当前指针：pm/state:state/current.json（round=2, phase=PERSISTING）
- 记忆包：【项目记忆·自动生成】…（未超阈值则无）

## A — Action（动作清单）
1. 按交接摘要推进当前目标

## F — Format（交付格式）
- 改动只允许落在 work/ 下（allowed_paths=["work/"]）
- 完成后在 SESSION.md 由编排器代追加 ↪ Return 块
- 验收标准：构建退出码 0；git diff --name-only 全部 ∈ work/
