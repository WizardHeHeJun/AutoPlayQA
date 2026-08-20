"""用现役 YOLO 模型给新帧做预标注，并把人工纠错编辑表应用成最终标签。

自举闭环（training/README.md《新增内容 SOP》）的第 ②③ 步：

    现役 onnx 预标注 -> 人眼看预览 -> 写编辑表纠错 -> --apply-edits 出最终标签
                                                        -> build_increment.py 合并

离线工具，跑在**项目环境**（onnxruntime + PIL，不需要 torch/ultralytics）。
下面 `<python>` 指项目 conda 环境的解释器：

    # ② 预标注（conf 默认 0.15：宁可多框让人删，别漏框让人重画）
    <python> training\\preannotate.py ^
        --model default --frames outputs\\dataset_work\\incoming ^
        --out outputs\\dataset_work\\pre_v3

    # ③ 人工纠错：看 <out>/preview/*.jpg，把 <out>/edits_template.py 另存为 edits.py 后填写
    <python> training\\preannotate.py ^
        --apply-edits outputs\\dataset_work\\pre_v3\\edits.py ^
        --out outputs\\dataset_work\\pre_v3

产物（都在 --out 下）：

    labels/           预标注 YOLO txt（`cls cx cy w h` 归一化；**空文件 = 负样本**）
    preview/          画框预览 jpg，人眼 QC 用
    summary.json      每帧检出 + 分数 + 模型/类别表（--apply-edits 靠它找回帧目录）
    edits_template.py 纠错编辑表模板（EDITS / DROP，带用法注释）
    labels_final/     --apply-edits 的产物：**全量**最终标签，直接喂 build_increment.py
    preview_final/    --apply-edits 的产物：被改动帧的回描图（--preview-all 可全画）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许从任意 cwd 直接 python training/preannotate.py
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from perception.yolo_detector import YoloDetector, YoloRegistry  # noqa: E402

# Windows 控制台默认 cp936，中文提示会变乱码（同 train_and_export.py）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # 非 TextIOWrapper / 已重定向
        pass

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
MODEL_DIR = ROOT / "task" / "models"
DEFAULT_MODEL_ONNX = MODEL_DIR / "yolo.onnx"
DEFAULT_CONF = 0.15
DEFAULT_PREVIEW_SCALE = 0.45
MIN_BOX_PX = 2.0  # 小于此的框是纠错时手滑，丢掉而不是写进标签

# 类别 id -> 预览框颜色；超出长度就循环，别再为"加了第 6 类"改代码
PALETTE: Tuple[Tuple[int, int, int], ...] = (
    (255, 70, 70), (60, 200, 255), (70, 255, 130), (255, 215, 40),
    (255, 110, 255), (120, 160, 255), (255, 160, 60), (160, 255, 60),
)

log = logging.getLogger("preannotate")


# ---------- 纯逻辑（可导入、单测覆盖）----------

def color_of(class_id: int) -> Tuple[int, int, int]:
    return PALETTE[int(class_id) % len(PALETTE)]


def iter_frames(frames_dir: Path, limit: Optional[int] = None) -> List[Path]:
    """帧目录下的图片，按文件名排序（确定性）。"""
    frames = sorted(p for p in frames_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    return frames[:limit] if limit else frames


def clamp_box(box: Sequence[float], width: int, height: int) -> Optional[List[float]]:
    """[cls,x1,y1,x2,y2] 裁进画面；退化框返回 None。"""
    cls, x1, y1, x2, y2 = box
    x1, x2 = sorted((float(x1), float(x2)))
    y1, y2 = sorted((float(y1), float(y2)))
    x1, y1 = max(0.0, x1), max(0.0, y1)
    x2, y2 = min(float(width), x2), min(float(height), y2)
    if x2 - x1 < MIN_BOX_PX or y2 - y1 < MIN_BOX_PX:
        return None
    return [int(cls), x1, y1, x2, y2]


def pixel_boxes_to_lines(boxes: Iterable[Sequence[float]], width: int, height: int) -> List[str]:
    """像素框 [cls,x1,y1,x2,y2] -> YOLO 归一化行 `cls cx cy w h`。"""
    lines: List[str] = []
    for box in boxes:
        clamped = clamp_box(box, width, height)
        if clamped is None:
            continue
        cls, x1, y1, x2, y2 = clamped
        lines.append(f"{cls} {(x1 + x2) / 2 / width:.6f} {(y1 + y2) / 2 / height:.6f} "
                     f"{(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}")
    return lines


def lines_to_pixel_boxes(text: str, width: int, height: int) -> List[List[float]]:
    """YOLO 归一化 txt -> 像素框 [cls,x1,y1,x2,y2]（编辑表用的就是像素坐标）。"""
    boxes: List[List[float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        cx, cy, bw, bh = (float(v) for v in parts[1:])
        boxes.append([int(parts[0]), (cx - bw / 2) * width, (cy - bh / 2) * height,
                      (cx + bw / 2) * width, (cy + bh / 2) * height])
    return boxes


def detections_to_pixel_boxes(dets: Iterable[Dict]) -> List[List[float]]:
    """YoloDetector.detect() 的输出 -> 像素框 [cls,x1,y1,x2,y2]。"""
    return [[int(d["class_id"]), *(float(v) for v in d["bbox"])] for d in dets]


def apply_edit_table(stem: str, pre_boxes: List[List[float]],
                     edits: Dict[str, List], drop: Dict[str, List[int]]) -> List[List[float]]:
    """编辑表语义：EDITS 整帧替换（[] = 负样本）> DROP 删指定下标 > 原样沿用预标注。"""
    if stem in edits:
        return [list(b) for b in edits[stem]]
    dropped = set(int(i) for i in drop.get(stem, []))
    return [list(b) for i, b in enumerate(pre_boxes) if i not in dropped]


def load_edit_table(path: Path) -> Tuple[Dict[str, List], Dict[str, List[int]]]:
    """按文件路径加载编辑表 .py，取出 EDITS / DROP（缺失当空表）。"""
    if not path.is_file():
        sys.exit(f"[FATAL] 编辑表不存在：{path}")
    spec = importlib.util.spec_from_file_location(f"_edits_{path.stem}", path)
    if spec is None or spec.loader is None:
        sys.exit(f"[FATAL] 无法加载编辑表：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    edits = dict(getattr(module, "EDITS", {}) or {})
    drop = {k: list(v) for k, v in (getattr(module, "DROP", {}) or {}).items()}
    return edits, drop


def edits_template_text(classes: Dict[int, str], sample_stems: Sequence[str]) -> str:
    """生成纠错编辑表模板正文（带本次运行的真实类别表与帧名示例）。"""
    cls_lines = "\n".join(f"    {i} = {n}" for i, n in sorted(classes.items())) \
        or "    （模型没有类别元数据）"
    consts = "\n".join(f"{_const_name(n)} = {i}" for i, n in sorted(classes.items()))
    ex1 = sample_stems[0] if sample_stems else "帧名（不带扩展名）"
    ex2 = sample_stems[1] if len(sample_stems) > 1 else ex1
    first_cls = _const_name(classes[min(classes)]) if classes else "0"
    return f'''"""人工纠错编辑表（由 preannotate.py 生成的模板，改完另存为 edits.py 再 --apply-edits）。

