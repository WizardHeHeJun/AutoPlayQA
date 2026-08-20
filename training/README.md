# training/ — YOLO 模型训练流水线（离线工具）

把「**采数据 → 预标注 → 人工纠错 → 并集 → 训练导出 → 部署验证**」沉淀成一条可复跑的流水线。

> **这是离线工具线，不属于运行时四层架构**：`training/` 下的任何东西都**不得被项目运行时代码
> import**（`perception/` / `task/` / `mcp_server.py` 只用 onnxruntime 推理 `task/models/*.onnx`，
> 不带 PyTorch、不碰 CUDA）。训练放在**独立的 conda 环境**里做，跟项目环境完全隔离。

下文 `<python>` 是解释器占位符，按所在环境替换：

| 占位 | 指哪个环境 | 用在 |
| --- | --- | --- |
| `<python>` | **项目环境**（onnxruntime + PIL + PyYAML） | `preannotate.py` / `build_increment.py` / pytest |
| `<train-python>` | **训练环境**（torch + ultralytics） | `train_and_export.py` |

| 文件 | 用途 |
| --- | --- |
| `README.md` | 本文档：全流程 SOP |
| `preannotate.py` | 现役模型给新帧**预标注** + 应用人工**纠错编辑表**（跑在项目环境，只要 onnxruntime） |
| `build_increment.py` | 纠错后的新帧**并入旧数据集**出新版（扩类别表 / 划分防泄漏 / 统计告警） |
| `train_and_export.py` | 训练 + 校验 + 导出 onnx + 部署到 `task/models/*.onnx`（一条命令） |

推理侧的用法（识别节点 / MCP 工具 / 类别名来源 / `models.json`）见 `task/models/README.md`，
本文只讲怎么造模型。

---

## ⓪ 闭环总览

> 应用更新加了新界面 / 新物件，或现役模型在某个场景漏检——走这条闭环。
>
> 路线是**闭集模型自举**：现役模型预标注 → 人工只纠错 → 合并重训。数据规模是数百帧、
> 全量重训十几分钟，**旧集全量并训本身就是 100% replay**，不要引入增量蒸馏 / LwF /
> replay 缓冲这类方案，纯属过度设计。

```
① 采帧      任意来源                        截图 / 回放录屏抽帧，人眼粗筛
② 预标注    preannotate.py                  现役 onnx 出预标注 txt + 预览 + 编辑表模板
③ 纠错      人工翻预览 + 填 edits.py         只改错的，不重画对的
④ 应用      preannotate.py --apply-edits    出 labels_final/（全量最终标签）
⑤ 合并      build_increment.py              旧集 + 新帧 -> 新版数据集（自动扩类别表）
⑥ 训练部署  train_and_export.py --deploy-to  训练→导出→备份旧模型→落 task/models/
⑦ 生效验证  重启 MCP + 单测 + 真机抽查
```

---

## ① 数据集布局（三个脚本共同的约定）

标准 ultralytics 检测数据集，`data.yaml` + 平级的 `images/` 与 `labels/`：

```
<dataset>/
├── data.yaml                    # path / train / val + names（类别 id → 名称）
├── images/train/  images/val/   # .png / .jpg / .jpeg
└── labels/train/  labels/val/   # 同名 .txt，YOLO 归一化格式 `cls cx cy w h`
```

```yaml
# data.yaml
path: <dataset 绝对路径>
train: images/train
val: images/val

names:
  0: button
  1: icon
```

三条硬约定，脚本都按它们工作：

- **空 `.txt` = 负样本**，不是"漏标了"。没有目标的画面必须给一个空 txt 参与训练，
  否则模型会满屏误检；**图片缺 txt 会被跳过并告警**，不会被静默当背景吃进去。
- **类别 id 只追加不重排**：任务 JSON 里 `{"type":"yolo","label":...}` 按**名字**引用，
  名字由 ultralytics 写进 onnx 元数据。重排 id 会让旧标签集体错位。
- **帧组不拆**：帧名形如 `<组名>_<序号>`（序号≥2 位），同组的近重复帧整组进 train 或 val。

---

## ② 预标注与纠错（`preannotate.py`）

```powershell
# 预标注：现役模型先把已知类框出来（conf 默认 0.15，宁多勿漏）
<python> training\preannotate.py `
    --model default --frames outputs\dataset_work\incoming `
    --out outputs\dataset_work\pre_v3 --conf 0.15

# 人工纠错：翻 pre_v3\preview\*.jpg（框上有 #序号 和分数），
# 把 pre_v3\edits_template.py 另存为 pre_v3\edits.py 填写，然后：
<python> training\preannotate.py `
    --apply-edits outputs\dataset_work\pre_v3\edits.py --out outputs\dataset_work\pre_v3
