---
name: edit-task
description: 安全修改已有任务 JSON：合并态/原始态分辨（include 节点绝不固化）、改名级联、validate 干跑→save→renumber 的编辑回路，以及数据驱动维护——用 task health 的 fallback_rate 修腐化锚点、用 task handoffs 把反复交接的 agent 节点固化为确定性节点、录制会话转草稿。当用户说"改一下这个任务"、"这个节点老超时/识别不到了"、"把 agent 节点固化"、"任务健康度"、"锚点漂了"、"从录制生成任务"时使用。从零写新任务用 author-task；本技能管"改旧"。
---

# 修改与维护任务流程（edit-task）

目标：在**不破坏共享语义、不丢引用**的前提下修改已有任务，并用运行数据（健康度 / 交接统计 / 录制会话）驱动维护，而不是凭感觉改。

**字段语法以 MCP `get_task_schema` 为准；编写判断（选通道/加断言）见 `author-task`**。本技能讲的是编辑特有的坑与回路。配套可视化编辑器 PipelineEditor（独立仓库，`scripts\dev.ps1` 启动后浏览器打开它打印的本地地址）承载同样的能力——人在画布上改，agent 走 MCP 工具，两边写的是同一份文件、过同一套校验；没装编辑器时本技能的所有步骤照常可用（走 MCP / CLI）。

**编辑类 MCP 有两个入口，优先用 `pipeline-editor`**（`.mcp.json` 里的 http server，编辑器后端内嵌）：它与画布同进程，你每次 `save_task`/`renumber_task` 后 **~2s 内用户的画布会自动重载**——用户能实时看到你改了什么并接手微调，所以**保存后先停手等用户表态**，别对同一任务连续叠改；若用户画布上有未保存修改，他会看到冲突横幅自行取舍。`pipeline-editor` 不可用（编辑器没起）时退回**本项目的 stdio MCP server**（`.mcp.json.example` 里名为 `autoplayqa`）的同名工具，功能一致但用户看不到实时画面。设备/感知/运行类工具只在本项目的 server 上。

## 0. 动手前的三个事实核对（跳过任何一条都可能改坏）

1. **你看到的是合并态**：`get_task` 返回 includes 已合并的节点表。查 `_merge.include_map`——值不是 `"<task>"` 的节点来自共享片段，**改它会影响所有引用该片段的任务**。要改片段节点，去改 `task/task_definitions/common/*.json` 本体；要只在本任务特化，主文件里写同名节点 + `"on_conflict": "overwrite"`。
2. **保存写的是原始态**：`save_task` 原样保留 `includes` 引用。**绝不要把 `get_task` 的合并结果直接存回去**——那会把片段节点固化进主文件、把任务级 `defaults` 固化进每个节点（还会把节点显式 `null` 的"退回引擎默认"语义抹掉）。改动始终基于磁盘上的原始 JSON。
3. **步号是导出物不是数据**：`step` 字段由图重算（`task renumber` / MCP 无对应、编辑器有按钮 / `write_step_labels`），引擎不读它。结构性修改（增删节点、改 next 顺序）后重跑 renumber，别手改 step。

## 1. 编辑回路（每次修改都走完整回路）

```
读原始 JSON（磁盘文件，不是 get_task 的合并态）
  → 修改
  → validate_task(task_json)   # MCP 干跑：结构校验 + 步号 + lint，不落盘
  → save_task(name, task_json) # 落盘；处理返回的 lint_warnings（W001-W006）
  → renumber                   # 结构变了才需要
  → run_task / start_task 回放验证，看 findings
```

- `validate_task` 失败信息里带节点名（`Node 'xxx' ...`），按图索骥。
- `lint.strict: true` 时有 warning 直接拒存——先逐条处理或写明豁免。
- 编辑器里同一回路是自动的：防抖实时校验 + 保存按钮 + 问题面板定位节点。

## 2. 改名 / 删除的级联清单（引用是名字字符串，不会自动跟随）

改一个节点名，必须同步检查这**六处**引用：

| 引用位置 | 在哪 |
|---|---|
| `entry` | 任务根 |
| 其它节点的 `next[]` | 全部节点 |
| 其它节点的 `on_timeout` | 全部节点 |
| watchdog 的 `skip_to` | 任务根 `watchdogs[]` |
| 任务级 `on_finding` | 任务根 |
| 套件的 `resume_after` / `case_entry` | `suites/*.json`（**跨文件**，validate_task 查不到；漏改会在套件预检时炸） |

