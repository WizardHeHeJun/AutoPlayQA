---
name: babysit-run
description: 看护循环：无人值守跑含 agent 交接节点的长任务——后台起 run、轮询进度、遇 agent_required 自己动手做完那一步并归档、续跑到底，最后汇总所有分段的 findings 并给出"该固化哪些 agent 节点"的建议。当用户说"看护跑"、"babysit"、"这个任务盯着跑完"、"带 agent 节点的任务跑一遍"时使用。
---

# 看护循环（babysit-run）

目标：把一个**含 `agent` 交接节点**的任务从头跑到尾。引擎每遇到 `agent` 节点就挂起交回给你，你按指令做完那一步再续跑——本技能就是这个"跑—挂起—人工—续跑"循环的固定打法，外加**全程动作归档**与**跨分段的 findings 汇总**。

适用：长流程冒烟、任务里有动态手势/看情况决策的步骤。纯确定性任务不需要看护，直接 `run_task` 或 `start_task` 即可；多用例连跑用 `run_suite`（但 **`run_suite` 不会续跑 agent 用例**——含 agent 节点的任务只能靠本技能跑完整）。

## 1. 前置

- adb 不在 PATH：`$env:PATH = "$env:LOCALAPPDATA\Android\Sdk\platform-tools;$env:PATH"`
- 设备在线：`list_devices`（无线设备先 `connect_device`），记下 `device_id`。
- 任务存在：`list_tasks` 确认名字；`get_task(name)` 看一眼 `_step_outline`，**心里有数哪些节点是 `agent` 动作**（`action.type == "agent"`，`llm` 是兼容别名）——那就是你会被叫醒的地方。
- 向用户确认：跑哪个任务、跑在哪台设备、起始画面是否就位。

## 2. 起跑

```
start_task(device_id, name)          # 返回 {ok, run_id, status: "running"}
```

- **全局只允许一个后台 run**（引擎是单例）。已有活跃 run 时返回 `{ok: False, error: ...}`，错误里带着活跃的 `run_id` 和任务名——**别重试硬起**：先 `get_run_status(<那个 run_id>)` 看它是谁、还要多久，然后问用户是等它跑完还是这次不跑。
- 想同步跑短任务用 `run_task`，但看护循环一律用 `start_task`——同步调用期间你看不到进度也接不了交接。

## 3. 轮询

```
get_run_status(run_id)
```

- 纯内存读，不碰设备，随时可查；但**节奏按 10~30 秒一次**，1 秒一查只会刷屏、把上下文烧光。
- `status` 只有四种：`running` / `agent_required` / `done` / `error`（引擎原生的 `completed`/`failed` 在 MCP 层已映射成 `done`/`error`）。
- `running` 时字段：`current_node`、`steps`、`elapsed_s`、`recent_events`（引擎刚记的流程事件：节点识别、动作执行、弹窗消除、恢复）。用它**叙述进度**给用户，也用它区分"慢但在动"和"卡死"：`steps` 和 `current_node` 长时间不变 = 卡住，去看 `recent_events` 找原因。
- 离开 `running` 后附完整 `result`（形状同 `run_task`：`steps` / `findings` / `report` / `handoff`），`error` 时另有 `error` 消息。

> 每次 `start_task` / `run_task` 返回后 PostToolUse 钩子会自动跑一遍 smoke-report 分诊并把结果注入上下文——这是预期行为，不用管、也不用手动再跑。

## 4. 接交接（status = `agent_required`）

从 `result["handoff"]` 取 **`{node, instruction}`**（只有这两个字段），然后：

**a. 开归档** —— 让这一轮人工操作留痕，不是流程里的黑洞：

```
record_actions_start(device_id, kind="handoff", task=<任务名>, node=<node>, run_id=<run_id>)
```

返回 `{ok, device_id, session_dir, manifest_path, started_at, context, step_count}`；`ok: False` 说明这台设备已有活跃日志，先 `record_actions_stop` 再开。

**b. 做那一步** —— 只做 `instruction` 要求的事：

- **优先 `screenshot_marked(device_id)` → `click_index(device_id, index)`**。走索引点击，命中的元素（文本 + bounds）会自动进归档，日后才能反推出确定性节点；裸 `click(x, y)` 只在屏幕刚好被标注过时才捡得到元素，纯坐标点击等于把这一步的语义扔了。
- 索引只对生成它的那一帧有效——**画面一变就重新 `screenshot_marked`**。
- 需要确认某段文字在不在屏上用 `find_text(device_id, text)`（先看 `found` 再动手）；`swipe` / `input_text` / `press_key` 也都会进归档。

**c. 停归档**：

```
record_actions_stop(device_id)       # 返回 {ok, session_dir, step_count, steps, ...}
```

**d. 续跑**：

```
start_task(device_id, name, start_after=<node>)   # 拿到新的 run_id
```