```

`--model` 接 `YoloRegistry` 名字（= `task\models\` 下的 `.onnx` 文件名 stem，`default` 是
`task/models/yolo.onnx`）或直接给 `.onnx` 路径。

产物（都在 `--out` 下）：

| 产物 | 说明 |
| --- | --- |
| `labels/` | 预标注 YOLO txt；**空文件 = 负样本** |
| `preview/` | 画框预览 jpg，框上标了 `#序号 类名 分数`，人眼 QC 用 |
| `summary.json` | 每帧检出 + 分数 + 模型/类别表（`--apply-edits` 靠它找回帧目录） |
| `edits_template.py` | 纠错编辑表模板，带**本次运行的真实类别表**与帧名示例 |
| `labels_final/` | `--apply-edits` 的产物：**全量**最终标签，可整目录喂 `build_increment.py` |
| `preview_final/` | `--apply-edits` 的回描图（默认只画被改过的帧，`--preview-all` 全画） |

编辑表三条语义，优先级从高到低：

| 表 | 含义 |
| --- | --- |
| `EDITS` | 帧名 → `[[cls, x1, y1, x2, y2], ...]`，**整帧替换**预标注；坐标是**原图像素**；写 `[]` = 负样本 |
| `DROP` | 帧名 → `[框下标, ...]`，删误检 / NMS 残留重复框；下标就是预览图上的 `#N` |
| 没列出的帧 | 原样沿用预标注 |

关键约定：

- **`--conf` 默认 0.15**：宁可多框让人删，别漏框让人重画。
- **新类别的 cls id 从"旧类数量"起算**：旧集 2 类（0/1）时新类写 `2`，并在下一步用
  `--new-classes` 声明。
- **误检帧收成负样本最提分**，别把它们删掉。
- 编辑表里的帧名拼错会**告警列出**，不会静默丢弃。

---

## ③ 并入旧数据集（`build_increment.py`）

```powershell
<python> training\build_increment.py `
    --base outputs\dataset_v2 `
    --new-images outputs\dataset_work\incoming `
    --new-labels outputs\dataset_work\pre_v3\labels_final `
    --out outputs\dataset_v3 --new-classes dialog
```

| 项 | 说明 |
| --- | --- |
| 旧集 | **原样全量并入**，train/val 划分保持不动（那是人工分层挑过的，随机重划会让稀有类饿死） |
| 新类别 | `--new-classes dialog toast`，只追加在类别表末尾；新标签里出现未声明的 cls id 会**直接报错退出**，不会写出半成品 |
| 划分防泄漏 | 新帧按**帧组**整组进 train 或 val。组名 = 帧名去掉尾部序号（`clip_a_016` → `clip_a`）。⚠️**命名不带序号的散帧无法自动分组**，会各自成组，近重复帧仍可能跨 split——这种素材要么统一命名成同前缀+序号，要么在报告里如实标注指标偏乐观 |
| 帧组撞上旧集 | 新帧的组若旧集里已出现，**钉死到旧集所在的 split**（跨 split 的钉 train），防止 val 素材泄漏进 train |
| 负样本 | 一律留 train，绝不进 val（val 里全是背景帧只会把指标做漂亮） |
| val 比例 | `--val-ratio` 默认沿用旧集比例；**稀有类覆盖优先于比例**——为了让每个类在 val 里至少有一帧，实际 val 可能略超设定值 |
| 混合组 | 组内混有负样本帧时优先不选它进 val；实在没有纯正样本组可用才退化选中，并打 `[WARN]` 说明拆分详情 |
| 划分可复现 | `--seed`（默认 0），同样输入必得同样划分 |
| 重名 | 新帧与旧集帧名冲突时自动改名（`--new-prefix` 可主动加前缀；注意前缀会改变帧组名） |
| 统计告警 | 每类框数 <10 告警（同 `train_and_export.py --check` 口径）、val 里某类 0 框告警、图片缺 txt 告警并跳过 |

---

## ④ 训练 → 导出 → 部署（`train_and_export.py`）

需要**装了 torch + ultralytics 的独立环境**。项目环境永远只要 onnxruntime，
**不要**在项目环境里装 torch/ultralytics。

```powershell
# 环境准备（一次性）
conda create -n yolo_train python=3.11 -y
<train-python> -m pip install ultralytics
# GPU 训练需要 CUDA 版 torch（--check 会告诉你当前是不是 CPU 版）：
<train-python> -m pip uninstall -y torch torchvision
<train-python> -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
#   cu 标签以 https://pytorch.org/get-started/locally/ 当前给出的为准

# 只自检环境 + 数据集，不训练
<train-python> training\train_and_export.py --check

# 训练 + 校验 + 导出 + 部署
<train-python> training\train_and_export.py `
    --data outputs\dataset_v3\data.yaml --name ui_icons_v3 `
    --deploy-to task\models\ui_icons.onnx
```

