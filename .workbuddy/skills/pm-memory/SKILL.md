# pm-memory（项目级 skill）

> 阈值压缩（规则而非 AI 自总结）+ 固定格式记忆包注入。
> 对应架构：ARCHITECTURE-v2 §7 T05 / PRD P1-3 / US-5。

## 调用约定

```bash
# 自检（阈值边界 / 压缩 / anchors / 固定格式逐行比对 / 归档）
python .workbuddy/skills/pm-memory/memory.py --selftest

# 由 pm-orchestrator 在每轮 ASKING_WEB_AI 前调用 should_inject / compress / render_pack
```

## 规则

1. **阈值**：`usage_chars ≥ threshold_chars`（默认 24000 字符，按字符数不引 tokenizer）才注入。
2. **规则压缩**：按「决策/约束/偏好/进度」四桶关键词归行，超 `max_pack_items`（默认 5）截断；
   阻塞行单独归桶。压缩零依赖、零模型调用。
3. **anchors 固化**：`update_anchors(pack)` 原子写 `memory/anchors.json`（去重，上限 20 条/桶）。
4. **归档**：每轮问答写 `memory/transcripts/round-<n>-<ts>.md`。

## 固定注入格式（v2 §10.7，禁止增删行，恰好 6 行）

```
【项目记忆·自动生成】
目标：<一句话>
已完成：<分号分隔，≤3 条>
阻塞：<无则写"无">
【当前问题】
<本轮问题全文>
```

## 错误码

本模块不中断主流程；文件损坏按空 Anchors 处理（容错优先）。
