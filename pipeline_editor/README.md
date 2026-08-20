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
