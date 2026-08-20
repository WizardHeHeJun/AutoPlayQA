# YOLO Viewer

真机 YOLO 检出的肉眼验证工具。连真机 scrcpy 抓帧，实时跑 YOLO 检测（全类）检框 +
中心准星 + 顶部 OCR，在一个 cv2 窗口里叠加显示，供人眼判断检出好坏
（例如调低 conf 阈值能不能看见弱检出、框跟得准不准）。

**偶发使用**：平时用不上，只在需要验证 YOLO 检出效果时才拉起，不挂在主流程
（`main.py` / `mcp_server.py`）上。

## 前置条件

- 项目 conda 环境（Python 3.11）及其依赖（onnxruntime / opencv / PIL）。
- adb 可用（默认从 `%LOCALAPPDATA%\Android\Sdk\platform-tools` 加载，可用
  `ADB_DIR` 环境变量覆盖）。
- 已通过 adb 连接一台真机（`adb devices` 能看到设备号）。
- `task/models/` 下部署了一个 YOLO `.onnx` 模型（默认读 `task/models/yolo.onnx`，
  也可用 `--model` 指向别的文件）。模型不存在时窗口照样起，只是一个框都检不出来。

## 启动方式

```powershell
# 方式一：启动器（推荐，免记路径，参数透传）
tools\yolo_viewer\run.bat
tools\yolo_viewer\run.bat --device XXXXXXXX --conf 0.15

# 项目解释器不在 PATH 时先告诉启动器用哪个
set PYTHON=<conda-envs>\game_automation\python.exe

# 方式二：直接用项目环境解释器
<python> tools\yolo_viewer\yolo_viewer.py --device XXXXXXXX
```

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--device` | `ADB_DEVICE` 环境变量 → `adb devices` 第一台 | 真机设备号 |
| `--model` | `<repo>/task/models/yolo.onnx` | YOLO 模型路径 |
| `--conf` | `0.20` | 初始 conf 阈值 |
| `--hide` | 空 | 隐藏这些类的框（类名，可多个），把刷屏的干扰类先关掉 |
| `--backend` | `scrcpy` | 抓帧后端；`screencap` 更清晰但更慢，细看检出用它 |

`-h` / `--help` 查看完整用法。

## 交互键

窗口需处于前台（鼠标点一下窗口再按键）：

| 键 | 作用 |
| --- | --- |
| `q` / `ESC` | 退出（干净释放 scrcpy 流 + 关窗口） |
| `+` | conf 阈值 +0.05（上限 0.80），立即生效重画 |
| `-` | conf 阈值 -0.05（下限 0.05），立即生效重画 |
| `s` | 把当前带标注的整帧存到 `outputs/yolo_viewer/shots_marked/<时间戳>.jpg` |
| `h` | 切换 `--hide` 指定的类是否显示 |

## 窗口内容

- 实时画面（自适应缩放到屏幕 90% 内，保持手机原生长宽比，不硬拉伸）
- 每个 YOLO 检测框 + `类名 conf` 文字，不同类不同颜色（按类名哈希取色）
- 左上角 HUD：conf 阈值 / fps / 每类检出计数 / 顶部 OCR 文本
- 屏幕中心准星十字
