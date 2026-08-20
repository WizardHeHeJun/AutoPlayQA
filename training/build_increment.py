"""把纠错后的新帧并入旧数据集，产出新一版 YOLO 数据集（自举闭环第 ④ 步）。

    旧数据集（保持原 train/val 划分，= 100% replay 防遗忘）
  + 新帧 + 纠错标签（preannotate.py --apply-edits 的 labels_final/）
  + 可选的新增类别（--new-classes）
  -> <out>/{data.yaml, images/{train,val}, labels/{train,val}}

离线工具，跑在**项目环境**（只用 PyYAML + shutil，不需要 torch/ultralytics）。
下面 `<python>` 指项目 conda 环境的解释器：

    <python> training\\build_increment.py ^
        --base outputs\\dataset_v2 ^
        --new-images outputs\\dataset_work\\incoming ^
        --new-labels outputs\\dataset_work\\pre_v3\\labels_final ^
        --out outputs\\dataset_v3 --new-classes dialog

三条纪律（都在代码里落实，不靠人记）：

1. **旧集全量并入**：删旧数据 = 教模型忘掉旧类。旧集的 train/val 划分原样保留
   （那是人工分层挑过的，随机重划会让稀有类某一侧饿死）。
2. **划分防泄漏**：新帧按「帧组」整组进 train 或 val，绝不拆开——录屏抽帧/连拍出来的
   相邻帧几乎逐像素相同，同组跨 split = val 指标自己骗自己。组名 = 帧名去掉尾部序号
   （`clip_a_016` -> `clip_a`）。**命名不带序号的散帧无法自动分组，
   会被当成各自独立的一组**——这种素材要么手工命名成同前缀，要么接受乐观指标并在报告里写明。
3. **负样本不进 val**：空标签帧一律留在 train。val 里全是背景帧只会把指标做漂亮。
"""
from __future__ import annotations

import argparse
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Windows 控制台默认 cp936，中文提示会变乱码（同 train_and_export.py）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # 非 TextIOWrapper / 已重定向
        pass

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
SPLITS = ("train", "val")
DEFAULT_VAL_RATIO = 0.15
THIN_CLASS_BOXES = 10  # 与 train_and_export.py --check 同口径
# 帧组名 = 去掉尾部的 _<两位以上数字>（录屏抽帧/连拍序号）。两位以上是为了不误伤
# 本身以单个数字结尾的语义帧名（如 v1_s9）；宁可把组切粗一点，也不能把同一段素材拆开。
SEQ_SUFFIX = re.compile(r"^(.+?)_\d{2,}$")


# ---------- 数据结构 ----------

@dataclass
class Frame:
    """一帧素材：图片 + 标签文本（空文本 = 负样本）。"""
    stem: str
    image: Path
    label_text: str
    class_ids: List[int] = field(default_factory=list)

    @property
    def positive(self) -> bool:
        return bool(self.class_ids)


@dataclass
class Dataset:
    data_yaml: Path
    names: Dict[int, str]
    frames: Dict[str, List[Frame]]  # split -> frames


# ---------- 纯逻辑（可导入、单测覆盖）----------

def group_key(stem: str) -> str:
    """帧名 -> 帧组名（去掉尾部序号），近重复帧序列归为同一组。"""
    m = SEQ_SUFFIX.match(stem)
    return m.group(1) if m else stem


def parse_label_text(text: str) -> List[Tuple[int, float, float, float, float]]:
    """YOLO txt -> [(cls, cx, cy, w, h)]；空行忽略，格式不对的行原地报错。"""
    rows: List[Tuple[int, float, float, float, float]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"第 {lineno} 行不是 5 列（cls cx cy w h）：{line!r}")
        try:
            rows.append((int(parts[0]), *(float(v) for v in parts[1:])))  # type: ignore[misc]
        except ValueError as exc:
            raise ValueError(f"第 {lineno} 行数值解析失败：{line!r}（{exc}）") from exc
    return rows


def merge_class_names(base_names: Dict[int, str],
                      new_classes: Sequence[str]) -> Dict[int, str]:
    """旧类别表 + 新类别（追加在末尾，旧 id 一律不动，否则旧标签集体错位）。"""
    if sorted(base_names) != list(range(len(base_names))):
        raise ValueError(f"旧类别表的 id 不是从 0 连续到 {len(base_names) - 1}：{base_names}")
    merged = dict(base_names)
    existing = set(base_names.values())
    for name in new_classes:
        name = str(name).strip()
        if not name:
            raise ValueError("--new-classes 里有空类别名")
        if name in existing:
            raise ValueError(f"类别 '{name}' 旧集里已经有了（id "
                             f"{[i for i, n in base_names.items() if n == name][0]}），"
                             "别重复声明")
        merged[len(merged)] = name
        existing.add(name)
    return merged