删除节点同理：先搜全部引用，把 `next` 里的项移除、`on_timeout`/`skip_to`/`on_finding` 改指别处。编辑器的改名操作自动级联前五处并弹出影响清单；套件引用仍要自查。

## 3. 数据驱动维护（别凭感觉修）

### 3.1 锚点腐化：`task health`（编辑器"工具→任务健康度"；`scan_health`）

`fallback_rate =（timeout_recoveries + popup_assisted_hits）/ 总命中`。某节点持续 > 0.3 = 锚点腐化嫌疑，按序排查：

1. **锚点文本还在吗**：`screenshot` + `find_text`/`ocr` 真机确认。游戏改版换文案是头号原因。
2. **换更稳的锚点**：易变的数字/昵称/活动名 → 换成界面标题等稳定文本；或 `ocr` 加 `roi` 缩小搜索范围。
3. **通道换对了吗**：dump 取不到 → `ui_text` 改 `ocr`；纯贴图 → `template`（重截模板）；常换皮 → `feature`。
4. **只是慢**：`timeout_ms` 加大、或补 `wait_still`——别急着换锚点。
5. `drift_count` 高但仍命中 = 布局挪了：**有意改版后清回放缓存**（`clear_replay_cache` / 编辑器"工具→回放缓存"），否则会有一波 anchor_drift 误报；清后首轮回放全屏重识别属正常。

### 3.2 交接固化：`task handoffs`（编辑器"工具→交接固化"；`scan_handoffs`）

同一 agent 节点 **≥3 次交接且 ≥80% 走同一套操作** = `solidify_candidate`，该固化了：

1. 取该节点最近一次交接录制（`record_actions_start(kind="handoff", task=..., node=...)` 录的，在 `outputs/agent_sessions/`）。
2. `task_editor.action_log_to_draft(session)` 转成识别驱动节点链（编辑器"生成草稿"按钮即此函数；锚点步骤自动变 `ui_text`/`ocr` + `target:"recognized"`，盲点步骤带 `TODO` 标记）。
3. 把节点链**融进原任务**替换 agent 节点：原 agent 节点的上游 `next` 指向链头，链尾 `next` 接原下游；补 `on_timeout` 与 finding 断言（草稿只有主干）。
4. 回放验证整段。**方向就是 agent 节点逐渐节点化**（项目约定）——交接是探路，不是终态。

### 3.3 录制 → 草稿（新流程也一样）

explore 类录制（`kind="explore"`）同样能出草稿：编辑器"工具→录制会话→草稿"选会话即可（逐步预览带截图与锚点标注）。草稿是**主干**：保存前必须补 QA 断言（watchdogs / 异常分支 finding / on_timeout），标红的盲点坐标逐个补锚点——这部分判断回 `author-task` 第 2/4 节。

## 4. 工具对照表（agent 用 MCP，人用编辑器，同一后端逻辑）

| 动作 | MCP 工具 | PipelineEditor |
|---|---|---|
| 读任务（合并态+步号） | `get_task` | 画布 + 大纲（include 节点灰色只读锁） |
| 干跑校验 | `validate_task` | 编辑时防抖自动跑 + 问题面板 |
| 保存 | `save_task` | 保存按钮（同样过 lint.strict 门） |
| 步号回写 | —（CLI `task renumber`） | 工具条"重排步号"（有未保存修改时禁用） |
| Lint | `lint_saved_task` | 保存后问题面板 |
| 健康度 | —（CLI `task health`） | 工具→任务健康度 |
| 交接统计 | —（CLI `task handoffs`） | 工具→交接固化（一键生成草稿） |
| 录制会话→草稿 | —（`action_log_to_draft`） | 工具→录制会话→草稿 |
| 清回放缓存 | `clear_replay_cache` | 工具→回放缓存 |
| 片段清单 | `list_includes` | 任务设置面板（includes 只读展示） |
| custom 动作名 | `list_custom_actions` | 动作表单下拉（动态） |

## 提交前自检清单

- [ ] 改的是主文件节点？（include 节点去片段文件里改，或 overwrite 特化）
- [ ] 保存的是原始态？（没把合并态/defaults 展开结果存回去）
- [ ] 改名/删除跑完六处级联（含**套件**的 resume_after/case_entry）？
- [ ] `validate_task` 干跑过了，`save_task` 的 lint_warnings 逐条处理？
- [ ] 结构变了的话 renumber 刷新步号？
- [ ] 有意改版引起的漂移，清了回放缓存？
- [ ] 回放验证过，findings 呈现给用户？
