# YOLO 模型库（目标检测识别通道）

这里放**训练好的 YOLO 模型**（`.onnx`），供第四条感知通道用 onnxruntime 推理——在画面上
**检测并分类**目标。模板匹配换分辨率/换皮肤就失配、对遮挡也脆；YOLO 抗位移·缩放·遮挡，
还能直接告诉你"这是哪一类"。

一句话约定：**把 `.onnx` 丢进本目录 + 在 `models.json` 里登记一条，它就能按名字用起来。**

> **本通道在模型就位前是惰性的**：没有 `task/models/yolo.onnx` 时 `YoloDetector.available()` 返回
> False，`detect_objects` / `yolo` 识别节点自动让位给文字/模板通道，不报错。

## 怎么用

1. **训练 + 导出**（离线做，本项目运行时只推理，不带 PyTorch）：

   ```bash
   # 标注截图 → 用 ultralytics 训练 → 导出 onnx
   yolo detect train data=ui_icons.yaml model=yolo11n.pt imgsz=640
   yolo export model=runs/detect/train/weights/best.pt format=onnx
   ```

   把导出的 `best.onnx` 放成 `task/models/yolo.onnx`（默认模型），或起个别的文件名当
   命名模型；也可在 `config.yaml` 用 `yolo.model` 指向本目录之外的路径。
   完整流水线（预标注 → 纠错 → 合并数据集 → 训练部署）见 `training/README.md`。
2. **类别名**：ultralytics 导出的 onnx **自带类别名**（元数据里），无需另配；想覆盖就在
   `config.yaml` 写 `yolo.classes: [button, icon, ...]`（按类别 id 顺序）。
3. **推理**：
   - 即时检测：MCP `detect_objects(device_id, classes=["button"])` → 返回带 `label`/`center` 的框，
     `center` 可直接 `click`；`list_yolo_classes()` 看模型认识哪些类。
   - 任务门控：节点 `recognition` 写 `{"type": "yolo", "label": "button", "conf": 0.25}`，
     配 `action.target = "recognized"` 点击命中框中心；省略 `label` = 检测到任意目标即命中。

## 多模型（一个检测域一个 onnx）

| 角色 | 文件 | 怎么引用 |
| --- | --- | --- |
| 默认模型 | `yolo.onnx` | 省略 `model` 参数即用它 |
| 命名模型 | `<名字>.onnx`（如 `ui_icons.onnx`、`objects.onnx`） | `model="ui_icons"` |

不同检测域**分文件不合并**：某个模型可能正被跑着的任务依赖，合并后另一个域每次扩数据都要
连带重训它；且合并要求把 A 域素材里出现的 B 域目标也补标——漏一个就是在教模型"这类=背景"
（未标注即负监督）。理由与训练流程见 `training/README.md`。

用法：

- MCP：`detect_objects(device_id, model="ui_icons", classes=["icon"])`；
  `list_yolo_classes(model="ui_icons")`（回包里的 `models` 列出所有已注册模型名）。
- 任务节点：`{"type": "yolo", "model": "ui_icons", "label": "icon", "conf": 0.3}`；
  省略 `model` = 默认模型，**旧任务 JSON 和旧调用行为不变**。
- **模型表在进程启动时建好**：新丢进来的 `.onnx` 要**重启 MCP server / CLI** 才会被发现
  （模型文件本身仍是懒加载，注册表只记路径）。
- **不用配置也能用**：本目录下除默认模型外的每个 `*.onnx` 都会按文件名自动注册
  （`objects.onnx` → `"objects"`），和 `task/templates/` 一样是丢进去就生效——本项目
  `config.yaml` 本就是可选的，只认配置会让「没写配置的机器上按名字取模型」直接失效。
  `config.yaml` 的 `yolo.models.<名字>` 只用来**调参**（conf/iou/input_size/providers，
  未配的键继承默认模型）或指向本目录之外的路径。
- 类别名以 onnx 元数据为准，节点按**名字**引用——所以给模型加类别不会打断已有任务。

## `models.json`（版本 manifest）

本目录的 `.onnx` 是**本机资产、不入库**（见 `.gitignore`），仓库里只留这份 manifest，
用来说明「这台机器上该有哪些模型、各是哪一版」。结构是**文件名 → 元数据对象**：

```json
{
  "yolo.onnx": {
    "version": "v2",
    "date": "2026-01-31",
    "notes": "UI 通用控件 2 类 button/icon（yolo11n，600 帧/860 框），mAP50 0.99。",
    "classes": ["button", "icon"],
    "training_ref": "<数据集或训练 run 的标识>"
  },
  "objects.onnx": {
    "version": "v1",
    "date": "2026-02-05",
    "notes": "场景内可交互物件检测。",
    "classes": ["crate", "door"]
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `version` | 建议 | 版本号，随模型文件一起递增 |
| `date` | 建议 | 该版本的部署日期 |
| `notes` | 建议 | 训练数据规模 / 类别 / 量化指标 / 已知短板 |
| 其它字段 | 可选 | 如 `classes` / `metrics` / `training_ref`，随便加，读取端原样透传 |

代码侧只有三个契约（`perception/yolo_detector.py`）：

- 顶层必须是一个 JSON 对象，键是**模型文件名**（含 `.onnx` 后缀），值是任意对象；
- `YoloRegistry.model_info(name)` 返回 `{"path": ..., "manifest": <该文件的条目或 {}>}`，
  `YoloRegistry.manifest()` 返回整份表，MCP `list_yolo_classes()` 顺带把 `version`/`date`/`notes`
  带回去；
- **manifest 纯附加，永不阻断加载**：整份文件缺失、JSON 写坏、或某个模型没有条目，
  都只记 warning/debug 并当作 `{}` 处理，模型照常加载（`load_model_manifest`）。

所以**更新任意模型文件时同步更新对应条目**——这是版本信息的唯一数据源，写没写不影响能不能跑，
但影响下次有人问"当前部署的是哪一版"时能不能答上来。

## 约定

- **模型文件不入库**（`.gitignore` 忽略 `task/models/*.onnx`）：模型是本机资产，各机器按
  `training/README.md` 自己训练/部署，仓库只保留本目录 + 本 README + `models.json`。
- 推理**默认走 CPU（onnxruntime）**，和 OCR 一致，换机 clone 即跑、不碰 CUDA；有硬件的机器可用
  `config.yaml` 的 `yolo.providers` 换执行后端（例：`["DmlExecutionProvider", "CPUExecutionProvider"]`，
  给逐帧推理这类高频场景用）。**带回退链**：当前 onnxruntime 装不出来的 provider 会被逐个过滤并记
  warning（不静默），列表末尾始终自动兜底 `CPUExecutionProvider`，全部不可用则回落纯 CPU——配置写错
  只会退回慢一点，不会让 YOLO 通道挂掉。模型默认输入 640，引擎读 onnx 输入尺寸自适应。
- 假设是 **YOLOv8 / v11 的 onnx 输出格式**（`[1, 4+nc, N]`，框为 letterbox 像素的 cx,cy,w,h，
  类别分已 sigmoid）。其它版本/自定义头需改 `perception/yolo_detector.py::_postprocess`。

> 本目录只放模型，不放代码。推理逻辑见 `perception/yolo_detector.py`。
