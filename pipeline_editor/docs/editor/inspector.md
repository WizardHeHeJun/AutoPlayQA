# 属性面板

右侧面板是改字段的主战场：节点级字段在「节点」Tab，任务级字段在「任务设置」Tab，
`roi` 与 `template` 两类字段还能直接从真机截图上框选取值。

## 节点 Tab

选中节点后在这里改所有字段，改动即时落到内存 doc，画布卡片同步刷新。
include 节点在这里全表单禁用，并在顶部提示来源文件。

「节点」Tab 自上而下分两段：**识别**（type 下拉驱动字段集，`roi` 是四个数字框 +
准星取值按钮）与**动作**（选 `custom` 时，handler 选定后按后端 schema 渲染出
带类型标注的参数表单）。

**识别（recognition）** — type 下拉驱动字段集：

- `always`（无条件命中）/ `ui_text`（控件文本，真机每次约 4.3s）/ `ocr` / `blank_screen` /
  `template` / `feature`（ORB）/ `yolo` / `and` / `or`。切换 type 会保留兼容字段、丢弃不兼容字段。
- `ui_text` / `ocr` → `expected`（待匹配文本）；`template` / `feature` → 模板选择器
  （下拉带缩略图预览，右侧剪刀 = 从截图裁新模板）。
- `threshold` 的含义随 type 变（文本相似度 0.65 / 模板相关性 0.8 / 黑白屏灰度标准差 8.0），
  label 上直接写了默认值。`template` 另有 `scales`（多尺度，逗号分隔）与 `grayscale`；
  `feature` 有 `min_matches` / `ratio`；`yolo` 有 `label` / `model` / `conf`，
  类别与模型名从后端拉真实可用列表做补全。
- 除 `always` / `and` / `or` 外都有 **`roi`**：四个数字框 `[x1,y1,x2,y2]`（设备原始像素，
  留空 = 全屏），旁边的准星按钮打开截图框选（见本页[截图取 ROI 与模板](#截图取-roi-与模板)），
  有值时多一个「清除」。
- `and` / `or` 递归嵌套子识别（最多 2 层，到第 2 层下拉里就没有 `and`/`or` 了）。
  子识别里出现 ≥2 个 `ui_text` 会弹黄色警告（每个都触发一次 uiautomator dump）。
  `and` 且子识别多于一条时可设 `box_index`，指定点击哪一条的命中框。

**动作（action）**：`click` / `drag` / `input_text` / `wait` / `key` / `gesture` / `none` /
`agent`（挂起交接给外部智能体，填 `text` 指令）/ `custom`（下拉选后端已注册的 handler，
`params` 用 JSON 文本框，解析失败当场红条报错）。
`click` 默认勾「点击识别命中中心」（`target: recognized`，推荐）；取消勾选才填 `x`/`y`。
`key` 的 keycode 提示了 4=BACK、3=HOME、82=MENU。`click`/`drag`/`input_text`/`key`/`gesture`
下方有「连发参数」折叠区（`repeat` / `repeat_delay_ms` / `repeat_wait_freezes_ms`，
用于 QTE 连点，不重跑识别）。旧的 `llm` 会显示成 `agent` 并提示是废弃别名，保存时保留原值。

**next 候选（顺序 = 识别优先级）**：列表可拖手柄排序，也有上下移按钮；每行可删、
点名字跳到该节点；「添加候选」下拉等价于在画布上连一条线。列表为空时提示
「next 为空 = 任务成功终点」，卡片上对应 `终点 ✓`。

**超时兜底 / QA**：`on_timeout` 下拉选恢复节点；`finding` 开关打开后填
severity（info/warning/error/critical）+ message——进入该节点就上报一条 QA 发现。

**时序调参**（折叠区，留空 = 走任务 `defaults` / 引擎默认）：`timeout_ms`（识别轮询总预算，
默认 10000）、`poll_interval_ms`（1000）、`post_delay_ms`（0），以及 `wait_still`
（画面静止再放行：`timeout_ms` / `interval_ms` / `threshold`，超时不算失败）。

## 任务设置 Tab

任务级字段：`entry`（入口节点）、`on_finding`（bug-skip 全局兜底节点）、`max_steps`、
`back_fallback`；若任务有 `includes`，这里以只读 Tag 列出并可改 `on_conflict`
（strict / overwrite）。

`defaults` 三项时序默认值（优先级：节点字段 > defaults > 引擎默认；节点写 `null` 可退回引擎默认）。

`watchdogs`（负向断言）：每条是一个识别 + severity + `message` + `fail_task`（命中即中止）、
`skip_to`（命中后记 finding 并跳转继续测）。
`popups`（良性弹窗白名单，仅在识别停滞时扫描，被消除的不记 finding）：`name` 日志标签 +
检测识别 + 可选 `confirm` 同帧二次门控 + 消除动作（只允许 click / key / gesture，
选了别的会当场红条提示）。

整个 Tab 自上而下是：任务元信息（`entry` / `on_finding` / `max_steps` /
`back_fallback` + 只读 includes 标签与 `on_conflict`）、`defaults` 三项时序默认值、
按 severity 着色折叠的 watchdogs 与 popups 两个列表。

## 截图取 ROI 与模板

**前置条件**：✅ 后端已启动　✅ 有**在线设备**
（后端经 adb 发现，运行面板和这两个弹窗里的设备下拉共用一个选择）

- **框选 ROI**：`roi` 字段旁的准星按钮 → 选设备后自动截一张全分辨率图 →
  在图上按住拖动画框（坐标换算回设备原始像素，弹窗顶部实时显示 `[x1, y1, x2, y2]`）→
  「使用此 ROI」写回字段。弹窗里还有**试识别**：文本类识别显示「OCR 试读」，
  模板类显示「模板试匹配」，点了会把命中框和分数以绿框叠加在截图上——所见即所得地调
  ROI 和 threshold。截图不满意点「重新截图」。
  弹窗顶部的标签会随拖动实时显示当前框的设备原始像素坐标，
  「OCR 试读」的命中结果以绿框 + 相似度分数叠加在截图上。

- **裁模板**：`template` 字段旁的剪刀按钮 → 选设备截图 → 拖框选中要做模板的区域 →
  填模板名 → 「保存模板」写进 AutoPlayQA 的模板目录（`task/templates/`），
  并自动回填到当前字段，模板下拉列表同时刷新。
  弹窗左上选设备、右上填模板名，图上拖出的橙框就是将被裁出并落盘的模板区域。