三条语义，优先级从高到低：

  EDITS  帧名 -> [[cls, x1, y1, x2, y2], ...]   **整帧替换**预标注，坐标是原图**像素**。
                 写 [] 表示这帧一个目标都没有（负样本）——误检帧收成负样本最提分。
  DROP   帧名 -> [预标注框下标, ...]            删掉误检/重复框，下标看 preview/ 里的 `#N`。
  其余未列出的帧                                原样沿用预标注。

类别 id：
{cls_lines}

工作方式：翻 <out>/preview/*.jpg，框对了就不管；框多了写 DROP；框错/漏了写 EDITS 重画
（像素坐标可用任意能放大读坐标的看图工具，或直接看预览估）。
"""

{consts}

EDITS = {{
    # "{ex1}": [[{first_cls}, 120, 340, 460, 700]],   # 重画：框贴紧目标可见边界
    # "{ex2}": [],                                    # 误检帧 -> 负样本
}}

DROP = {{
    # "{ex1}": [0, 2],   # 删掉预标注的第 0、2 号框（下标见 preview 里的 #N）
}}
'''


def _const_name(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(name)).upper() or "CLS"


def summarize_boxes(per_frame: Dict[str, List[List[float]]],
                    classes: Dict[int, str]) -> Dict[str, object]:
    """帧 -> 框 的统计：帧数 / 正负样本 / 每类框数与出现帧数。"""
    box_counts: Counter = Counter()
    frames_with: Counter = Counter()
    positives = 0
    for boxes in per_frame.values():
        if boxes:
            positives += 1
        for cls in {int(b[0]) for b in boxes}:
            frames_with[cls] += 1
        for b in boxes:
            box_counts[int(b[0])] += 1
    return {
        "frames": len(per_frame),
        "positives": positives,
        "negatives": len(per_frame) - positives,
        "boxes": dict(box_counts),
        "frames_with": dict(frames_with),
        "classes": dict(classes),
    }


# ---------- 落盘 / 渲染 ----------

def load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_preview(image: Image.Image, items: Sequence[Tuple[int, float, float, float, float, str]],
                   out_path: Path, scale: float, font: ImageFont.ImageFont) -> None:
    """把框回描到原图并缩放存 jpg（items: (cls, x1, y1, x2, y2, 标注文字)）。"""
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    for cls, x1, y1, x2, y2, text in items:
        color = color_of(cls)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=7)
        top = max(0.0, y1 - 46)
        draw.rectangle([x1, top, x1 + 8 * max(8, len(text)) + 16, top + 44], fill=(0, 0, 0))
        draw.text((x1 + 5, top + 2), text, fill=color, font=font)
    width, height = canvas.size
    canvas.resize((max(1, int(width * scale)), max(1, int(height * scale)))).save(
        out_path, quality=85)


def resolve_detector(model: str, conf: float) -> YoloDetector:
    """--model 既接受 YoloRegistry 名字（default / 任意 .onnx 文件名 stem），也接受 .onnx 路径。"""
    candidate = Path(model)
    if candidate.suffix.lower() == ".onnx":
        path = candidate if candidate.is_absolute() else (ROOT / candidate)
        if not path.is_file():
            sys.exit(f"[FATAL] 模型文件不存在：{path}")
        return YoloDetector(log, model_path=path, conf=conf)
    registry = YoloRegistry(log, config={"model": str(DEFAULT_MODEL_ONNX), "conf": conf},
                            model_dir=MODEL_DIR)
    detector = registry.get(model)
    if detector is None:
        sys.exit(f"[FATAL] 未知模型名 '{model}'；已注册：{', '.join(registry.names())}\n"
                 f"        （名字来自 {MODEL_DIR} 下的 *.onnx 文件名，也可以直接传路径）")
    return detector


def print_stats(title: str, stats: Dict[str, object],
                scores: Optional[Dict[int, List[float]]] = None) -> None:
    classes: Dict[int, str] = stats["classes"]  # type: ignore[assignment]
    boxes: Dict[int, int] = stats["boxes"]      # type: ignore[assignment]
    frames_with: Dict[int, int] = stats["frames_with"]  # type: ignore[assignment]
    total = int(stats["frames"])
    print(f"\n--- {title} ---")
    print(f"  帧数            {total}")
    print(f"  有框帧          {stats['positives']}  ({int(stats['positives']) / max(1, total):.0%})")
    print(f"  负样本帧        {stats['negatives']}")
    print(f"  框总数          {sum(boxes.values())}")
    header = f"\n  {'class':<14}{'boxes':>7}{'frames':>8}"
    if scores is not None:
        header += f"{'min':>7}{'med':>7}{'max':>7}{'>=.5':>7}"
    print(header)
    for cid in sorted(classes):
        row = f"  {classes[cid]:<14}{boxes.get(cid, 0):>7}{frames_with.get(cid, 0):>8}"
        if scores is not None:
            vals = sorted(scores.get(cid, []))
            if vals:
                row += (f"{vals[0]:>7.2f}{vals[len(vals) // 2]:>7.2f}{vals[-1]:>7.2f}"
                        f"{sum(1 for v in vals if v >= 0.5):>7}")
        print(row)


# ---------- 两个模式 ----------

def run_annotate(args: argparse.Namespace) -> int:
    frames_dir = args.frames.resolve()
    if not frames_dir.is_dir():
        sys.exit(f"[FATAL] 帧目录不存在：{frames_dir}")
    out_dir = args.out.resolve()
    labels_dir, preview_dir = out_dir / "labels", out_dir / "preview"
    for d in (labels_dir, preview_dir):
        if d.is_dir():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    detector = resolve_detector(args.model, args.conf)
    if not detector.available():
        sys.exit(f"[FATAL] 模型不可用（缺 onnxruntime 或找不到 {detector.model_path}）")
    classes = detector.class_names()
    print(f"[model] {detector.model_path}")
    print(f"[model] 类别 {len(classes)} 个：" + ", ".join(f"{i}:{n}" for i, n in sorted(classes.items())))

    frames = iter_frames(frames_dir, args.limit)
    print(f"[frames] {frames_dir}  共 {len(frames)} 帧  (conf={args.conf})")
    if not frames:
        sys.exit(f"[FATAL] {frames_dir} 里没有图片（支持 {', '.join(IMAGE_SUFFIXES)}）")

    font = load_font(40)
    per_frame: Dict[str, List[List[float]]] = {}
    scores: Dict[int, List[float]] = defaultdict(list)
    summary_frames: List[Dict] = []

    for i, path in enumerate(frames):
        image = Image.open(path).convert("RGB")
        width, height = image.size
        dets = detector.detect(image, conf=args.conf)
        stem = path.stem

        boxes = detections_to_pixel_boxes(dets)
        per_frame[stem] = boxes
        for d in dets:
            scores[int(d["class_id"])].append(float(d["score"]))

        (labels_dir / f"{stem}.txt").write_text(
            "\n".join(pixel_boxes_to_lines(boxes, width, height)), encoding="utf-8")
        render_preview(
            image,
            [(int(d["class_id"]), *d["bbox"], f"#{n} {d['label']} {d['score']:.2f}")
             for n, d in enumerate(dets)],
            preview_dir / f"{stem}.jpg", args.preview_scale, font)

        summary_frames.append({
            "frame": path.name, "stem": stem, "size": [width, height], "n": len(dets),
            "dets": [{"label": d["label"], "class_id": int(d["class_id"]),
                      "score": float(d["score"]), "bbox": list(d["bbox"])} for d in dets],
        })
        if (i + 1) % 100 == 0:
            print(f"  [{i + 1}/{len(frames)}] 有框帧 "
                  f"{sum(1 for b in per_frame.values() if b)}", flush=True)

    stats = summarize_boxes(per_frame, classes)
    (out_dir / "summary.json").write_text(json.dumps({
        "model": args.model, "model_path": str(detector.model_path), "conf": args.conf,
        "frames_dir": str(frames_dir), "classes": {str(k): v for k, v in classes.items()},
        "stats": {k: v for k, v in stats.items() if k != "classes"},
        "frames": summary_frames,
    }, indent=1, ensure_ascii=False), encoding="utf-8")

    template = out_dir / "edits_template.py"
    template.write_text(edits_template_text(classes, [f["stem"] for f in summary_frames]),
                        encoding="utf-8")

    print_stats("预标注统计", stats, scores)
    print(f"\n[out] 标签   {labels_dir}")
    print(f"[out] 预览   {preview_dir}")
    print(f"[out] 编辑表模板 {template}")
    print("\n下一步：翻预览纠错 -> 把模板另存为 edits.py 填写 -> "
          f"preannotate.py --apply-edits {out_dir / 'edits.py'} --out {out_dir}")
    return 0


def run_apply_edits(args: argparse.Namespace) -> int:
    out_dir = args.out.resolve()
    summary_path = out_dir / "summary.json"
    if not summary_path.is_file():
        sys.exit(f"[FATAL] 找不到 {summary_path}——先跑一次预标注再 --apply-edits")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    classes = {int(k): v for k, v in summary.get("classes", {}).items()}
    frames_dir = (args.frames or Path(summary["frames_dir"])).resolve()
    labels_dir = out_dir / "labels"
    final_dir, preview_dir = out_dir / "labels_final", out_dir / "preview_final"
    for d in (final_dir, preview_dir):
        if d.is_dir():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    edits, drop = load_edit_table(args.apply_edits.resolve())
    sizes = {f["stem"]: tuple(f["size"]) for f in summary["frames"]}
    known = set(sizes)
    unknown = sorted((set(edits) | set(drop)) - known)
    if unknown:
        print(f"[WARN] 编辑表里有 {len(unknown)} 个帧名不在本次预标注里（拼错了？）：")
        for stem in unknown[:10]:
            print(f"[WARN]   {stem}")

    touched = (set(edits) | set(drop)) & known
    font = load_font(40)
    per_frame: Dict[str, List[List[float]]] = {}
    changed = 0

    for stem in sorted(known):
        width, height = sizes[stem]
        pre_text = (labels_dir / f"{stem}.txt").read_text(encoding="utf-8") \
            if (labels_dir / f"{stem}.txt").is_file() else ""
        pre_boxes = lines_to_pixel_boxes(pre_text, width, height)
        boxes = apply_edit_table(stem, pre_boxes, edits, drop)
        per_frame[stem] = [b for b in (clamp_box(b, width, height) for b in boxes) if b]
        (final_dir / f"{stem}.txt").write_text(
            "\n".join(pixel_boxes_to_lines(boxes, width, height)), encoding="utf-8")
        if stem in touched:
            changed += 1
        if args.preview_all or stem in touched:
            image_path = _find_frame(frames_dir, stem)
            if image_path is None:
                print(f"[WARN] 找不到帧图片 {stem}，跳过回描预览")
                continue
            image = Image.open(image_path).convert("RGB")
            render_preview(image,
                           [(int(b[0]), b[1], b[2], b[3], b[4],
                             f"#{n} {classes.get(int(b[0]), b[0])}")
                            for n, b in enumerate(per_frame[stem])],
                           preview_dir / f"{stem}.jpg", args.preview_scale, font)

    stats = summarize_boxes(per_frame, classes)
    print(f"[apply] 编辑表 {args.apply_edits}：EDITS {len(edits)} 帧 / DROP {len(drop)} 帧，"
          f"本次改动 {changed} 帧，全量写出 {len(per_frame)} 个标签")
    print_stats("纠错后统计", stats)
    print(f"\n[out] 最终标签 {final_dir}")
    print(f"[out] 回描预览 {preview_dir}")
    print("\n下一步：build_increment.py --base <旧数据集> "
          f"--new-images {frames_dir} --new-labels {final_dir} --out <新数据集>")
    return 0


def _find_frame(frames_dir: Path, stem: str) -> Optional[Path]:
    for suffix in IMAGE_SUFFIXES:
        path = frames_dir / f"{stem}{suffix}"
        if path.is_file():
            return path
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="YOLO 自举预标注 / 纠错编辑表应用（AutoPlayQA 离线工具）")
    p.add_argument("--model", default="default",
                   help="YoloRegistry 名字（来自 task/models/*.onnx 的文件名 stem）"
                        "或 .onnx 路径；默认 default = task/models/yolo.onnx")
    p.add_argument("--frames", type=Path, default=None,
                   help="待标注的帧目录（预标注模式必填；--apply-edits 时默认读 summary.json）")
    p.add_argument("--out", type=Path, required=True, help="产物目录")
    p.add_argument("--conf", type=float, default=DEFAULT_CONF,
                   help=f"预标注置信度阈值（默认 {DEFAULT_CONF}：宁多勿漏，多的让人删）")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 帧（试跑用）")
    p.add_argument("--preview-scale", type=float, default=DEFAULT_PREVIEW_SCALE,
                   help=f"预览图缩放比例（默认 {DEFAULT_PREVIEW_SCALE}）")
    p.add_argument("--apply-edits", type=Path, default=None,
                   help="切到应用模式：把这份编辑表 .py 应用到预标注上，出 labels_final/")
    p.add_argument("--preview-all", action="store_true",
                   help="应用模式下给所有帧回描预览（默认只画被编辑表改过的帧）")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.apply_edits is not None:
        return run_apply_edits(args)
    if args.frames is None:
        sys.exit("[FATAL] 预标注模式必须给 --frames <帧目录>")
    return run_annotate(args)


if __name__ == "__main__":
    sys.exit(main())
