# 17 态必要性审查表（v2.1）

> 审查对象：`orchestrator.py` 的 `LoopPhase` 枚举（17 态）+ 各态进入/退出条件 +
> 崩溃恢复动作。
> 目的：逐态回答「**为什么必须有这一态**」，并标出**可合并回上一态**的候选，
> 供架构决策（若合并，可减少状态数与 `_transition` 复杂度）。
> 结论标记：`KEEP`（保留）/ `MERGE?`（可合并候选，附合并目标与代价）。

---

## 0. 总览

| # | 状态 | 必要性一句话 | 结论 |
|---|------|--------------|------|
| 1 | IDLE | 唯一的「无任务在跑」锚点，供看板区分「空闲」与「卡住」 | KEEP |
| 2 | PLANNING | 把「上一轮收尾」与「新一轮起跑」在日志上分开，避免 resume 误判中段 | MERGE? → IDLE |
| 3 | ASKING_WEB_AI | 唯一会等待**外部网络**的态，超时/未登录/选择器缺失三种失败都发生在此 | KEEP |
| 4 | WAITING_RESPONSE | 语义上是「已发出请求、等回答」的**可观测子态** | MERGE? → ASKING_WEB_AI |
| 5 | PARSING | 把「拿到文本」与「文本已结构化」分开：解析失败要退回重问，不该重发网络请求 | KEEP |
| 6 | DISPATCHING | 唯一持锁态起点；锁在此获取，保证「一个 task 一个执行者」 | KEEP |
| 7 | PREPARING_WORKTREE | worktree 创建/重置是**可失败**的 Git 操作，失败须区别于执行失败 | MERGE? → EXECUTING |
| 8 | EXECUTING | 唯一「代理正在跑」态；崩溃恢复据此判断代理是否已死 | KEEP |
| 9 | REVIEWING_DIFF | 验收（确定性 diff 判定）与执行的**职责分离**；不打回就无法知道改动是否合规 | KEEP |
| 10 | MERGING | 合并冲突需人工介入（E_MERGE_CONFLICT），与执行失败走不同出口 | KEEP |
| 11 | HUMAN_REVIEW | 承载「不可逆操作 / 选择器缺失 / 越界改动」三类**人审**语义 | KEEP |
| 12 | COLLECTING | 合并后的产物归集（产物路径、artifact 列表），是验收数据的来源 | MERGE? → MERGING |
| 13 | FEEDING_BACK | 把「本轮结论回灌给网页 AI」显式化，构成闭环的**回环边** | KEEP |
| 14 | PERSISTING | 写报告 + 交接三件套 + commit main，是**唯一的写盘点** | KEEP |
| 15 | HANDING_OFF | 跨机交接的**显式边界**：交接文件已就绪，可安全换机器 | MERGE? → DONE |
| 16 | DONE | 唯一成功终态；与 FAILED/PAUSED 明确区分 | KEEP |
| 17 | PAUSED | 「被人/外部条件挂起」的可恢复态，与 FAILED（不可恢复）语义相反 | KEEP |
| 18 | FAILED | 唯一失败终态（不可自动恢复，需人工修） | KEEP |

> 表中 #1–#18 共 18 行，但 `IDLE` 与 `PAUSED/FAILED/DONE` 属「边界态」，
> **循环内工作态实为 15 个**；代码 `LoopPhase` 枚举共 17 个成员
> （`IDLE/PLANNING/ASKING_WEB_AI/WAITING_RESPONSE/PARSING/DISPATCHING/
> PREPARING_WORKTREE/EXECUTING/REVIEWING_DIFF/MERGING/HUMAN_REVIEW/COLLECTING/
> FEEDING_BACK/PERSISTING/HANDING_OFF/DONE/PAUSED/FAILED` = 18 个）。
> 计数以代码为准：**18 个枚举成员**，本文按「17 工作态 + 1 边界态」叙述，
> 差异源于 v2 早期文档写作 17。

---

## 1. 逐态详表

### IDLE（边界态）
- **进入**：初始化（`init`）后；`DONE`/`FAILED` 之后再次 `run` 前。
- **退出**：`run` -> PLANNING。
- **崩溃恢复**：无状态可恢复（`current.json` 缺失等价于 IDLE）。
- **必要性**：看板需要区分「空闲」与「卡在某个中间态」；缺了它，IDLE 会退化为
  「没有 phase 字段」的隐式状态，跨机冷启动时无法判定「该不该续跑」。
