---
name: live-record
description: 观察式录制：用户在真机上手动操作一遍流程，智能体通过 MCP 工具同步监控、识别每一步，最终生成识别驱动的任务 JSON（非盲回放草稿）。当用户说"我手动走一遍你来录"、"观察式录制"、"live record"、"你监控我操作生成任务"时使用。
---

# 观察式录制（live-record）

目标：用户手动演示一遍操作流程，你（智能体）实时观察并识别每一步，产出一个**真机验证过锚点的识别驱动任务 JSON**，保存到 `task/task_definitions/` 并回放验证。

与内置"方式三"（CLI `record on` 录指令 → 盲回放草稿）不同：本流程录的是用户在**手机屏幕上的真实操作**，产出直接是正式质量的任务，不需要二次加固。

第三种采集方式是 **`explore-task` 技能**（智能体自探自录：你自己驱动设备走一遍，动作日志自动记锚点再转草稿）——本技能录的是**用户的手**，那个录的是**智能体自己的手**，用户说不清每一步、但你自己点得动流程时用它。

## 前置条件

- adb 不在 PATH：`$env:PATH = "$env:LOCALAPPDATA\Android\Sdk\platform-tools;$env:PATH"`
- 设备已连接（MCP `list_devices` 确认；无线设备用 `connect_device`）
- 任务 JSON 格式以 MCP `get_task_schema` 返回的 TASK_SCHEMA_DOC 为准
- 开始前向用户确认：流程名字、起始画面、用做法 A 还是 B

## 做法 A：一步一停（默认，流程短 / 无复杂手势时用）

1. 用户停在起始画面，告诉你开始。
2. `screenshot` + `ui_dump` 记录当前画面；dump 有节点用 `ui_text` 锚点，dump 为空（游戏单 Surface）用 `ocr`，必要时记 ROI。
3. 用户做**一步**操作后停 1~2 秒，告诉你（或你轮询截图比对像素差发现画面变了）。
4. 你截图比对前后画面：
   - 新画面的特征文本 → 下一节点的 recognition；
   - 用户点了什么 → 从前一帧的可点元素 + 画面跳转结果推断，拿不准**直接问用户**。
5. 重复 3-4 直到流程结束。
6. 中途出现弹窗/广告等异常分支：记成带 `finding` 字段的节点，不要丢弃。

限制：adb 截图约 1 秒/帧，用户必须一步一停；连点快滑会丢帧 → 改用做法 B。

### 可选：用后台监控替代手动 screenshot 轮询

做法 A 的第 3 步"你轮询截图"可以交给后台线程，主流程不变：

1. `start_monitor(device_id, interval_ms=1000, max_frames=200)` → 返回 `monitor_dir`，后台开始按间隔存帧。
2. 用户做完一步告诉你时，`get_new_frames(device_id)` → 拿到这段时间新增帧的**路径 + 毫秒时间戳**（不返图片内容），挑关键帧（一般是最后一帧 = 操作后画面，必要时回看变化那一帧）自己 `Read`。
3. 流程结束 `stop_monitor(device_id)`。停了帧还在盘上，还能再拉一次尾帧。

好处：不用每看一眼就花一个回合去调 `screenshot`；坏处是帧仍受截图速率限制，**连点/快滑照样丢帧——那种场景仍然走做法 B**。返回值里 `dropped` 一直涨就说明拉得太稀或 `max_frames` 太小；`latched_off` 非空说明连续取帧失败已自动停（先查设备连接）。

## 做法 B：手势录制（流程长 / 有滑动手势 / 用户不想一步一停时用）

MCP 工具直接录用户的真实手指操作：触摸事件切分成手势 + 每个手势自带按下前/操作后的帧和落点锚点图，坐标不靠推断、不丢帧。

1. **校准**（可选前置，每台设备一次）：`calibrate_touch(device_id)`。
   - 命中 `outputs/touch_calibration/<序列号>.json` 缓存且分辨率一致 → 直接返回缓存；分辨率对不上（换设备/改过分辨率/横竖屏）自动重新校准并覆盖。
   - 返回 `ok: false` 说明设备不在线或面板没有 ABS_MT_POSITION，先解决再录。
   - 不调也行：`record_gestures_start` 自己会校准并刷新同一份缓存；显式调用只是想提前确认能录。
2. **开录**：`record_gestures_start(device_id)` → 返回 `session_dir`（产物落 `outputs/recordings/<时间戳>/`）。
   - 一台设备同时只能有一个录制会话；重复 start 会明确报错。
3. **让用户按正常速度演示整个流程**（不用一步一停，也不用你轮询截图）。演示中告诉用户：出现弹窗/报错照常处理，那些也要录进去。
4. **停录**：`record_gestures_stop(device_id)` → 拿到手势序列：
   - 每个手势：`index`、`type`（tap / long_press / swipe / multi_touch）、`params`（tap 的 x/y；swipe 的 x1/y1/x2/y2 + duration_ms + path）、`down_point`、`duration_ms`、`t_offset_ms`（相对开录时刻）、`recorded_at`；
   - `images`：`before`（按下前那一帧，无点击高光）、`after`（操作稳定后那一帧）、`anchor`（落点 120×120 裁剪图）的文件路径，直接 Read 看图；
   - 完整信息另存 `session_dir/gestures.json`（含多指回放用的 pointer frames）；录制中每记一条就落一次盘，中途断了也不丢。
5. **映射锚点**（离线做，无需设备）：按 `index` / `t_offset_ms` 顺序逐个手势——Read 它的 `anchor` 裁剪图看清用户点的是什么控件，再 Read `before` 帧确认这一步的画面特征文本，两者合起来就是该节点的 recognition 锚点（游戏单 Surface 的画面写 `ocr` + ROI，ROI 取 `down_point` 附近）。该手势的 `after` 帧 = 这一步的预期结果，用来定下一节点的锚点、判断要不要拆节点。坐标只用于反查锚点，**不进任务 JSON**。
   - 锚点文本吃不准时，让用户把设备停回那一屏，用 `find_text` / `ui_dump` / `ocr` 在真机上核对一次再写。

## 产出任务 JSON

本技能负责把**观察到的每一步**准确映射成节点；之后的结构化、加 QA 断言、回放迭代由 **`author-task` 技能**收口（两个技能配合：这里管"录"，那里管"写"）。

映射阶段（live-record 专属）：

- 每一步一个节点：`recognition` 用该步**操作前画面**的特征文本（`ui_text` 优先，游戏画面用 `ocr`+ROI），`action` 用 `{"type": "click", "target": "recognized"}`，**不写死坐标**（坐标只用于反查锚点文本）。
- 锚点文本反查不到（纯图标按钮、OCR 识别不出）时才退化为坐标 click，并在节点旁加注释性 finding 说明。
- 滑动步骤：固定 drag 参数，或语义是"滑到某元素出现"时改用 custom `swipe_until`。
- 中途出现的弹窗 / 异常分支：记成带 `finding` 字段的节点，列进相关步骤的 `next`，别丢弃。

→ 收口阶段（转 `author-task`）：补 `on_timeout` 恢复、任务级 `watchdogs` / `on_finding` / `popups`，`save_task` 过校验、`run_task` 回放迭代到稳定通过，按需 `task renumber` 整步号。回放结果里的 `findings` 即使任务成功也要呈现给用户。
