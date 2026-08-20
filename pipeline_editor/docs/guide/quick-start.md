# 快速上手

把后端和前端跑起来，打开一个任务，走一遍从改图到保存、运行的完整流程。

## 启动

`<python>` = AutoPlayQA 所用环境的 Python 解释器（见框架 README 的环境说明）。
以下命令都在**仓库根**执行。

```powershell
# 依赖（一次性）：装在 AutoPlayQA 所用的同一个环境里
<python> -m pip install -r pipeline_editor\requirements.txt
cd pipeline_editor\frontend; npm install

# 开发
powershell -File pipeline_editor\scripts\dev.ps1 -Python <python>
# 或分开：
#   <python> pipeline_editor\backend\main.py                 # :8930
#   cd pipeline_editor\frontend; npm run dev                 # :5173（proxy → 8930）
```

后端默认按自身位置上推一级定位仓库根（`pipeline_editor/` 的上一级），
把编辑器挪到别处时用环境变量 `AUTOPLAYQA_ROOT` 覆盖。

## 打开与新建任务

- **任务列表**（顶栏「任务」）直接扫 `task/task_definitions/*.json`，列出任务名、
  入口节点、节点数、`includes`、修改时间（默认按修改时间倒序），行尾有删除。
  点任务名进编辑器。
- **新建任务**：右上「新建任务」→ 填文件名（如 `shop_smoke`，不带 `.json`）→
  后端立即落盘一个只有单节点 `开始` 的骨架（`recognition: always` + `action: none` +
  `next: []`），随后自动跳进编辑器。名字有白名单校验，`../` 一类路径会被后端拒绝。

## 典型编辑流

1. **任务** 页点任务名 → 进编辑器，画布自动 fitView 展示全图。
2. 看懂画布：卡片 = 一个节点（识别 + 动作），实线 = `next`，橙虚线 = `on_timeout`，
   灰底虚框带锁 = include 只读节点。
3. 单击节点 → 右侧自动跳到「节点」Tab，改识别 / 动作 / next；改完卡片和大纲立刻跟着变。
4. 在画布上拖连线补流程、选中边按 Delete 删连线；需要新节点用工具条「新建节点」。
5. 停手 0.8 秒后自动跑一次后端真值校验：有错就在工具条弹红条 + 问题节点红描边，
   点「问题」Tab 看详情并定位。
6. `Ctrl+S` 保存。保存成功会提示写了多少节点、有几条 lint 提示。
7. 需要真机验证：切「运行」Tab 选设备点运行，画布实时高亮当前节点。

「编辑器」各章按此流程展开，可按需跳读：
[看懂画布](/editor/canvas) →
[编排：连线、删边、增删节点](/editor/editing) →
[属性面板](/editor/inspector) →
[校验、lint 与保存](/editor/validate-save) →
[运行与调试](/editor/run)。

## 速查表

**快捷键**：

| 按键 | 作用 |
| --- | --- |
| `Ctrl+S` | 保存 |
| `Ctrl+Z` | 撤销 |
| `Ctrl+Shift+Z` | 重做 |
| `Delete` / `Backspace` | 删除选中的**边**（只删边，不删节点） |

**鼠标操作**：

| 操作 | 效果 |
| --- | --- |
| 单击节点 | 选中，右侧自动切到「节点」Tab（运行中且你停在「运行」Tab 时不抢焦点） |
| 双击节点 | 强意图，任何时候都切到「节点」Tab |
| 从底部**蓝点**拖到目标节点 | 把目标追加到源节点 `next` 末尾（= 连一条 next） |
| 从右侧**橙点**拖到目标节点 | 设置源节点的 `on_timeout`（单值，会覆盖） |
| 单击边 | 选中（变蓝），再按 `Delete` / `Backspace` 删除 |
| 单击画布空白 | 取消选中 |
| 拖动节点 | 调布局；停止拖动 1.5 秒自动写 sidecar，不产生「未保存」 |