def validate_frames(frames: Sequence[Frame], n_classes: int, where: str) -> List[str]:
    """校验标签：cls id 越界 / 归一化坐标出界，返回人话错误列表（空 = 通过）。"""
    errors: List[str] = []
    for frame in frames:
        try:
            rows = parse_label_text(frame.label_text)
        except ValueError as exc:
            errors.append(f"{where} {frame.stem}.txt：{exc}")
            continue
        for cls, cx, cy, bw, bh in rows:
            if not 0 <= cls < n_classes:
                errors.append(f"{where} {frame.stem}.txt：类别 id {cls} 越界"
                              f"（当前类别表只有 0..{n_classes - 1}）"
                              "——新类别要用 --new-classes 声明")
            if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
                errors.append(f"{where} {frame.stem}.txt：中心点 ({cx}, {cy}) 不在 [0,1]"
                              "——标签不是归一化坐标？")
            if not (0.0 < bw <= 1.0 and 0.0 < bh <= 1.0):
                errors.append(f"{where} {frame.stem}.txt：宽高 ({bw}, {bh}) 不在 (0,1]")
    return errors


def plan_split(frames: Sequence[Frame], forced: Dict[str, str], val_ratio: float,
               seed: int = 0) -> Dict[str, str]:
    """给新帧分 train/val：整组不拆、负样本进 train、稀有类保证 val 有覆盖。

    forced: 帧组 -> split，用于「这一组旧集里已经有了」的防泄漏钉死。

    **val 候选优先选纯正样本组**：帧组若混有负样本帧，一旦被选中进 val，组内负样本帧仍会
    被下面「负样本一律 train」的规则掰回 train，导致同组正负样本被拆到两侧——同段素材背景
    逐像素相同，这就是 train/val 背景泄漏，而且此前完全静默。因此候选阶段优先选全正样本的
    「纯组」，只有没有纯组可用时才退化到混合组，并在结果里对被拆分的组打 [WARN] 留痕。
    """
    groups: Dict[str, List[Frame]] = defaultdict(list)
    for frame in frames:
        groups[group_key(frame.stem)].append(frame)

    def group_is_pure(g: str) -> bool:
        return all(f.positive for f in groups[g])

    positives = [f for f in frames if f.positive]
    target = int(round(val_ratio * len(positives)))
    group_split: Dict[str, str] = {g: forced[g] for g in groups if g in forced}

    def val_positive_count() -> int:
        return sum(1 for g, s in group_split.items() if s == "val"
                   for f in groups[g] if f.positive)

    free = [g for g in sorted(groups) if g not in group_split
            and any(f.positive for f in groups[g])]
    rng = random.Random(seed)
    rng.shuffle(free)

    # ① 稀有类优先：让每个类在 val 里至少有一帧覆盖（稀有类在 val 缺席 = 指标全靠猜）
    class_frames: Counter = Counter()
    for frame in positives:
        for cls in set(frame.class_ids):
            class_frames[cls] += 1
    covered = {cls for g, s in group_split.items() if s == "val"
               for f in groups[g] for cls in f.class_ids}
    for cls, _ in sorted(class_frames.items(), key=lambda kv: (kv[1], kv[0])):
        if cls in covered:
            continue
        candidates = [g for g in free
                      if any(cls in f.class_ids for f in groups[g])]
        if not candidates:
            continue
        # 纯正样本组优先；没有纯组能覆盖这个类时才退化到混合组（拆分统一在最后打 WARN）
        pure_candidates = [g for g in candidates if group_is_pure(g)]
        pick_pool = pure_candidates or candidates
        # 组越小越好：整组进 val，大组会把训练数据一次性搬空
        pick = min(pick_pool, key=lambda g: (sum(1 for f in groups[g] if f.positive), g))
        group_split[pick] = "val"
        free.remove(pick)
        covered |= {c for f in groups[pick] for c in f.class_ids}

    # ② 补到目标比例：纯正样本组排在混合组前面，混合组留到纯组不够用时才被迫上场
    free.sort(key=lambda g: not group_is_pure(g))
    while val_positive_count() < target and free:
        group_split[free.pop(0)] = "val"

    for g in groups:
        group_split.setdefault(g, "train")

    # ③ 兜底：新帧不能整批进 val（否则这批素材一帧都没训到）。但**不能动 forced**——
    # 那是旧集已有帧组的防泄漏钉子，掰弯它等于把 val 素材喂进 train。
    if len(groups) > 1 and all(s == "val" for s in group_split.values()):
        movable = [g for g in groups if g not in forced]
        if movable:
            group_split[max(movable, key=lambda g: (len(groups[g]), g))] = "train"

    # 混合组最终仍留在 val：组内负样本帧马上会被下面的规则拆回 train，打 WARN 说明拆分详情
    for g in sorted(groups):
        if group_split[g] != "val" or group_is_pure(g):
            continue
        neg_stems = [f.stem for f in groups[g] if not f.positive]
        print(f"[WARN] 帧组 {g} 含负样本帧但被选中进 val（没有纯正样本组可覆盖需求）："
              f"负样本帧 {', '.join(neg_stems)} 按规则改判 train，组内正负样本被拆开")

    # 负样本一律 train
    return {f.stem: ("val" if group_split[group_key(f.stem)] == "val" and f.positive
                     else "train")
            for f in frames}


