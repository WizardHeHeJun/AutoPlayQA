# PipelineEditor — AutoPlayQA 可视化任务编排器

AutoPlayQA（识别门控任务引擎）的 Web 可视化编辑器，随框架仓库分发在
`pipeline_editor/`，直接编辑 `task/task_definitions/<任务名>.json`，不另存副本。

- **画布编排**：拖拽连线编排任务状态机，实线 = `next`、橙虚线 = `on_timeout`。
- **真值校验 + lint**：停手自动干跑 `task_loader.resolve_task`，编辑器不复刻校验规则。
- **截图取 ROI / 模板**：从真机截图上框选写回字段，带试识别所见即所得。
- **真机运行高亮**：后台线程跑引擎，WebSocket 推步进，画布实时高亮当前节点。
- **AI / MCP 协同**：`/mcp` 内嵌编辑面 MCP server，AI 一落盘画布即刻同步。

## 文档

**完整文档见 `docs/`**（VitePress 站点，本地 `cd docs && npm install && npm run dev`）：

- 快速上手（启动、打开/新建任务、典型编辑流、速查表）：`docs/guide/quick-start.md`
- 使用指南（画布、编排、属性面板、校验保存、运行、套件报告、AI/MCP、工具页）：`docs/editor/`
- 架构说明与数据流硬约束：`docs/reference/architecture.md`

## 架构

```text
frontend/  React 19 + TS + React Flow 12 + antd 5 + zustand(+zundo undo/redo)
backend/   FastAPI（直接 import AutoPlayQA 模块；校验真值 = task_loader.resolve_task）
```

- **单一数据源**：任务 JSON 的内存镜像（`editorStore.doc`）；画布是派生视图，
  连线/删边是对 `next` 数组的增量操作，永不从图反推 JSON。
- **include 节点只读**（灰色虚框 + 锁图标）：来自 `_merge.include_map`，
  保存时剔除，绝不固化进主文件。
- **defaults 不展开**：后端给编辑器的合并态特意不做 `_apply_defaults`，
  round-trip 语义无损——JSON 深度相等，由 `tests/test_task_roundtrip.py` 守着
  （保存链路统一以 `indent=2` 重写文件，格式可能与手写原文不同）。
- **布局 sidecar**：节点坐标存 `task/task_definitions/.layout/<name>.json`，
  不污染任务 JSON 与 git diff。
- **运行**：后台线程跑引擎，on_step 回调经 WebSocket 推送（事件带 seq，
  断线快照补齐），画布高亮当前节点 + visited 轨迹。

## 启动

`<python>` = AutoPlayQA 所用环境的 Python 解释器（见框架 README 的「环境」一节）。
命令都在**仓库根**执行。

```powershell
# 依赖（一次性）：装在框架用的同一个环境里，不另建 venv
# （编辑器后端依赖已并入根 requirements.txt；pipeline_editor\requirements.txt 转发到它）
<python> -m pip install -r requirements.txt
cd pipeline_editor\frontend; npm install

# 开发
powershell -File editor.ps1 -Python <python>
# 或分开：
#   powershell -File pipeline_editor\scripts\dev.ps1 -Python <python>   # editor.ps1 转发的就是它
#   <python> pipeline_editor\backend\main.py          # :8930
#   cd pipeline_editor\frontend; npm run dev          # :5173（proxy → 8930）
```

后端默认按自身位置上推一级定位仓库根（`pipeline_editor/` 的上一级）；
把编辑器挪到仓库外时，用环境变量 `AUTOPLAYQA_ROOT` 指向框架根目录。

启动后浏览器打开它打印的地址（默认 `http://localhost:5173`）。要连真机的话，
先保证 adb 在 PATH：`$env:PATH = "$env:LOCALAPPDATA\Android\Sdk\platform-tools;$env:PATH"`。

## 核心用法

编辑的就是仓库 `task/task_definitions/` 里的任务 JSON，不另存副本。