`start_after` 的语义是**从该节点的 `next` 开始轮询**（该节点本身不重跑）。拿到新 `run_id` 后回到第 3 步继续轮询。一个任务里有几个 agent 节点，就会走几轮——**每一轮都是一段独立的 run，记下每段的 `run_id` / 起跑节点 / 结束状态**。

## 5. 汇总（done / error）

**关键事实：每段 run 的 findings 是各自独立的目录，`report.json` 各写一份。** 没有哪个文件会替你合并——汇总是本技能的活。

按分段顺序拼一份连续叙述，每段给出：

| 项 | 来源 |
| --- | --- |
| 段序 / `run_id` / `start_after` 起点 | 你自己记的 |
| 终态 `status` | `get_run_status` 的 `status` |
| 走了多少步、停在哪 | `steps` / `current_node` / `elapsed_s` |
| findings | `result["findings"]` |
| 证据目录 | `result["report"]["report_path"]`、`report["report_html_path"]`（给人看的 HTML）、`report["export_path"]`（若配了导出） |
| 交接归档 | 该段对应的 `session_dir` |

**findings 即使任务成功也必须完整呈现**——这是项目铁律：异常 = 测试发现，findings 才是这个工具的产出本体，不是流程噪音。每条 finding 连同它的 `log_excerpt` / `recent_flow` 一起讲清楚，不要只报个数字。

`error` 段要说清是**引擎判失败**（`result` 里有 `error` 和 findings）还是**编排异常**（只有顶层 `error` 消息），并交代后续分段是否还跑了。

## 6. 收尾：沉淀检查（每次看护跑完都做）

```powershell
<python> -c "from task.handoff_stats import scan_handoffs, format_handoff_report; print(format_handoff_report(scan_handoffs(task_name='<任务名>')))"
```

（交互式 CLI 里等价命令：`task handoffs <任务名> [--days N]`。`<python>` 指**项目环境**的解释器——该命令 import 项目代码，须从项目根目录、用装好 requirements.txt 的 Python 环境运行（环境搭建见 README「环境」节；本仓库开发机的解释器路径记在本地 CLAUDE.md，不入库）。）

报告按 (任务, 节点) 聚合历史交接：`sessions`（交接次数）、`signatures`（不同动作签名数）、`dominant_ratio`（最常见签名占比）、`dominant_signature`（那串 `[动作类型, 元素文本]`）、`solidify_candidate`。行首 `->` 就是候选（≥3 次交接且 ≥80% 走同一签名）。

**有 `solidify_candidate` 的节点，要向用户明确建议固化**，措辞照实说：这个 agent 节点每次交接都在做同样的事，它不是"没法确定性化"，只是**还没人把它写成节点**。两条固化路径：

- 拿该节点的一份 handoff `session.json`，走 `explore-task` 技能的草稿转换路径（`task_editor.action_log_to_draft`）出节点，再补 QA 断言；
- 或直接照 `dominant_signature` 手写节点——签名里的元素文本就是现成的识别锚点，动作写 `{"type": "click", "target": "recognized"}`。

这是项目方向：**agent 节点应该逐渐消失**，人工回合才是回放里最贵的一段。

## 7. 安全约定

- **交接步只做 `instruction` 要求的事**，不即兴探索、不顺手点别的——你多点的每一下都会进归档，污染签名统计，也可能把游戏带到任务预期之外的状态。
- **交接中撞见游戏异常（报错弹窗、黑屏、卡死、明显不对的数值）不要绕过**：先 `screenshot` 留证（归档目录里也有每步动作前的帧），把现象、时间点、截图路径**如实写进最终汇总**，和引擎自己记的 findings 并列呈现。这与"绕过噪音继续跑"是两回事——异常就是我们要找的东西。
- 一台设备同时只有一份动作日志、全局同时只有一个后台 run；任何"已被占用"的报错都先查清占用者，别靠重试撞开。
- 不要为了"让它跑过"去改任务 JSON——加固任务是 `author-task` 的活，看护跑期间只观察和上报。

## 自检清单

- [ ] 每段 run 的 `run_id` / 起点 / 终态都记下了，汇总是**连续叙述**而不是只报最后一段？
- [ ] 每次交接都开了 `record_actions_start(kind="handoff", task=, node=, run_id=)` 并正常 `record_actions_stop`？
- [ ] 交接操作走的是 `screenshot_marked` + `click_index`，没有无谓的裸坐标点击？
- [ ] 续跑用的是 `start_task(device_id, name, start_after=<handoff.node>)`，节点名与 `handoff["node"]` 逐字一致？
- [ ] **所有分段的 findings 都汇总呈现了**（任务成功也要报）？证据路径给全（report.json / report.html / export zip）？
- [ ] 跑了 `handoff_stats` 沉淀检查，`solidify_candidate` 节点已向用户提出固化建议？
- [ ] 交接中观察到的游戏异常写进汇总了，没有被当噪音吞掉？