def count_boxes(frames: Sequence[Frame]) -> Counter:
    counts: Counter = Counter()
    for frame in frames:
        for cls in frame.class_ids:
            counts[cls] += 1
    return counts


# ---------- 读盘 ----------

def labels_dir_for(images_dir: Path) -> Path:
    """images/... -> labels/...（YOLO 约定；取最后一个名为 images 的层级）。"""
    parts = list(images_dir.parts)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "images":
            parts[i] = "labels"
            return Path(*parts)
    return images_dir.parent / "labels"


def read_frames(images_dir: Path, labels_dir: Path, where: str) -> List[Frame]:
    """配对图片与标签 txt；缺标签的图片**跳过并报告**，不静默当背景吃进去。"""
    if not images_dir.is_dir():
        sys.exit(f"[FATAL] 图片目录不存在：{images_dir}")
    if not labels_dir.is_dir():
        sys.exit(f"[FATAL] 标签目录不存在：{labels_dir}")
    frames: List[Frame] = []
    orphans: List[str] = []
    for image in sorted(p for p in images_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES):
        label = labels_dir / f"{image.stem}.txt"
        if not label.is_file():
            orphans.append(image.name)
            continue
        text = label.read_text(encoding="utf-8")
        frames.append(Frame(stem=image.stem, image=image, label_text=text,
                            class_ids=[r[0] for r in parse_label_text(text)]))
    if orphans:
        print(f"[WARN] {where}：{len(orphans)} 张图没有对应 label txt，已跳过"
              "（负样本请给一个空 txt，别留空缺）：")
        for name in orphans[:5]:
            print(f"[WARN]   {name}")
    return frames


def load_yaml_dict(path: Path) -> Dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_base(base: Path) -> Dataset:
    """--base 既接受数据集目录，也接受 data.yaml 本身。"""
    data_yaml = base if base.is_file() else base / "data.yaml"
    if not data_yaml.is_file():
        sys.exit(f"[FATAL] 找不到旧数据集描述：{data_yaml}")
    cfg = load_yaml_dict(data_yaml)
    names = cfg.get("names") or {}
    if isinstance(names, list):
        names = dict(enumerate(names))
    names = {int(k): str(v) for k, v in names.items()}
    if not names:
        sys.exit(f"[FATAL] {data_yaml} 里没有 names 类别表")

    root = Path(cfg.get("path") or data_yaml.parent)
    if not root.is_absolute():
        root = (data_yaml.parent / root).resolve()
    frames: Dict[str, List[Frame]] = {}
    for split in SPLITS:
        rel = cfg.get(split)
        if not rel:
            sys.exit(f"[FATAL] {data_yaml} 缺少 {split} 段")
        images_dir = (root / rel).resolve()
        frames[split] = read_frames(images_dir, labels_dir_for(images_dir), f"旧集 {split}")
    return Dataset(data_yaml=data_yaml, names=names, frames=frames)


# ---------- 落盘 ----------

def emit(frames: Sequence[Tuple[Frame, str]], out_dir: Path, prefix: str,
         taken: Optional[set] = None) -> Dict[str, List[Frame]]:
    """把 (帧, split) 写进 <out>/images|labels/<split>/，返回实际落盘的帧。"""
    taken = taken if taken is not None else set()
    written: Dict[str, List[Frame]] = {s: [] for s in SPLITS}
    for frame, split in frames:
        stem = f"{prefix}{frame.stem}"
        if stem in taken:
            n = 2
            while f"{stem}__{n}" in taken:
                n += 1
            print(f"[WARN] 帧名冲突：{stem} 已存在，本帧改名为 {stem}__{n}")
            stem = f"{stem}__{n}"
        taken.add(stem)
        shutil.copy2(frame.image, out_dir / "images" / split / f"{stem}{frame.image.suffix}")
        (out_dir / "labels" / split / f"{stem}.txt").write_text(
            frame.label_text, encoding="utf-8")
        written[split].append(Frame(stem=stem, image=frame.image,
                                    label_text=frame.label_text,
                                    class_ids=list(frame.class_ids)))
    return written


