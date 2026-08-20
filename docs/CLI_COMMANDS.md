# CLI 命令手册

`python main.py` 启动交互式 CLI 后进入 `apq>` 提示符，逐条输入命令。本文档按命令族列出语法、说明与示例（命令解析见 `user_interface/command_parser.py`，分发实现见 `user_interface/cli_handler.py`）。

## 目录

- [通用命令](#通用命令)
- [设备管理（device）](#设备管理device)
- [Agent 管理（agent）](#agent-管理agent)
- [直接动作](#直接动作)
- [自然语言指令（action）](#自然语言指令action)
- [任务管理（task）](#任务管理task)
- [会话录制（record）](#会话录制record)
- [手势录制（record gestures）](#手势录制record-gestures)
- [调试开关（debug）](#调试开关debug)

## 通用命令

| 命令 | 说明 |
|------|------|
| `help` | 打印命令帮助文本 |
| `exit` | 退出 CLI |

## 设备管理（device）

`device` 与 `devices` 是同一命令的两种写法（别名）。

| 命令 | 说明 | 示例 |
|------|------|------|
| `device list` | 枚举当前 adb 可见的设备/模拟器，并同步进 Agent 池 | `device list` |
| `device connect <ip[:port]>` | 通过 Wi-Fi 连接设备（`adb connect`），端口默认 5555 | `device connect 192.168.1.100:5555` |
| `device disconnect [ip[:port]]` | 断开一个无线 adb 设备；不带地址断开全部无线设备 | `device disconnect 192.168.1.100:5555` |
| `device tcpip <device_id> [port]` | 把 USB 连接的设备切到 TCP/IP 模式，打印可用于 `connect` 的地址；端口默认 5555 | `device tcpip emulator-5554` |
| `device pair <ip:pairing_port> <code>` | Android 11+ 无线调试配对（每台机器只需一次），配对端口与连接端口不同 | `device pair 192.168.1.100:37251 123456` |

## Agent 管理（agent）

一台设备对应一个 Agent，`agent select` 决定后续动作命令作用于哪台/哪些设备。

| 命令 | 说明 | 示例 |
|------|------|------|
| `agent list` / `agents list` | 列出当前 Agent 池及被选中状态 | `agent list` |
| `agent select <index\|device_id\|all>` | 按序号 / 设备号 / `all` 选择目标设备 | `agent select 1` 或 `agent select all` |

## 直接动作

| 命令 | 语法 | 说明 | 示例 |
|------|------|------|------|
| `click` | `click <x> <y>` | 在当前选中设备上点击绝对像素坐标 | `click 540 1200` |
| `drag` | `drag <x1> <y1> <x2> <y2> [duration_ms]` | 拖拽/滑动，时长默认 500ms | `drag 200 1000 200 400 300` |
| `input` | `input <text>` | 向当前焦点输入框输入文本 | `input hello123` |

## 自然语言指令（action）

| 命令 | 说明 | 示例 |
|------|------|------|
| `action <指令>` | 走本地解析器（`core/text_resolver.py`）：先试显式坐标正则，再用 dump/OCR 做文本定位 | `action 点击设置按钮` |
| `<不带任何前缀的输入>` | 未匹配到任何已知命令时，默认按自然语言指令解析（等同 `action`） | 直接输入 `点击设置按钮` |

## 任务管理（task）

| 命令 | 语法 | 说明 | 示例 |
|------|------|------|------|
| `task list` | `task list` | 列出 `task/task_definitions/` 下已保存的任务 | `task list` |
| `task show` | `task show <name>` | 先打印按步号排序的流程大纲，再打印任务原始 JSON | `task show open_settings` |
| `task run` | `task run <name>` | 在当前选中的设备（或 `all`）上运行任务；命中 `agent` 节点会打印续跑指引 | `task run open_settings` |
| `task resume` | `task resume <name> <node>` | agent 交接步骤完成后，从指定节点续跑（等价于 `task run` 的 `start_after`） | `task resume open_settings 手动登录` |
| `task renumber` | `task renumber <name>` | 按当前节点图重算 `step` 步号并写回文件 | `task renumber open_settings` |
| `task suites` | `task suites` | 列出 `task/task_definitions/suites/` 下的套件定义 | `task suites` |
| `task suite` | `task suite <name> [device_id]` | 运行一个套件：登录一次连跑多个用例，用例失败按策略重启重试；不带 `device_id` 时用当前选中设备（仅一台时可省） | `task suite smoke_mini` |
| `task cache status` | `task cache status` | 查看回放锚点缓存（OCR ROI 加速用）大小与路径 | `task cache status` |
| `task cache clear` | `task cache clear` | 清空回放锚点缓存 | `task cache clear` |
| `task lint` | `task lint <name>` | 对任务跑最佳实践体检（W001-W007，只提醒不阻断） | `task lint open_settings` |
| `task health` | `task health [name] [--days N]` | 跨 run 聚合历史 `report.json` 的 `node_stats`，看锚点腐烂趋势；不带任务名统计全部任务，`--days` 限定时间窗 | `task health open_settings --days 7` |
| `task handoffs` | `task handoffs [name] [--days N]` | 聚合 `agent` 交接动作日志，提示哪些交接节点值得固化成确定性节点 | `task handoffs --days 14` |
| `task save` | `task save <name>` | 把当前 `record on/off` 录到的命令序列存成回放草稿任务（`always` 识别 + 字面动作，需要再由智能体改写为识别驱动版本） | `task save draft_flow` |

## 会话录制（record）

用于把一系列 CLI 命令录成回放草稿任务，配合 `task save` 使用。

| 命令 | 说明 | 示例 |
|------|------|------|
| `record on` | 开始录制（会清空上一次会话记录） | `record on` |
| `record off` | 停止录制，打印已捕获的命令数 | `record off` |
| `record status` | 查看当前是否在录制及已记录命令数 | `record status` |

## 手势录制（record gestures）

用于观察式录制（用户在真机上用手指演示流程），走 `getevent` 真手指手势捕获，区别于上面基于 CLI 命令的 `record on/off`。

| 命令 | 语法 | 说明 |
|------|------|------|
| `record gestures start` | `record gestures start [device_id]` | 开始记录用户真实手指手势（tap/长按/滑动/多指），自动做触摸面板→显示像素标定；`device_id` 在只有一台设备时可省 |
| `record gestures stop` | `record gestures stop [device_id]` | 停止记录，打印手势序列（类型/参数/耗时）与产物目录（`outputs/recordings/<时间戳>/`） |
| `record gestures status` | `record gestures status [device_id]` | 查看当前是否在录制及已捕获手势数 |

## 调试开关（debug）

| 命令 | 说明 |
|------|------|
| `debug on` | 打开调试落盘（截图标注、识别候选、trace，写入 `outputs/debug/`） |
| `debug off` | 关闭调试落盘 |
| `debug status` | 查看当前调试开关状态 |