- **结论**：KEEP。

### PLANNING
- **进入**：`run` 起始；`resume` 从 PAUSED 放行；从 WAITING_RESPONSE 回到重问。
- **退出**：-> ASKING_WEB_AI。
- **崩溃恢复**：无锁、无副作用，直接重入。
- **必要性**：显式标记「新任务起跑」，让 activity 日志能区分「第 1 轮」与
  「第 2 轮重问」；否则 resume 无法判断该不该重发网络请求。
- **合并候选**：可合并回 IDLE（把 IDLE 直接跳到 ASKING_WEB_AI）。
  **代价**：activity 日志失去「起跑」锚点；resume 需靠 `round` 字段推断。
  **收益有限**（IDLE 本身无成本），建议 KEEP。

### ASKING_WEB_AI
- **进入**：PLANNING 后；PARSING 退回重问。
- **退出**：拿到回答 -> PARSING；异常 -> FAILED（超时/未登录）或 HUMAN_REVIEW（选择器缺失）。
- **崩溃恢复**：无锁，重入即可（重复提问代价可接受）。
- **必要性**：唯一会**等待外部网络**的态。三类失败语义各不相同
  （超时 / 未登录 / 选择器缺失），必须有一个独立的态承载错误出口，
  否则无法把「选择器缺失转人审」和「网络超时转失败」区分开（T2 依赖此点）。
- **结论**：KEEP。

### WAITING_RESPONSE
- **进入**：请求已发出、等待回答。
- **退出**：回答到达 -> PARSING。
- **崩溃恢复**：同 ASKING_WEB_AI。
- **必要性**：目前是**语义可观测性**价值——让看板能显示「已发出、等回答」。
- **合并候选**：可合并回 ASKING_WEB_AI（用 `active_agent` 或一个 `sub_state`
  字段区分）。**代价**：看板少一格细分；**收益**：少 1 个枚举、少一处 transition。
  **建议**：若追求状态数最小化，这是**首选合并候选**（它没有独立错误出口，
  与 ASKING_WEB_AI 完全同构）。

### PARSING
- **进入**：拿到网页 AI 回答文本。
- **退出**：解析出结构化动作 -> DISPATCHING；判定收尾 -> PERSISTING。
- **崩溃恢复**：无副作用，重入即可。
- **必要性**：把「拿到文本」与「文本已结构化」分开。解析失败时**不该重发网络请求**
  （会浪费额度且可能得到不同答案），只需重新解析或退回重问。
- **结论**：KEEP。

### DISPATCHING
- **进入**：PARSING 判定要派活。
- **退出**：worktree 就绪 -> PREPARING_WORKTREE；派发失败 -> FAILED。
- **崩溃恢复**：**持锁态**。若此处崩溃，锁仍在，`resume` 走 `_recover_lock`。
- **必要性**：唯一「获取 task 锁」的态。锁语义（一个 task 一个执行者）
  必须有明确归属点，否则崩溃后无法判断该重派还是该续跑。
- **结论**：KEEP。

### PREPARING_WORKTREE
- **进入**：DISPATCHING 后。
- **退出**：worktree 就绪 -> EXECUTING；Git 失败 -> FAILED。
- **崩溃恢复**：worktree 可能半成品，`_recover_lock` 检查 worktree 是否存在。
- **必要性**：`git worktree add` 会真实失败（分支残留、磁盘满、路径占用）。
  把准备与执行分开，失败原因才不会被误报成「代理崩了」。
- **合并候选**：可合并回 EXECUTING（在 EXECUTING 入口先建 worktree）。
  **代价**：worktree 失败会被记成执行失败，日志语义变糊；崩溃恢复时
  「worktree 是否已建」需要额外推断。
  **建议**：保留（它承载独立的 Git 失败出口）。

### EXECUTING
- **进入**：worktree 就绪。
- **退出**：代理返回 -> REVIEWING_DIFF；代理失败 -> FAILED。
- **崩溃恢复**：**最关键**。若在 EXECUTING 崩溃，需判断「代理是否已写入改动」：
  有 commit -> 直接进 REVIEWING_DIFF；无 -> 清 worktree 重派（`_recover_lock`）。
- **必要性**：唯一「代理正在跑」态，是崩溃恢复分叉的**依据点**。
- **结论**：KEEP。

