"""YOLO 训练 → 校验 → 导出 onnx → 部署到 task/models/yolo.onnx（一条命令）。

离线工具：只在**装了 torch/ultralytics 的训练环境**里跑，**不被项目运行时代码 import**。
项目运行时只用 onnxruntime 推理，不依赖 PyTorch/ultralytics——所以训练环境要和项目环境
分开，别把 torch 装进项目环境。

用法（`<python>` = 训练环境的解释器）：
    <python> training\\train_and_export.py --check
    <python> training\\train_and_export.py --name train_v2

也可以从别的环境触发：设环境变量 `YOLO_TRAIN_PYTHON=<训练环境解释器>`，脚本会自动切过去
重跑自己（带标志位防死循环）。

完整流程见 training/README.md。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 训练环境解释器。默认就是当前解释器（直接用训练环境的 python 跑本脚本）；设了
#: YOLO_TRAIN_PYTHON 时脚本会自动切过去重跑，方便从项目环境一键触发训练。
#: 不写死绝对路径：这是每台机器各不相同的本机配置，不是仓库资产。
YOLO_TRAIN_PY = Path(os.environ.get("YOLO_TRAIN_PYTHON") or sys.executable)

DEFAULT_DATA = ROOT / "outputs" / "yolo_dataset" / "data.yaml"
RUNS_DIR = ROOT / "outputs" / "yolo_runs"
BACKUP_DIR = RUNS_DIR / "_deployed_backup"
DEPLOY_PATH = ROOT / "task" / "models" / "yolo.onnx"
#: 预训练权重的本地兜底目录：这里已经有 yolo11n.pt 之类时直接复用，免联网重下。
FALLBACK_WEIGHTS = ROOT / "outputs" / "yolo_work"

REEXEC_FLAG = "AUTOPLAYQA_YOLO_REEXEC"
#: 默认运行名前缀（`--name` 不给时自动排号 <前缀>_v1、_v2 ...）
RUN_PREFIX = "train"

# Windows 控制台默认 cp936，中文提示会变乱码（ultralytics 自己 import 时才改成 utf-8，
# 那之前打印的行就花了）。这里先手动统一。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # 非 TextIOWrapper / 已重定向
        pass

# 增强参数（改前先想清楚）：默认按「固定镜头、目标不会镜像出现」的场景取值，
# 所以几何增强压到很轻（不翻转、只 ±3° 抖动），靠色彩抖动覆盖光照差异。
# 目标左右都可能出现（角色/可移动物件）时用 --fliplr 打开翻转。
AUG = dict(
    fliplr=0.0, flipud=0.0, degrees=3.0, scale=0.4, translate=0.1,
    hsv_h=0.02, hsv_s=0.7, hsv_v=0.5, mosaic=1.0, close_mosaic=15, erasing=0.0,
)


# ---------- 环境自检 ----------

def ensure_yolo_train_env() -> None:
    """不在训练环境里就用 YOLO_TRAIN_PYTHON 重跑本脚本（带标志位防死循环）。"""
    same = os.path.normcase(sys.executable) == os.path.normcase(str(YOLO_TRAIN_PY))
    if same:
        return
    if os.environ.get(REEXEC_FLAG):
        sys.exit(f"[FATAL] 重定向后仍不在训练环境：{sys.executable}")
    if not YOLO_TRAIN_PY.is_file():
        sys.exit(
            f"[FATAL] 找不到训练环境解释器 {YOLO_TRAIN_PY}（来自 YOLO_TRAIN_PYTHON）\n"
            "        建一个装了 ultralytics 的独立环境，例如：\n"
            "          conda create -n yolo_train python=3.11 -y\n"
            "          <该环境的 python> -m pip install ultralytics\n"
            "        然后把 YOLO_TRAIN_PYTHON 指向它，或直接用它跑本脚本"
        )
    print(f"[env] 当前解释器 {sys.executable} 不是训练环境，转由 {YOLO_TRAIN_PY} 执行\n",
          flush=True)
    env = dict(os.environ, **{REEXEC_FLAG: "1"})
    raise SystemExit(subprocess.call([str(YOLO_TRAIN_PY), str(Path(__file__).resolve()),
                                      *sys.argv[1:]], env=env))


def probe_torch() -> dict:
    """返回 torch 版本 / CUDA 可用性 / GPU 名称，并打印结论。"""
    try:
        import torch
    except ImportError:
        sys.exit("[FATAL] 训练环境里没有 torch，先按 training/README.md 的环境准备一节安装")
    info = {
        "version": torch.__version__,
        "cuda": bool(torch.cuda.is_available()),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "cuda_build": torch.version.cuda,
    }
    print(f"[env] python      : {sys.executable}")
    print(f"[env] torch       : {info['version']} (built with CUDA {info['cuda_build']})")
    if info["cuda"]:
        print(f"[env] GPU        : {info['gpu']}  ->  将用 GPU 训练")
    else:
        print("[WARN] torch 不可用 CUDA（当前很可能是 CPU 版）。CPU 也能训，但只有几十帧的")
        print("[WARN] 小数据集就要十几分钟，数据集一大就难以忍受。装 CUDA 版 torch：")
        print(f"[WARN]   {YOLO_TRAIN_PY} -m pip uninstall -y torch torchvision")
        print(f"[WARN]   {YOLO_TRAIN_PY} -m pip install torch torchvision "
              "--index-url https://download.pytorch.org/whl/cu128")
        print("[WARN] （cu 标签以 https://pytorch.org/get-started/locally/ 当前给出的为准）")
    try:
        import ultralytics
        print(f"[env] ultralytics : {ultralytics.__version__}")
    except ImportError:
        sys.exit(f"[FATAL] 没装 ultralytics：{YOLO_TRAIN_PY} -m pip install ultralytics")
    return info


def probe_dataset(data_yaml: Path) -> dict:
    """校验数据集结构，统计帧数 / 框数 / 类别分布。"""
    import yaml

    if not data_yaml.is_file():
        sys.exit(f"[FATAL] 数据集描述文件不存在：{data_yaml}")
    cfg = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    base = Path(cfg.get("path") or data_yaml.parent)
    names = cfg.get("names") or {}
    if isinstance(names, list):
        names = dict(enumerate(names))
    stats = {"names": names, "splits": {}, "boxes": Counter(), "frames": 0, "orphans": []}
    for split in ("train", "val"):
        rel = cfg.get(split)
        if not rel:
            sys.exit(f"[FATAL] data.yaml 缺少 {split} 段：{data_yaml}")
        img_dir = (base / rel).resolve()
        lbl_dir = Path(str(img_dir).replace(os.sep + "images" + os.sep,
                                            os.sep + "labels" + os.sep))
        if not img_dir.is_dir():
            sys.exit(f"[FATAL] 图片目录不存在：{img_dir}")
        imgs = sorted(p for p in img_dir.iterdir()
                      if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        pos = 0
        for img in imgs:
            lbl = lbl_dir / (img.stem + ".txt")
            if not lbl.is_file():
                stats["orphans"].append(str(img))
                continue
            lines = [ln for ln in lbl.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                pos += 1
            for ln in lines:
                stats["boxes"][int(ln.split()[0])] += 1
        stats["splits"][split] = {"frames": len(imgs), "positives": pos,
                                  "negatives": len(imgs) - pos}
        stats["frames"] += len(imgs)

    print(f"[data] {data_yaml}")
    print(f"[data] 类别 {len(names)} 个：" + ", ".join(f"{i}:{n}" for i, n in sorted(names.items())))
    for split, s in stats["splits"].items():
        print(f"[data] {split:<5} 帧 {s['frames']:>4}（有目标 {s['positives']}，负样本 {s['negatives']}）")
    print("[data] 框数：" + ", ".join(f"{names.get(i, i)}={c}" for i, c in sorted(stats["boxes"].items()))
          + f"  合计 {sum(stats['boxes'].values())}")
    if stats["orphans"]:
        print(f"[WARN] {len(stats['orphans'])} 张图没有对应 label txt（会被 ultralytics 当背景）：")
        for p in stats["orphans"][:5]:
            print(f"[WARN]   {p}")
    thin = [names.get(i, i) for i, c in stats["boxes"].items() if c < 10]
    if thin:
        print(f"[WARN] 样本过少的类别（<10 框，mAP50-95 会很难看）：{', '.join(map(str, thin))}")
    return stats


# ---------- 训练 / 导出 / 部署 ----------

def next_run_name() -> str:
    """扫 outputs/yolo_runs 下的 <RUN_PREFIX>_vN，取下一个序号。"""
    used = [int(m.group(1)) for d in RUNS_DIR.glob(f"{RUN_PREFIX}_v*")
            if d.is_dir() and (m := re.fullmatch(rf"{RUN_PREFIX}_v(\d+)", d.name))]
    return f"{RUN_PREFIX}_v{max(used, default=0) + 1}"


def resolve_weights(model: str) -> str:
    """预训练权重：优先本地已有的副本，避免 ultralytics 联网重下。"""
    if Path(model).is_file():
        return str(Path(model).resolve())
    local = FALLBACK_WEIGHTS / model
    if local.is_file():
        print(f"[train] 复用本地预训练权重 {local}")
        return str(local)
    print(f"[train] 本地没有 {model}，ultralytics 将自动下载")
    return model


def train(args, device: str) -> Path:
    from ultralytics import YOLO

    run_dir = RUNS_DIR / args.name
    aug = dict(AUG)
    if args.fliplr is not None:
        aug["fliplr"] = float(args.fliplr)
    if args.mosaic is not None:
        aug["mosaic"] = float(args.mosaic)
        aug["close_mosaic"] = 0 if aug["mosaic"] == 0 else aug["close_mosaic"]
    print(f"\n=== 训练 {args.name}（device={device}, imgsz={args.imgsz}, "
          f"batch={args.batch}, epochs={args.epochs}, fliplr={aug['fliplr']}）===")
    YOLO(resolve_weights(args.model)).train(
        data=str(args.data), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
        device=device, workers=args.workers, cache=True, patience=args.patience,
        project=str(RUNS_DIR), name=args.name, exist_ok=True, seed=0, plots=True,
        **aug,
    )
    best = run_dir / "weights" / "best.pt"
    if not best.is_file():
        sys.exit(f"[FATAL] 训练结束但没有 {best}")
    return best


def validate(best: Path, args, device: str) -> dict:
    from ultralytics import YOLO

    print(f"\n=== 校验 {best} ===")
    m = YOLO(str(best)).val(
        data=str(args.data), imgsz=args.imgsz, device=device, workers=args.workers,
        project=str(RUNS_DIR), name=f"{args.name}_val", exist_ok=True,
    )
    per_class = {m.names[i]: float(ap) for i, ap in zip(m.box.ap_class_index, m.box.ap50)}
    print(f"[val] mAP50={m.box.map50:.4f}  mAP50-95={m.box.map:.4f}")
    for name, ap in per_class.items():
        print(f"[val]   {name:<14} mAP50={ap:.4f}")
    return {"map50": float(m.box.map50), "map": float(m.box.map), "per_class": per_class}


def export_onnx(best: Path, imgsz: int) -> Path:
    from ultralytics import YOLO

    print(f"\n=== 导出 onnx（opset=12, simplify=False）===")
    # simplify=False：perception/yolo_detector.py 期望的是原生 YOLOv8/v11 输出格式
    # [1, 4+nc, N]；开 simplify 需要额外装 onnxslim，且没有必要。
    out = YOLO(str(best)).export(format="onnx", imgsz=imgsz, opset=12, simplify=False)
    path = Path(out)
    print(f"[export] {path}  ({path.stat().st_size / 1e6:.1f} MB)")
    return path


def deploy(onnx: Path, target: Path = DEPLOY_PATH) -> None:
    print(f"\n=== 部署到 {target} ===")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"{target.stem}_{stamp}.onnx"
        shutil.copy2(target, backup)
        print(f"[deploy] 旧模型已备份 -> {backup}")
    shutil.copy2(onnx, target)
    print(f"[deploy] {onnx} -> {target}")


def summarize(args, data_stats: dict, metrics: dict | None, deployed: bool) -> None:
    names = data_stats["names"]
    cls_list = "/".join(names[i] for i in sorted(names))
    boxes = sum(data_stats["boxes"].values())
    line = (f"模型：{Path(getattr(args, 'deploy_to', DEPLOY_PATH)).name} 更新为 {args.name}"
            f"（{Path(args.model).stem}，"
            f"{len(names)} 类 {cls_list}，{data_stats['frames']} 帧 / {boxes} 框"
            + (f"，mAP50 {metrics['map50']:.3f} / mAP50-95 {metrics['map']:.3f}" if metrics else "")
            + f"，{datetime.now():%Y-%m-%d}）")

    summary = RUNS_DIR / args.name / "deploy_summary.txt"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(line + "\n", encoding="utf-8")

    print("\n" + "=" * 72)
    print("下一步：部署验证（见 training/README.md 的部署验证一节）")
    print("  1) 单测（用**项目环境**解释器，不是训练环境）：")
    print("     <python> -m pytest tests/test_yolo_detector.py -v")
    print("  2) 真机抽查（MCP）：list_yolo_classes() 看类别，")
    print("     detect_objects(device_id, conf=0.25) 对着实机画面核对框和 center")
    print("  3) 回归：跑一个用 yolo 识别节点的任务，确认命中率没掉")
    if deployed:
        print("\nmanifest 条目建议（task/models/models.json 要记训练数据规模/类别/日期）：")
        print(f"  {line}")
        print(f"  （已写入 {summary}）")
    print("=" * 72)


def main() -> None:
    ensure_yolo_train_env()
    p = argparse.ArgumentParser(description="YOLO 训练→导出→部署流水线（AutoPlayQA 离线工具）")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help=f"数据集 yaml（默认 {DEFAULT_DATA}）")
    p.add_argument("--model", default="yolo11n.pt", help="预训练权重/模型规格，如 yolo11n.pt / yolo11s.pt")
    p.add_argument("--name", default=None, help=f"运行名（默认自动取下一个 {RUN_PREFIX}_vN）")
    p.add_argument("--epochs", type=int, default=120)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=None, help="默认 GPU=16 / CPU=8")
    p.add_argument("--workers", type=int, default=None, help="默认 GPU=4 / CPU=0（Windows 上别调太高）")
    p.add_argument("--patience", type=int, default=30)
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--check", action="store_true", help="只做环境+数据集自检，不训练")
    p.add_argument("--export-only", action="store_true", help="跳过训练，用已有 run 的 best.pt 导出部署")
    p.add_argument("--no-deploy", action="store_true", help="导出后不覆盖部署路径")
    p.add_argument("--deploy-to", type=Path, default=DEPLOY_PATH,
                   help=f"部署目标 onnx 路径（默认 {DEPLOY_PATH}）。训练第二个检测域的模型"
                        "（如 task/models/<名字>.onnx）时指过去，别覆盖默认模型")
    p.add_argument("--cache", default="ram", choices=["ram", "disk", "none"],
                   help="ultralytics 图片缓存。默认 ram（最快）；本机内存被别的进程吃掉时"
                        "改 disk（可用提交内存只剩几 GB 时 ram 缓存必 OOM）")
    p.add_argument("--mosaic", type=float, default=None,
                   help="mosaic 概率，覆盖 AUG 默认的 1.0。mosaic 每样本要一块 "
                        "(imgsz*2)² 的画布，本机提交内存吃紧时会 OOM；"
                        "数据集本身已有合成增广时关掉它损失有限")
    p.add_argument("--fliplr", type=float, default=None,
                   help="水平翻转概率，覆盖 AUG 默认的 0。带烘焙光照/文字的静态目标不能镜像，"
                        "但左右都可能出现的目标镜像合法，这类数据集给 0.5")
    args = p.parse_args()

    print("=" * 72)
    print("AutoPlayQA · YOLO 训练流水线")
    print("=" * 72)
    torch_info = probe_torch()
    data_stats = probe_dataset(args.data.resolve())

    if args.device == "cuda" and not torch_info["cuda"]:
        sys.exit("[FATAL] 指定了 --device cuda 但 torch 用不了 CUDA，先按上面的提示装 CUDA 版 torch")
    device = "0" if (args.device in ("auto", "cuda") and torch_info["cuda"]) else "cpu"
    on_gpu = device != "cpu"
    if args.batch is None:
        args.batch = 16 if on_gpu else 8
    if args.workers is None:
        args.workers = 4 if on_gpu else 0
    args.cache = False if args.cache == "none" else args.cache

    if args.check:
        print("\n[check] 自检完成（未训练）。去掉 --check 即开始训练。")
        return

    if args.name is None:
        args.name = next_run_name()
        print(f"\n[run] 未指定 --name，自动使用 {args.name}")

    metrics = None
    if args.export_only:
        best = RUNS_DIR / args.name / "weights" / "best.pt"
        if not best.is_file():
            sys.exit(f"[FATAL] --export-only 但找不到 {best}")
        print(f"\n[run] --export-only：直接用 {best}")
    else:
        best = train(args, device)
        metrics = validate(best, args, device)

    onnx = export_onnx(best, args.imgsz)
    if args.no_deploy:
        print(f"\n[deploy] --no-deploy：模型留在 {onnx}，未覆盖 {args.deploy_to}")
    else:
        deploy(onnx, args.deploy_to.resolve())
    summarize(args, data_stats, metrics, deployed=not args.no_deploy)


if __name__ == "__main__":
    main()
