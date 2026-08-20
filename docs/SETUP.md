# 环境搭建手册

本文档说明如何从零搭建 AutoPlayQA 的运行环境。AutoPlayQA 本身**不调用任何大模型**，运行本项目不需要任何 API Key；`config.yaml` 缺省时全部走内置默认值，克隆仓库后即可直接启动。

## 目录

- [环境要求](#环境要求)
- [Python 环境](#python-环境)
- [依赖安装](#依赖安装)
- [adb 配置](#adb-配置)
- [配置文件](#配置文件)
- [首次启动验证](#首次启动验证)
- [接入方资产（可选）](#接入方资产可选)
- [常见问题](#常见问题)

## 环境要求

| 项目 | 要求 | 说明 |
|------|------|------|
| 操作系统 | Windows | 项目当前面向 Windows 开发/运行 |
| Python | 3.11 | conda / venv 均可，见下文 |
| Android 设备 | 模拟器或真机，已开启 USB 调试 | 通过 ADB 连接 |
| Android SDK platform-tools | 含 `adb.exe` | 需能在命令行直接调用 `adb` |

## Python 环境

用 conda 或 venv 都可以，选一种即可。**不要用系统自带 / Microsoft Store 的 Python**（这类发行版常常是占位 stub，无法正常安装依赖）。

### 方式一：conda

```powershell
conda create -n autoplayqa python=3.11 -y
conda activate autoplayqa
```

### 方式二：venv

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

## 依赖安装

```powershell
pip install -r requirements.txt
```

`requirements.txt` 内容与用途：

| 依赖 | 用途 | 是否可选 |
|------|------|----------|
| `PyYAML` | 解析 `config.yaml` | 必需 |
| `Pillow` | 图像读写与裁剪 | 必需 |
| `numpy` | 像素/数组运算（像素差分、灰度统计等） | 必需 |
| `rapidocr-onnxruntime` | 本地 OCR 引擎（懒加载，首次用到才初始化） | 必需（OCR 识别通道依赖它） |
| `onnxruntime` | rapidocr 与 YOLO 检测器的推理后端 | 必需（无模型时 YOLO 通道自动让位） |
| `opencv-python` | 模板匹配（`template`）/ ORB 特征匹配（`feature`） | 必需 |
| `av` | scrcpy 帧流后端的本地 H.264 解码 | 必需（scrcpy 不可用会自动回退 `screencap`） |
| `mcp` | MCP 服务器（`mcp_server.py`，FastMCP/stdio） | 只用本地 CLI 可不装，但默认随依赖一起装 |
| `requests` | findings 结果推送（飞书机器人 / 通用 webhook） | 可选：只在配置了 `findings.notifiers` 时用到；未安装时 notifier 懒加载失败会自动降级为"不推送"，不影响其余功能 |
| `pytest` | 跑单元测试 | 开发/测试用 |

> YOLO 目标检测通道本身不需要额外安装任何深度学习框架（不依赖 PyTorch），复用的就是 `onnxruntime` 做推理；`task/models/` 下没有放 `.onnx` 模型文件时，该通道自动报告不可用，不会报错中断。

## adb 配置

adb 需要能在命令行直接调用。若不在系统 PATH 中，每次启动前手动加入（以 Android Studio 默认安装路径为例，实际路径按本机 SDK 安装位置调整）：

```powershell
$env:PATH = "$env:LOCALAPPDATA\Android\Sdk\platform-tools;$env:PATH"
```

也可以把 `platform-tools` 目录永久加入系统环境变量 PATH，这样每次开新终端都不用重复设置。

验证：

```powershell
adb devices
```

能看到设备列表（哪怕是空列表，只要不报错）即说明 adb 可用。

## 配置文件

项目根目录的 `config.yaml.example` 是完整的配置模板，包含所有配置段的默认值与用途说明（`app`/`adb`/`capture`/`recording`/`replay_cache`/`yolo`/`scene`/`engine`/`lint`/`debug`/`findings`/`sentinel`/`execution`）。

```powershell
copy config.yaml.example config.yaml
```

**不复制也能跑**：`core/config.py` 在找不到 `config.yaml` 时返回空配置，项目每一处读取配置的地方都带了合理默认值（见 `config.yaml.example` 里每个键旁边的注释）。只有需要偏离默认行为时才需要建这个文件并编辑对应字段，比如：

- 想用无线 adb 自动连接 → 编辑 `adb.wireless` 列表；
- 想换回精确像素截图 → `capture.backend: screencap`；
- 想收到 findings 结果推送 → 配置 `findings.notifiers`；
- 想收紧任务保存的 lint 检查 → `lint.strict: true`。

## 首次启动验证

```powershell
# 交互式 CLI（确保 adb 已在 PATH）
python main.py

# 或启动 MCP 服务器（stdio），交给 Claude Code / Codex 驱动
python mcp_server.py

# 跑单元测试（不依赖真机，subprocess 全部 mock）
python -m pytest tests/ -v
```

CLI 启动后输入 `device list` 应能看到已连接的设备；输入 `help` 查看全部命令（完整命令手册见 `docs/CLI_COMMANDS.md`）。

## 接入方资产（可选）

框架本身不绑定任何游戏，以下目录默认是空的，由接入方（被测游戏所在项目）按需提供，不影响框架本身跑起来：

| 目录/机制 | 内容 | 缺失时的行为 |
|------|------|------|
| `task/task_definitions/` | 任务 JSON（含 `common/` 共享节点、`suites/` 套件） | 空目录，`task list` 返回空列表 |
| `task/templates/` | OpenCV 模板匹配图库（`template`/`feature` 识别通道共用） | `find_template` 返回未找到，不报错 |
| `task/models/` | YOLO 模型（`.onnx`，默认路径 `task/models/yolo.onnx`） | YOLO 通道报告 `available: False`，不猜测 |
| `perception.scene_classifier.register_scene_probe` | 场景分类标签探针（业务方注册） | 内置只有 `blank`（近黑/息屏/空帧），其余场景一律 `unknown` |

## 常见问题

**Q: `uiautomator dump` 在某些 ROM 上没有任何输出？**
部分 ROM（例如深度定制的国产 ROM）`uiautomator dump /dev/tty` 走 tty 直接输出会失败或为空。`perception/ui_dump_matcher.py` 已内置自动回退：改用"写文件再 `adb pull`"的方式取 dump，无需手动处理。

**Q: 游戏画面里点不到东西，`ui_dump` 返回很少节点甚至没有？**
很多游戏用单一 Surface 渲染整个界面，uiautomator 天然看不到里面的控件节点。这种情况下识别应该走 `ocr`（配合 `roi` 缩小识别范围）而不是 `ui_text`；`find_text` 工具本身也会自动按 dump→OCR 的顺序两级兜底。

**Q: scrcpy 帧流用不了怎么办？**
默认截图后端是 scrcpy 常驻 H.264 流（`capture.backend: scrcpy`），任何一次失败都会自动回退到 `screencap`，连续失败会整体切换（"latch off"）到 `screencap`，不会中断任务运行。如果设备环境本来就不支持 scrcpy，或者需要绝对精确的像素（比如很紧的像素差分/`blank_screen` 阈值），可以在 `config.yaml` 里把 `capture.backend` 直接设为 `screencap`。

**Q: adb 命令卡住不动？**
`adb.timeout_s`（默认 30 秒）是单次阻塞 adb 调用的硬上限，超时后自动走既有回退链或返回明确错误，不会无限等待。个别很慢的模拟器可以适当调大。

**Q: 无线 adb 怎么连？**
先用 USB 连接执行 `enable_wireless`（CLI: `device tcpip <device_id>`）切到 TCP/IP 模式拿到地址，再 `connect_device`/`device connect <ip:port>` 连接；Android 11+ 的无线调试配对码场景用 `pair_device`/`device pair <ip:pairing_port> <code>`。把地址写进 `config.yaml` 的 `adb.wireless` 列表后，每次启动会自动尝试连接。