从项目环境一键触发也行：设 `YOLO_TRAIN_PYTHON=<train-python>`，脚本会自动切过去重跑自己。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--data` | `outputs/yolo_dataset/data.yaml` | 数据集描述 |
| `--model` | `yolo11n.pt` | 预训练权重；`outputs/yolo_work/` 下已有的同名文件会被自动复用免下载 |
| `--name` | 自动 `train_v<N+1>` | 运行名 = `outputs/yolo_runs/<name>/` |
| `--epochs` / `--patience` | 120 / 30 | 早停由 patience 兜底，比手工掐轮数稳 |
| `--imgsz` | 640 | 输入边长；小目标掉点时**先加分辨率再换大模型** |
| `--batch` | GPU 16 / CPU 8 | 显存吃紧就调小 |
| `--workers` | GPU 4 / CPU 0 | Windows 上别调太高 |
| `--device` | `auto` | `auto`/`cuda`/`cpu`；`cuda` 但没 CUDA 会直接报错退出 |
| `--cache` | `ram` | 内存吃紧改 `disk`（可用提交内存只剩几 GB 时 ram 缓存必 OOM） |
| `--mosaic` / `--fliplr` | 覆盖 `AUG` | 见下 |
| `--check` | — | 只自检不训练 |
| `--export-only` | — | 跳过训练，用已有 run 的 `best.pt` 重新导出部署 |
| `--no-deploy` | — | 导出但不覆盖部署路径 |

增强参数写死在脚本的 `AUG` 里，默认按「固定镜头、目标不会镜像出现」取值：`fliplr=flipud=0`、
`degrees=3`，靠 HSV 抖动覆盖光照差异。**目标左右都可能出现时（角色、可移动物件）给
`--fliplr 0.5`**；数据集本身已有合成增广、或本机提交内存吃紧时给 `--mosaic 0`。

导出固定 `opset=12, simplify=False`——这是 `perception/yolo_detector.py::_postprocess` 期望的
原生 YOLOv8/v11 输出格式 `[1, 4+nc, N]`。换了 `--imgsz` **不用**改代码，`YoloDetector` 读 onnx
的输入尺寸自适应。部署前旧模型会先备份到 `outputs/yolo_runs/_deployed_backup/`，**回滚就是把它
拷回去**。

- **数据集小的时候优先扩数据，不是换大模型**。
- 改过标签后建议删掉数据集里的 `labels/*.cache`，避免 ultralytics 用旧缓存。

---

## ⑤ 部署到 `task/models/` 并验证（必做）

```powershell
# 1) 单测：用**项目环境**（不是训练环境）
<python> -m pytest tests/test_yolo_detector.py -v

# 2) 真机抽查（MCP 工具，需先 connect_device）
#    list_yolo_classes()                        -> 类别 id/名称是否是新训的那套
#    detect_objects(device_id, conf=0.25)       -> 框/label/center 是否对得上画面
#    detect_objects(device_id, classes=["icon"]) -> 单类过滤是否正常
```

落位与登记：

1. `--deploy-to` 已经把 onnx 拷进 `task/models/`（默认模型是 `yolo.onnx`；第二个检测域用
   `task/models/<名字>.onnx`，按文件名 stem 自动注册成命名模型）。
2. **重启 MCP server / CLI**：`YoloRegistry` 在进程启动时扫目录建表，新丢进来的 `.onnx`
   不重启不会被发现（模型文件本身仍是懒加载）。
3. **更新 `task/models/models.json` 的对应条目**：`.onnx` 是本机资产不入库，manifest 是仓库里
   唯一记录"这台机器该有哪些模型、各是哪一版"的地方。脚本末尾会打印一行现成的说明
   （训练数据规模 / 类别 / 指标 / 日期），并写到 `outputs/yolo_runs/<name>/deploy_summary.txt`，
   直接抄进 `notes`。字段定义见 `task/models/README.md`。

验证要点：

- 单测是**格式契约**测试（输出张量形状、NMS、letterbox 反算、识别节点校验），模型换了它必须还绿；
  它不验证精度。
- 真机抽查看三件事：**类别表对不对、框位置准不准、`center` 点下去是不是目标**。
- 有 yolo 识别节点的任务跑一遍回归，确认命中率没退化。
- 运行名按 `<前缀>_v<N>` 递增，**别覆盖旧 run 目录**——回滚和对比都要靠它。
- 类别增删是**破坏性变更**：改类别名时要同步 grep `task/task_definitions/` 里的 yolo 节点。

---

## 相关

- 推理侧用法、`models.json` 字段与约定：`task/models/README.md`
- 推理实现：`perception/yolo_detector.py`（`_postprocess` 假设 YOLOv8/v11 输出格式）
- 肉眼验证检出效果：`tools/yolo_viewer/`