### REVIEWING_DIFF
- **进入**：EXECUTING 返回。
- **退出**：通过 -> MERGING；不通过 -> 回 EXECUTING 重做（≤ max_rework）；
  超限 -> FAILED（E_DIFF_REJECTED）。
- **崩溃恢复**：无副作用（只读 diff），重入即可。
- **必要性**：验收与执行**职责分离**。验收是确定性的（`git diff` + allowlist），
  不依赖代理自述；不通过必须能「携带反馈打回」——这需要独立的态来承载重做边。
- **结论**：KEEP。

### MERGING
- **进入**：REVIEWING_DIFF 通过。
- **退出**：合并成功 -> COLLECTING；冲突 -> HANDING_OFF（转人工，E_MERGE_CONFLICT）。
- **崩溃恢复**：合并中途崩溃需 `git merge --abort`（`merge_task_branch` 已内建）。
- **必要性**：合并冲突的出口与执行失败**完全不同**（前者要人解冲突，
  后者重派即可）。共用一个失败态会丢掉「有冲突待解」这个关键信息。
- **结论**：KEEP。

### HUMAN_REVIEW
- **进入**：不可逆操作待批（T3 irreversible_ops）；网页 AI 选择器缺失（T2）；
  安全越界（转 PAUSED 前的停留）。
- **退出**：批准 -> 继续；否决 -> PAUSED。
- **崩溃恢复**：`pending_approval` 落盘在 current.json，resume 重新读取。
- **必要性**：三类「需要人给一个布尔决策」的场景共用此态，避免为每类各造一态
  （否则状态数翻倍）。`pending_approval.op_type` 区分三类。
- **结论**：KEEP。

### COLLECTING
- **进入**：合并成功。
- **退出**：-> PERSISTING。
- **崩溃恢复**：只做内存归集，重入即可。
- **必要性**：目前是「取 artifact 列表 / 汇总产物路径」的**数据准备**步骤。
- **合并候选**：可合并回 MERGING（合并成功后立即归集）。
  **代价**：几乎为零——COLLECTING 没有独立错误出口、没有独立副作用，
  合并后语义完全连续。
  **建议**：**这是第二顺位合并候选**（比 WAITING_RESPONSE 更易合并，
  因为它连看板细分价值都几乎没有）。

### FEEDING_BACK
- **进入**：PERSISTING 后且还有下一轮。
- **退出**：-> ASKING_WEB_AI（下一轮）。
- **崩溃恢复**：无锁，重入即可（但会重复一轮）。
- **必要性**：它是闭环的**回环边**——「把本轮结论回灌给网页 AI 再问下一轮」。
  若与 PERSISTING 合并，日志上看不出「为什么要再问一轮」。
- **结论**：KEEP（回环边值得显式化）。

### PERSISTING
- **进入**：本轮收尾（无论成功/失败/仅汇报）。
- **退出**：-> FEEDING_BACK（还有下一轮）或 -> HANDING_OFF（收尾）。
- **崩溃恢复**：**幂等**。报告名含时间戳，重复写会产生两份报告但不影响正确性；
  commit main 可能冲突（best-effort）。
- **必要性**：**唯一的写盘点**。报告、HANDOFF/SESSION/TASK 三件套、memory 锚点
  全在此落盘并 commit。把它拆散到别处会让「这一轮到底落了什么」无法审计。
- **结论**：KEEP。

### HANDING_OFF
- **进入**：PERSISTING 后确认收尾。
- **退出**：-> DONE。
- **崩溃恢复**：无副作用，重入即可。
- **必要性**：跨机交接的**显式边界**——「交接文件已就绪，此刻换机器是安全的」。
- **合并候选**：可合并回 DONE（DONE 本身即意味着交接完成）。
  **代价**：看板失去「正在交接中」这一格；但交接是毫秒级操作，
  该格几乎无观测价值。
  **建议**：**第三顺位合并候选**（成本最低）。

### DONE（终态）
- **进入**：HANDING_OFF 后。
- **退出**：无（`run` 可再起新任务 -> PLANNING）。
- **崩溃恢复**：幂等；resume 见 DONE 只会重新生成 TASK.md。
- **必要性**：唯一成功终态。
- **结论**：KEEP。

