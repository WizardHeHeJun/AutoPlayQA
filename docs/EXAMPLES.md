# 任务 JSON 实例集

本文档给出可直接复制、跑得通的任务 JSON 示例，覆盖从最小可用到 QA 断言、共享节点、套件、`custom` 动作、agent 交接的完整场景。全部用通用 UI 词汇（设置/主界面/确认等），不涉及任何具体游戏内容。完整字段参考见 `get_task_schema`（MCP）或 `action/action_schema.py` 的 `TASK_SCHEMA_DOC`；编写规范见 `.claude/skills/author-task/SKILL.md`。

## 目录

- [1. 最小任务：识别门控 + target: recognized](#1-最小任务识别门控--target-recognized)
- [2. 带 QA 断言的任务：watchdogs / finding / on_finding / popups / bug-skip](#2-带-qa-断言的任务watchdogs--finding--on_finding--popups--bug-skip)
- [3. includes 共享节点示例](#3-includes-共享节点示例)
- [4. suite 套件示例](#4-suite-套件示例)
- [5. custom 动作示例](#5-custom-动作示例)
- [6. agent 交接续跑流程示例](#6-agent-交接续跑流程示例)

## 1. 最小任务：识别门控 + target: recognized

进入设置、点开显示子菜单的两步流程。每个节点先"识别"（确认锚点真的出现在屏幕上）再"动作"，动作用 `"target": "recognized"` 直接点识别命中的位置，不写死坐标；非终端节点都带 `on_timeout` 兜底，避免识别不到时任务直接判失败。

```json
{
  "entry": "打开设置",
  "nodes": {
    "打开设置": {
      "step": "1",
      "recognition": {"type": "ui_text", "expected": "设置"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["进入显示设置"],
      "timeout_ms": 10000,
      "poll_interval_ms": 500
    },
    "进入显示设置": {
      "step": "2",
      "recognition": {"type": "ui_text", "expected": "显示"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["确认已进入显示页"],
      "on_timeout": "打开设置"
    },
    "确认已进入显示页": {
      "step": "3",
      "recognition": {"type": "ui_text", "expected": "亮度"},
      "action": {"type": "none"},
      "next": []
    }
  }
}
```

- `"确认已进入显示页"` 是一个纯识别节点（`action: none`），只用来断言流程真的走到了预期界面，`next: []` 表示任务到此结束。
- 游戏类目标（单 Surface 渲染，`ui_dump` 拿不到节点）把 `recognition.type` 换成 `ocr`（可加 `roi` 缩小识别范围）即可，其余写法不变。

## 2. 带 QA 断言的任务：watchdogs / finding / on_finding / popups / bug-skip

一个"进入某个功能页"的流程，加上任务级负向断言（`watchdogs`）、异常分支自我上报（`finding`）、良性弹窗白名单（`popups`）和上报后跳过（bug-skip：`skip_to` + `on_finding`）。

```json
{
  "entry": "打开设置",
  "on_finding": "回到主界面",
  "popups": [
    {
      "name": "用户协议弹窗",
      "recognition": {"type": "ocr", "expected": "用户协议", "roi": [100, 800, 980, 1600]},
      "action": {"type": "click", "target": "recognized"}
    }
  ],
  "watchdogs": [
    {
      "type": "ocr",
      "expected": "网络异常",
      "roi": [0, 0, 1080, 400],
      "severity": "error",
      "message": "进入设置页时弹出网络异常提示",
      "skip_to": "回到主界面"
    },
    {
      "type": "blank_screen",
      "threshold": 8.0,
      "severity": "critical",
      "message": "长时间白屏/黑屏，疑似卡死",
      "fail_task": true
    }
  ],
  "nodes": {
    "打开设置": {
      "recognition": {"type": "ui_text", "expected": "设置"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["设置主页", "加载失败提示"],
      "on_timeout": "回到主界面"
    },
    "设置主页": {
      "recognition": {"type": "ui_text", "expected": "显示"},
      "action": {"type": "none"},
      "next": []
    },
    "加载失败提示": {
      "recognition": {"type": "ocr", "expected": "加载失败"},
      "finding": {"severity": "warning", "message": "设置页加载失败提示（异常分支，自我上报）"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["回到主界面"]
    },
    "回到主界面": {
      "recognition": {"type": "ui_text", "expected": "主界面"},
      "action": {"type": "none"},
      "next": []
    }
  }
}
```

要点：

- `popups[0]`：已知良性的用户协议弹窗，**仅在识别卡住时**才扫一遍并点掉，**不记 finding**（噪音，不是异常）；未列入白名单的弹窗仍会照常卡成超时或触发 watchdog。
- `watchdogs[0].skip_to`：识别到"网络异常"文字 → 记一条 finding 留证后跳到 `回到主界面` 继续测，不中止整个任务。
- `watchdogs[1].fail_task`：长时间白屏直接判任务失败（没给 `skip_to` 兜底）。
- 任务级 `on_finding`：logcat 崩溃/ANR 这类没有对应 watchdog 的 bug，统一兜到 `回到主界面`。
- 节点 `加载失败提示.finding`：异常分支一进入就自我上报，不依赖 watchdog 命中。
- **触发源只有两个**：watchdog 命中 + logcat 崩溃/ANR。纯粹的识别超时/卡顿绝不触发 bug-skip，那是 `on_timeout` 的职责。

## 3. includes 共享节点示例

通用弹窗处理、返回主界面这类逻辑很多任务都要用，写成共享节点文件放在 `task/task_definitions/common/` 下，任务用 `"includes"` 引入。

`task/task_definitions/common/popups.json`（片段文件，只允许 `description` + `nodes` 两个键）：

```json
{
  "description": "通用弹窗关闭 + 回到主界面",
  "nodes": {
    "关闭通用弹窗": {
      "recognition": {"type": "ocr", "expected": "知道了"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["回到主界面"]
    },
    "回到主界面": {
      "recognition": {"type": "ui_text", "expected": "主界面"},
      "action": {"type": "none"},
      "next": []
    }
  }
}
```

引用它的任务文件：

```json
{
  "entry": "打开设置",
  "includes": ["common/popups.json"],
  "on_conflict": "strict",
  "nodes": {
    "打开设置": {
      "recognition": {"type": "ui_text", "expected": "设置"},
      "action": {"type": "click", "target": "recognized"},
      "next": ["设置主页"],
      "on_timeout": "关闭通用弹窗"
    },
    "设置主页": {
      "recognition": {"type": "ui_text", "expected": "显示"},
      "action": {"type": "none"},
      "next": []
    }
  }
}
```

- `on_timeout: "关闭通用弹窗"` 跨文件引用了共享片段里的节点，加载时会在**合并后**的节点表上整体校验，全部通过才允许运行（原子加载）。
- 任务保存时保留 `includes` 引用（不会把片段节点内联进任务文件），共享片段更新后，引用它的任务下次运行自动生效。
- 重名节点默认报错（`on_conflict: "strict"`）；确需在任务里特化共享节点，把 `on_conflict` 设为 `"overwrite"`，任务文件最后合并，会覆盖同名的共享节点。

## 4. suite 套件示例

多个用例共享一次冷启动 + 登录连续跑，省掉每个用例重复的开场时间。`cases`/`resume_after`/`case_entry`/`landing` 均为必填字段，没有框架默认值。

```json
{
  "name": "smoke_mini",
  "cases": ["case_open_settings", "case_open_profile"],
  "resume_after": "确认已到达主界面",
  "case_entry": "用例开始",
  "landing": {"type": "ui_text", "expected": "主界面", "roi": [0, 2200, 1080, 2400]},
  "on_case_failure": "restart_retry",
  "max_retries": 1
}
```

- 首个用例整段冷启动（走完自己的 `entry` 到 `resume_after` 那个节点为止）；后续用例跳过重复的开场部分，直接从 `resume_after` 之后续跑各自的 `case_entry`。
- `landing` 是两个用例之间的落地画面校验（识别 spec），确认上一用例结束后确实停在预期界面，可显式设为 `null` 关闭该检查。
- `on_case_failure` 三选一：`restart_retry`（默认，冷启动重跑重试）/ `restart_continue`（不重试，下一用例照常冷启动）/ `abort`（终止套件，剩余用例标记跳过）。
- 每个用例仍是独立 run，各自写独立的 `findings` 目录与 `report.json`；套件连跑只省登录时间，不合并证据。

## 5. custom 动作示例

`custom` 用于介于"单条 adb 原子动作"和"agent 挂起"之间的多步确定性逻辑（不需要智能判断）。以下两个是框架内置的 `custom` 处理器。

**`swipe_until`：反复滑动直到识别命中（在列表/滚动页里找目标）**

```json
{
  "找到目标选项": {
    "recognition": {"type": "always"},
    "action": {
      "type": "custom",
      "name": "swipe_until",
      "params": {
        "recognition": {"type": "ocr", "expected": "高级设置"},
        "swipe": {"x1": 540, "y1": 1800, "x2": 540, "y2": 600, "duration_ms": 300},
        "max_swipes": 6,
        "settle_ms": 800
      }
    },
    "next": ["点击目标选项"]
  },
  "点击目标选项": {
    "recognition": {"type": "ocr", "expected": "高级设置"},
    "action": {"type": "click", "target": "recognized"},
    "next": []
  }
}
```

**`launch_app`：冷启动拉起应用（作为任务开场节点）**

```json
{
  "entry": "冷启动应用",
  "popups": [
    {
      "name": "首次启动权限弹窗",
      "recognition": {"type": "ocr", "expected": "允许"},
      "action": {"type": "click", "target": "recognized"}
    }
  ],
  "nodes": {
    "冷启动应用": {
      "recognition": {"type": "always"},
      "action": {
        "type": "custom",
        "name": "launch_app",
        "params": {"package": "com.example.app", "force_stop": true, "settle_ms": 3000}
      },
      "next": ["确认到达主界面"]
    },
    "确认到达主界面": {
      "recognition": {"type": "ui_text", "expected": "主界面"},
      "action": {"type": "none"},
      "next": []
    }
  }
}
```

- `launch_app` 会先确保屏幕唤醒解锁，再按需 `force-stop` 后拉起应用，因此适合放在任务入口做冷启动开场。
- 用 `custom` 动作的任务节点，`recognition` 常写 `{"type": "always"}`——这一步不需要先确认锚点，动作本身（滑动查找/冷启动）就是要做的事；真正的门控发生在 `custom` 处理器内部（`swipe_until` 内部反复识别）或紧接着的下一个节点（`launch_app` 后用 `确认到达主界面` 断言结果）。
- 其余内置 `custom` 动作：`gm_command`（下发 GM 指令）、`ensure_checkbox`（幂等把开关拨到目标态）、`set_text_field`（清空后输入文本）、`click_topmost_text`（点列表里最靠上的同名项）。`list_custom_actions`（MCP）/ 任务加载时的注册校验会给出当前可用的完整名单，不要凭记忆硬编码。

## 6. agent 交接续跑流程示例

某一步需要智能判断（比如无法用识别锚点确定性表达的动态手势），任务节点写成 `agent` 动作：

```json
{
  "手动完成验证步骤": {
    "recognition": {"type": "ui_text", "expected": "验证"},
    "action": {"type": "agent", "text": "请观察当前弹出的验证控件，完成后点击确认按钮"},
    "next": ["验证完成"]
  },
  "验证完成": {
    "recognition": {"type": "ui_text", "expected": "主界面"},
    "action": {"type": "none"},
    "next": []
  }
}
```

外部智能体（Claude Code / Codex）驱动的续跑流程：

1. 调用 `run_task(device_id, name)`（或后台版 `start_task` + `get_run_status` 轮询）；
2. 引擎识别到 `手动完成验证步骤` 节点后**不执行动作**，直接挂起返回：
   ```json
   {"status": "agent_required", "handoff": {"node": "手动完成验证步骤", "instruction": "请观察当前弹出的验证控件，完成后点击确认按钮"}}
   ```
3. 智能体用 `screenshot`/`click`/`swipe` 等动作工具，按 `handoff.instruction` 手动完成这一步；
4. 调用 `run_task(device_id, name, start_after="手动完成验证步骤")` 续跑，引擎从该节点之后继续按识别门控往下走。

CLI 等价流程：`task run <name>` 命中 agent 节点后打印续跑指引 → 人工在 CLI 里用 `click`/`drag`/`input` 完成该步 → `task resume <name> <节点名>`。

真正无法确定性化的步骤才用 `agent`；能用识别锚点/`custom` 处理器表达的操作应该节点化，避免每次回放都要人工/智能体介入一轮。
