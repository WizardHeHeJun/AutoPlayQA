# 任务库（识别门控状态机任务）

这里存放**任务 JSON**（`*.json`），每个文件是一台识别门控状态机：引擎在每个节点先**识别**
当前屏幕（`ui_text` / `ocr` / `template` / `feature` / `yolo` / `scene` / `blank_screen` / `always`），命中后执行**动作**，
再按 `next` 候选轮询进入下一节点。任务由 CLI / MCP 加载执行，项目自身不调用任何大模型。

## 怎么用

- **看格式**：写任务前先读权威 schema —— MCP `get_task_schema()` 或 `action/action_schema.py`
  里的 `TASK_SCHEMA_DOC`（含 `includes` / `watchdogs` / `agent` 交接 / `findings` 等完整字段）。
  以那份为准，别在本文件里复刻一份。
- **CLI**：`task list` / `task show <name>` / `task run <name>` / `task save <name>`；
  断点续跑 `task resume <name> <node>`。
- **MCP**：`list_tasks` / `get_task` / `save_task` / `run_task`（长流程用 `start_task` +
  `get_run_status` 后台跑）。
- **推荐创建方式**：用户手动演示一遍、智能体观察生成（技能 `live-record`）——产出的是
  识别驱动的任务，不是盲回放坐标。

## 文件约定

- **文件名 = 任务名**（`open_settings.json` → `task run open_settings`）。
- **本目录的任务是本地资产，默认不入库**（根 `.gitignore` 忽略 `task/task_definitions/**/*.json`），
  和 `task/templates/` 的采集图同一范式——你自己录的任务留在本机。
- **唯一例外 `open_settings.json`**：随仓库发布的最小样例，被测试
  `tests/test_navigation_task.py::test_shipped_sample_task_is_valid` 校验——**别删/别改坏它**；
  它也是新任务的参考模板。
- 写任务优先用 **`ui_text`/`ocr`/`template` 识别锚点 + `"target": "recognized"`**，不要写死坐标；
  游戏内单 Surface 用 `ocr`/`template`，避开 `ui_text` dump。
- 共享节点（弹窗处理等）抽到 include 文件，用 `"includes"` 引用，多任务复用。
- **套件**：`suites/<name>.json` 是**用例编排**（不是任务），形如
  `{"name":..., "cases":["main_smoke", ...], "on_case_failure":"restart_retry", "max_retries":1}`——
  登录一次连跑多个用例，跑挂了自动重启恢复（CLI `task suite <name>` / MCP `run_suite`）。
  前提：每个用例都以**同一个主场景**（游戏主界面）为起点（节点 `用例开始` 真门控）与终点。详见
  `task/suite_runner.py` 与 `ai-docs/docs/modules/task/task-module-guide.md`。

> 本目录只放任务数据，不放代码。加载 / 校验见 `task/task_loader.py`，执行引擎见
> `task/task_engine.py`；任务目录默认 `task/task_definitions/`，可由调用方覆盖。
