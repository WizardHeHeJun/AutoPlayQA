---
name: explore-task
description: 探索自录：你（智能体）亲自在真机上摸一遍流程，动作日志自动记录每一步点到的元素锚点，再把日志转成识别驱动的任务草稿。当用户说"你自己探一遍生成任务"、"探索录制"、"explore record"、"你摸一遍这个流程写成任务"时使用。
---

# 探索自录（explore-task）

目标：你自己驱动设备走一遍流程，`record_actions_start` 全程记下每一步**点到的元素**（不只是坐标），停录后把日志确定性地转成任务草稿，再收口给 `author-task` 补断言、回放加固。

三种采集方式的分工一句话：**`live-record` 录的是用户的手，`explore-task` 录的是智能体自己的手**，CLI `record on` 录的是指令（盲回放草稿）。前两者采集完都收口于 `author-task`。

什么时候用本技能：用户能说清**目标和终点**但说不清每一步（"你自己进去看看怎么领日常奖励，写成任务"），且流程你自己点得动、不需要用户的手法/账号操作。

## 1. 前置

- adb 不在 PATH：`$env:PATH = "$env:LOCALAPPDATA\Android\Sdk\platform-tools;$env:PATH"`
- 设备在线：`list_devices`（无线设备先 `connect_device`）。
- **向用户问清三件事**，别自己猜：
  1. 目标流程是什么（一句话，将来的任务名）；
  2. **终点怎么判定**（到哪个画面/出现什么文字算做完）——这决定最后一个节点的锚点；
  3. 有没有不该碰的东西（消耗品、充值、删档类操作）。
- 确认起始画面：`screenshot` 看一眼，和用户说的起点对上再开录（起点不对，录出来的第一个节点锚点就是错的）。

## 2. 开录

```
record_actions_start(device_id, kind="explore", label=<流程名>)
```

返回 `{ok, device_id, session_dir, manifest_path, started_at, context, step_count}`；产物落 `outputs/agent_sessions/<时间戳>_<label>/`，`session.json` **每步都重写一次**，中途断了也不丢。

`ok: False` 表示这台设备已有活跃日志（错误里带着它的 `session_dir`）——先 `record_actions_stop` 再开，别覆盖。

开录后每个动作都会多一次截图开销，**探完立刻停录**，别让日志开着漫游。

## 3. 探索纪律（决定草稿质量的地方）

- **每步先 `screenshot_marked(device_id)`，再优先 `click_index(device_id, index)`**。这是本技能的核心：`click_index` 点中的元素（`source` / `text` / `bounds`）会自动写进日志，草稿生成器据此写出**识别锚点节点**；裸 `click(x, y)` 只有在屏幕刚好被标注过、且坐标落在某元素内时才反查得到元素，否则这一步只剩坐标，草稿里会退化成带 `TODO: 盲点坐标，待补锚点` 的盲点节点。
- 索引**只对生成它的那一帧有效**——画面一变就重新 `screenshot_marked`。
- `screenshot_marked` 的 `source` 默认 `"auto"`：先 uiautomator dump，dump 稀疏时（游戏单 Surface 渲染）自动回退 OCR。**元素表来自 OCR 也照用**——`click_index` 一样点得中，草稿会把这类元素写成 `ocr` 识别 + ROI，正是游戏内该用的通道。必要时显式传 `source="ocr"` 或 `"both"`。它比普通截图慢（要跑 dump/OCR），用在每一步的决策帧上，别放进紧循环。
- 锚点文本吃不准（图标无字、OCR 认错）时用 `find_text(device_id, text)` 或 `ocr(device_id, roi=...)` 当场核一遍，别事后猜。
- **允许走弯路**（点错了 `press_key(device_id, 4)` 按 BACK 退回来重走），但**当场记下哪几步是弯路**（步号 / 你点的东西）——停录后要剔掉。
- 不做用户划的禁区操作；碰到没见过的弹窗，**先截图记下来**再决定点不点：这可能是要写成 `finding` 分支的东西，不是随手关掉的噪音。

## 4. 停录

```
record_actions_stop(device_id)
```

返回 `{ok, device_id, session_dir, manifest_path, started_at, ended_at, context, step_count, steps}`。每个 step 是 `{index, t_offset_ms, tool, action, element, screenshot}`：`tool` 是你调的 MCP 工具名，`action` 是执行的动作 JSON（已是 `{"type","params"}` 格式），`element` 是命中的标注元素（没有则 `null`），`screenshot` 是**动作前那一帧**的文件名（相对 `session_dir`）。