def write_data_yaml(out_dir: Path, names: Dict[int, str]) -> Path:
    path = out_dir / "data.yaml"
    path.write_text(
        f"path: {out_dir}\ntrain: images/train\nval: images/val\n\nnames:\n"
        + "".join(f"  {i}: {names[i]}\n" for i in sorted(names)), encoding="utf-8")
    return path


def report(all_frames: Dict[str, List[Frame]], names: Dict[int, str],
           new_stems: set) -> None:
    print("\n" + "=" * 72)
    total_boxes: Counter = Counter()
    for split in SPLITS:
        frames = all_frames[split]
        pos = sum(1 for f in frames if f.positive)
        boxes = count_boxes(frames)
        total_boxes.update(boxes)
        print(f"[{split}] 帧 {len(frames)}（正 {pos} / 负 {len(frames) - pos}）  框 "
              + ", ".join(f"{names[i]}={boxes.get(i, 0)}" for i in sorted(names)))
    print("[合计] 框 " + ", ".join(f"{names[i]}={total_boxes.get(i, 0)}" for i in sorted(names))
          + f"  共 {sum(total_boxes.values())}")

    new_frames = [f for split in SPLITS for f in all_frames[split] if f.stem in new_stems]
    new_boxes = count_boxes(new_frames)
    print(f"[新增] 帧 {len(new_frames)}（正 {sum(1 for f in new_frames if f.positive)}）  框 "
          + ", ".join(f"{names[i]}={new_boxes.get(i, 0)}" for i in sorted(names)))

    thin = [names[i] for i in sorted(names) if total_boxes.get(i, 0) < THIN_CLASS_BOXES]
    if thin:
        print(f"[WARN] 样本过少的类别（<{THIN_CLASS_BOXES} 框，mAP50-95 会很难看）："
              + ", ".join(thin))
    val_boxes = count_boxes(all_frames["val"])
    missing = [names[i] for i in sorted(names) if val_boxes.get(i, 0) == 0]
    if missing:
        print(f"[WARN] val 里一个框都没有的类别（指标对它没有意义）：{', '.join(missing)}")
    if not all_frames["val"]:
        print("[WARN] val 是空的，ultralytics 会训不起来")