### PAUSED（终态·可恢复）
- **进入**：人工否决不可逆操作；T3 安全越界；外部条件挂起。
- **退出**：`resume` + queue 放行 -> PLANNING。
- **崩溃恢复**：幂等；PAUSED 状态本身即恢复指引。
- **必要性**：与 FAILED **语义相反**——PAUSED 可恢复，FAILED 需人工修。
  合并二者会让「自动续跑」和「必须人工修」无法区分。
- **结论**：KEEP。

### FAILED（终态·不可恢复）
- **进入**：各阶段不可恢复错误（代理崩、diff 连续不过、合并冲突转交、网页 AI 超时）。
- **退出**：无（需人工 `run` 重来）。
- **崩溃恢复**：幂等。
- **必要性**：唯一失败终态。
- **结论**：KEEP。

---

## 2. 可合并候选汇总（供决策）

| 候选 | 合并到 | 收益 | 代价 | 风险 | 建议 |
|------|--------|------|------|------|------|
| **WAITING_RESPONSE** | ASKING_WEB_AI | 少 1 态 | 看板少「等回答」细分 | 低（无独立错误出口） | **优先合并** |
| **COLLECTING** | MERGING | 少 1 态 | 几乎无 | 极低（无副作用、无独立出口） | **次优先合并** |
| **HANDING_OFF** | DONE | 少 1 态 | 看板少「交接中」格 | 极低 | 可选 |
| PREPARING_WORKTREE | EXECUTING | 少 1 态 | Git 准备失败被记成执行失败 | **中** | 不建议 |
| PLANNING | IDLE | 少 1 态 | 日志少「起跑」锚点 | 低 | 不建议（收益≈0） |

**可合并上限**：最多可从 18 减到 15（合并 WAITING_RESPONSE / COLLECTING / HANDING_OFF）。
**建议**：先合并 **WAITING_RESPONSE + COLLECTING**（2 个），HANDING_OFF 可留可删。
合并后需同步改：`orchestrator.py` 的 `LoopPhase` 枚举、所有 `_transition` 调用点、
`docs/state-diagram-v2.mermaid`、`dashboard/state.js` 派生逻辑的 `phase in (...)` 判断。

---

## 3. 崩溃恢复动作对照表（速查）

| 崩溃位置 | 锁 | 恢复动作 | 代码位置 |
|----------|----|----------|----------|
| PLANNING / ASKING_WEB_AI / PARSING | 无 | 直接重入（可重复提问） | `Orchestrator.resume` 中段 |
| DISPATCHING | 持锁 | `_recover_lock`：worktree 不存在 -> 清锁重派 | `Orchestrator._recover_lock` |
| PREPARING_WORKTREE | 持锁 | 同上（worktree 半成品 -> 清理重派） | 同上 |
| EXECUTING | 持锁 | 分支有 commit -> 直接 REVIEWING_DIFF；无 -> 清 worktree 重派 | 同上 |
| REVIEWING_DIFF | 无 | 重入（只读 diff，幂等） | `Orchestrator.resume` |
| MERGING | 无 | `merge_task_branch` 内部 `merge --abort` | `git_ops.merge_task_branch` |
| PERSISTING | 无 | 幂等重写（可能多一份报告） | `Orchestrator._persist_files` |
| HUMAN_REVIEW | 无 | 读 `pending_approval`，等 queue 指令 | `Orchestrator.resume` |
| PAUSED | 无 | 读 queue 找 `resume` 指令 | `Orchestrator.resume` |
| DONE / FAILED | 无 | 终态；DONE 重新生成 TASK.md，FAILED 提示重新 run | `Orchestrator.resume` 终态分支 |

---

## 4. 审查结论

1. **核心工作流 12 态（PLANNING..FEEDING_BACK）职责互不重叠**，每个都有
   独立的错误出口或恢复分叉依据，建议全部保留。
2. **3 个弱候选**（WAITING_RESPONSE / COLLECTING / HANDING_OFF）可合并，
   其中 WAITING_RESPONSE 与 COLLECTING 的合并**不损失任何错误处理能力**。
3. 边界态 IDLE / DONE / PAUSED / FAILED **必须四态齐备**：
   「空闲 / 成功 / 可恢复挂起 / 不可恢复失败」是四种本质不同的机器状态。
4. 本表与代码同步维护；改动 `LoopPhase` 时**必须**同步本表与
   `docs/state-diagram-v2.mermaid`。