记下 `session_dir`，下一步要读它的 `session.json`。

## 5. 生成草稿

1. **Read `<session_dir>/session.json`**，对着 `steps` 逐条过：`element.text` 有没有、是不是弯路、有没有失败的动作。
2. **剔除弯路 / 失败步**：把净化后的 session（保留 `context` / `started_at`，`steps` 只留有效步；`index` 不必重排，转换按位置走）写到 scratchpad 里一份，**不要改原始 `session.json`**（原始日志是证据，也是 handoff 统计的输入）。
3. **转草稿**（确定性、本地、不碰设备）：

```powershell
<python> -c "import json,sys,pathlib; from task.task_editor import action_log_to_draft; s=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); pathlib.Path(sys.argv[2]).write_text(json.dumps(action_log_to_draft(s), ensure_ascii=False, indent=2), encoding='utf-8'); print('draft ->', sys.argv[2], len(s['steps']), 'steps')" <净化后的session.json> <草稿输出.json>
```

（写文件而不是 print，避开 Windows 控制台编码把中文锚点打成乱码。session 没有可用 steps 时会抛 `ValueError`。`<python>` 指**项目环境**的解释器——该命令 import 项目代码，须从项目根目录、用装好 requirements.txt 的 Python 环境运行（环境搭建见 README「环境」节；本仓库开发机的解释器路径记在本地 CLAUDE.md，不入库）。）

草稿形状：每步一个节点，节点名 `step_<NN>[_元素文本]`，有 `element.text` 的写成 `ui_text`（dump 来源）或 `ocr` + ROI（OCR 来源）识别 + `{"type": "click", "target": "recognized"}`，`next` 串成链，末节点 `next: []`，每节点 `post_delay_ms: 800`。

4. **补盲点**：草稿里带 `"comment": "TODO: 盲点坐标，待补锚点"` 的节点 = 没有元素、只剩坐标的步。逐个回到该步的 `screenshot` 帧（或让设备停回那一屏用 `find_text` / `ocr` 核一次），换成真锚点；实在只有纯图标没有文字，考虑 `template`（`capture_template` 先存图）或 `feature` 通道，最后才保留坐标并写明理由。
5. **保存**：

```
save_task(name, task_json)      # task_json 是 JSON 字符串
```

返回 `{ok, path, nodes, lint_warnings}`。**`lint_warnings` 逐条处理或写清豁免理由**（W001 缺 on_timeout / W002 异常分支缺 finding / W003 冷启动缺 popups / W004 有锚点却写死坐标 / W005 无任何 QA 断言）——刚出炉的草稿通常会把 W001/W005 全占上，那正是下一步要补的。config `lint.strict: true` 时有 warning 直接拒存（返回 `ok: False` 且不写盘）。

## 6. 收口：转 author-task

草稿只是**能回放的骨架**，不是合格任务。接着走 `author-task` 技能补齐：

- `on_timeout` 恢复、分支节点的 `finding` 字段、任务级 `watchdogs` / `on_finding` / `popups` 白名单；
- 用户约定的终点做成真识别节点（别让流程靠"点完就算完"收尾）；
- `run_task` 回放迭代到稳定通过；长流程用 `start_task` + `get_run_status` 轮询，含 `agent` 节点则交给 `babysit-run` 技能看护跑完。
- 回放结果里的 `findings` **即使任务成功也要呈现给用户**。
- 结构定型后可 `task renumber <name>` 整一遍步号。

## 自检清单

- [ ] 开录前问清了目标流程、**终点判定**和禁区，起始画面已核对？
- [ ] 探索全程 `screenshot_marked` + `click_index`，裸坐标点击只在别无选择时出现？
- [ ] 弯路 / 失败步已在净化副本里剔除，**原始 `session.json` 未被修改**？
- [ ] 草稿里所有 `TODO: 盲点坐标，待补锚点` 节点都补了锚点或写明保留坐标的理由？
- [ ] `save_task` 已过校验，`lint_warnings` 逐条处理或写清豁免？
- [ ] 已明确移交 `author-task` 补 QA 断言并回放验证，没有把裸草稿当成品交付？