def build(args: argparse.Namespace) -> int:
    base = load_base(args.base)
    base_counts = {s: len(base.frames[s]) for s in SPLITS}
    base_total = sum(base_counts.values())
    print(f"[base] {base.data_yaml}")
    print(f"[base] 类别 {len(base.names)} 个："
          + ", ".join(f"{i}:{n}" for i, n in sorted(base.names.items())))
    print(f"[base] train {base_counts['train']} 帧 / val {base_counts['val']} 帧")

    new_images = args.new_images or (args.new / "images" if args.new else None)
    new_labels = args.new_labels or (args.new / "labels" if args.new else None)
    if new_images is None or new_labels is None:
        sys.exit("[FATAL] 要么给 --new <含 images/ 与 labels/ 的目录>，"
                 "要么分别给 --new-images / --new-labels")
    new_frames = read_frames(new_images.resolve(), new_labels.resolve(), "新帧")
    if not new_frames:
        sys.exit(f"[FATAL] {new_images} 里没有可用的新帧（图片 + 同名 txt）")

    names = merge_class_names(base.names, args.new_classes or [])
    if args.new_classes:
        print("[class] 新增类别：" + ", ".join(
            f"{i}:{names[i]}" for i in sorted(names) if i >= len(base.names)))

    errors = validate_frames([f for s in SPLITS for f in base.frames[s]],
                             len(base.names), "旧集")
    errors += validate_frames(new_frames, len(names), "新帧")
    if errors:
        print(f"\n[FATAL] 标签校验不通过（{len(errors)} 条）：")
        for line in errors[:20]:
            print(f"  - {line}")
        if len(errors) > 20:
            print(f"  ...（还有 {len(errors) - 20} 条）")
        return 1

    used_classes = {c for f in new_frames for c in f.class_ids}
    for i in sorted(names):
        if i >= len(base.names) and i not in used_classes:
            print(f"[WARN] 新类别 {names[i]} 在新帧标签里一个框都没有（--new-classes 拼错了？）")

    val_ratio = args.val_ratio
    if val_ratio is None:
        val_ratio = (base_counts["val"] / base_total) if base_total else DEFAULT_VAL_RATIO
        print(f"[split] --val-ratio 未指定，沿用旧集比例 {val_ratio:.2%}")

    # 防泄漏：新帧的帧组若旧集里已出现，钉死到旧集所在的 split（跨 split 的钉 train）
    base_group_splits: Dict[str, set] = defaultdict(set)
    for split in SPLITS:
        for frame in base.frames[split]:
            base_group_splits[group_key(frame.stem)].add(split)
    forced = {g: ("train" if len(s) > 1 else next(iter(s)))
              for g, s in base_group_splits.items()}
    overlap = sorted({group_key(f.stem) for f in new_frames} & set(forced))
    if overlap:
        print(f"[split] {len(overlap)} 个帧组旧集里已有，钉死到旧集所在 split 防泄漏："
              + ", ".join(f"{g}->{forced[g]}" for g in overlap[:5])
              + ("  ..." if len(overlap) > 5 else ""))

    assignment = plan_split(new_frames, forced, val_ratio, seed=args.seed)
    n_val_new = sum(1 for s in assignment.values() if s == "val")
    n_groups = len({group_key(f.stem) for f in new_frames})
    print(f"[split] 新帧 {len(new_frames)} 张分成 {n_groups} 个帧组 -> "
          f"val {n_val_new} 帧 / train {len(new_frames) - n_val_new} 帧（整组不拆、负样本不进 val）")
    if n_groups == 1:
        print("[WARN] 新帧只有 1 个帧组：要么全进 train（val 拿不到新素材），"
              "要么组内近重复帧跨 split。检查帧命名是否带了序号后缀。")

    out_dir = args.out.resolve()
    if out_dir.exists():
        if not args.force:
            sys.exit(f"[FATAL] 输出目录已存在：{out_dir}（加 --force 覆盖）")
        shutil.rmtree(out_dir)
    for split in SPLITS:
        (out_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (out_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    taken: set = set()
    written: Dict[str, List[Frame]] = {s: [] for s in SPLITS}
    base_pairs = [(f, s) for s in SPLITS for f in base.frames[s]]
    for split, frames in emit(base_pairs, out_dir, "", taken).items():
        written[split] += frames
    new_pairs = [(f, assignment[f.stem]) for f in new_frames]
    new_written = emit(new_pairs, out_dir, args.new_prefix, taken)
    new_stems = {f.stem for s in SPLITS for f in new_written[s]}
    for split, frames in new_written.items():
        written[split] += frames

    data_yaml = write_data_yaml(out_dir, names)
    report(written, names, new_stems)
    print(f"\n[done] {data_yaml}")
    print("下一步：训练（在装了 ultralytics 的训练环境里）")
    print(f"  <python> training\\train_and_export.py "
          f"--data {data_yaml} --name <run名> --deploy-to task\\models\\<模型>.onnx")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把纠错后的新帧并入旧 YOLO 数据集，产出新一版数据集（AutoPlayQA 离线工具）")
    p.add_argument("--base", type=Path, required=True,
                   help="旧数据集目录或它的 data.yaml")
    p.add_argument("--new", type=Path, default=None,
                   help="新帧目录（内含 images/ 与 labels/）")
    p.add_argument("--new-images", type=Path, default=None,
                   help="新帧图片目录（与 --new 二选一，可配合 --new-labels 分开指）")
    p.add_argument("--new-labels", type=Path, default=None,
                   help="新帧标签目录，通常是 preannotate.py 的 labels_final/")
    p.add_argument("--out", type=Path, required=True, help="新数据集输出目录")
    p.add_argument("--new-classes", nargs="*", default=None,
                   help="新增类别名（按顺序追加到类别表末尾，旧 id 不动）")
    p.add_argument("--val-ratio", type=float, default=None,
                   help="新帧里进 val 的正样本比例；默认沿用旧集的 val 占比")
    p.add_argument("--new-prefix", default="",
                   help="新帧落盘时的文件名前缀（与旧集重名时用，例 v3_）。注意前缀会改变"
                        "帧组名，下一轮再拿这个数据集当 --base 时它与原组不再算同一组")
    p.add_argument("--seed", type=int, default=0, help="划分随机种子（默认 0，结果可复现）")
    p.add_argument("--force", action="store_true", help="输出目录已存在时先删掉重建")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    return build(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