| 页面 / 操作 | 干什么 |
| --- | --- |
| 任务列表页 | 打开 / 新建任务（对应 `task/task_definitions/*.json`） |
| 画布 | 拖拽连线编排状态机：**实线 = `next`**、**橙虚线 = `on_timeout`**；include 共享节点灰色锁定（只读，保存时自动剔除、绝不固化进主文件） |
| 属性面板 | 按字段编辑识别（通道 / expected / roi）与动作（custom 动作下拉 + 参数表单） |
| 校验保存 | 停手自动干跑框架 `resolve_task` 真值校验 + lint，问题面板点击定位节点；保存按钮落盘 |
| 截图取 ROI / 模板 | 从真机截图上框选写回字段，带试识别所见即所得 |
| 运行面板 | 选设备真机跑任务，WebSocket 推步进，画布实时高亮当前节点与走过的轨迹，可协作式停止 |
| 套件 / 报告页 | suite 连跑与 findings 报告查看 |
| 工具页 | 任务健康度、交接固化（agent 节点转草稿）、回放缓存、录制会话转草稿 |

## 与 AI 智能体协同（可选）

仓库根的 `.mcp.json.example` 已带 `pipeline-editor` 条目（http `127.0.0.1:8930/mcp`，
编辑器后端内嵌）。**编辑器开着时**，智能体改任务走这个入口——每次保存后画布 ~2s 内
自动重载，人能实时看到 AI 改了什么并接手微调；编辑器没开时智能体自动退回 stdio 的
`autoplayqa`，编辑工具同名同语义，只是看不到实时画布。

## 测试

```powershell
# 全量（仓库根一条命令跑框架 + 编辑器两套后端回归，配置见根 pytest.ini）
<python> -m pytest

# 只跑编辑器后端回归（无需设备）：REST+MCP 双通道 round-trip、路径穿越、乐观并发、
# custom action 参数 schema 提取
<python> -m pytest pipeline_editor\tests -q
# 前端
cd pipeline_editor\frontend; npm run typecheck; npm test
```

## 编辑器依赖框架的这些能力

编辑器不改框架、也不复刻框架的任何规则，它直接 import：

- `task/task_loader.py` 的 `resolve_task`（**校验真值**）与 `task/task_lint.py` 的 `lint_task`；
- `task/custom_actions` 注册表（属性面板的 handler 下拉与参数表单，
  参数 schema 由 `backend/action_schema_introspect.py` 从 handler 源码静态提取）；
- `task/task_engine.py` 的 `TaskEngine`，含 `request_stop()` 协作式干净停止
  （节点边界 + 轮询循环两处检查点；停止时不触发 popup 扫除 / BACK 兜底），
  错误串为 `TaskEngine.STOP_ERROR`——运行面板的「停止」走的就是它；
- `perception/` 的截图与 OCR（取 ROI / 裁模板 / 试识别）。

框架 `mcp_server.py` 上另有 5 个编辑面薄工具供外部智能体使用（编辑器自身不依赖）：
`validate_task`、`list_custom_actions`、`lint_saved_task`、`get_step_labels`、`list_includes`。

## 致谢与借鉴

交互范式重点借鉴 [MaaPipelineEditor](https://github.com/kqcoxn/MaaPipelineEditor)（可视化构建
MaaFramework Pipeline 的工作流编辑与调试工具）：画布拖拽节点 + 连线表达流转、属性面板按
字段编辑、画布与 JSON 实时同步、内置截图 / 识别辅助工具、调试时节点高亮——本编辑器的
「画布编排 / 截图取 ROI 与模板 / 真机运行高亮」与之同构，画布技术选型（React + React Flow）
也与其一致。

差异点：本编辑器为独立实现（Python FastAPI 后端，无代码复用），且不复刻任何校验规则——
直接 import AutoPlayQA 的 `task_loader.resolve_task` 做真值校验；它编排的协议是 AutoPlayQA
任务 JSON，其「识别门控 + `next` 候选轮询」结构本身借鉴自
[MaaFramework](https://github.com/MaaXYZ/MaaFramework) 的 Pipeline 思想（见框架 README
的[致谢一节](../README.md#致谢与借鉴)）。
