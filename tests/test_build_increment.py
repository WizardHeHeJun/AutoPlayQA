"""training/build_increment.py 的单测：纯文件操作，不依赖真机 / 真模型 / 真图片解码。"""
from __future__ import annotations

from pathlib import Path

import pytest

from training import build_increment as bi

BOX = "0.5 0.5 0.2 0.2"


# ---------- 帧组 ----------

@pytest.mark.parametrize("stem,expected", [
    ("clip_alpha_016", "clip_alpha"),
    ("0805_191702_21466f_r10_00120", "0805_191702_21466f_r10"),
    ("shot_20260806_120858_015825", "shot_20260806_120858"),
    ("v1_s9", "v1_s9"),          # 单个数字结尾不当序号（不误伤语义帧名）
    ("lonely", "lonely"),
    ("00120", "00120"),          # 全是数字：没有前缀可留，整名当组名
])
def test_group_key(stem, expected):
    assert bi.group_key(stem) == expected


def test_labels_dir_for():
    assert bi.labels_dir_for(Path("/ds/images/train")) == Path("/ds/labels/train")
    # 路径里有多个 images 时取最后一个
    assert bi.labels_dir_for(Path("/images/ds/images/val")) == Path("/images/ds/labels/val")
    assert bi.labels_dir_for(Path("/ds/frames")) == Path("/ds/labels")


# ---------- 标签解析 / 校验 ----------

def test_parse_label_text():
    assert bi.parse_label_text("") == []
    assert bi.parse_label_text("\n \n") == []
    assert bi.parse_label_text(f"1 {BOX}\n0 {BOX}\n") == [
        (1, 0.5, 0.5, 0.2, 0.2), (0, 0.5, 0.5, 0.2, 0.2)]


@pytest.mark.parametrize("text", ["0 0.5 0.5 0.2", "0 0.5 0.5 0.2 0.2 0.2", "button 0.5 0.5 0.2 0.2"])
def test_parse_label_text_rejects_malformed(text):
    with pytest.raises(ValueError):
        bi.parse_label_text(text)


def _frame(stem: str, text: str = "") -> bi.Frame:
    return bi.Frame(stem=stem, image=Path(f"{stem}.png"), label_text=text,
                    class_ids=[r[0] for r in bi.parse_label_text(text)])


def test_validate_frames_accepts_good_labels():
    assert bi.validate_frames([_frame("a", f"0 {BOX}\n1 {BOX}"), _frame("neg")], 2, "新帧") == []


def test_validate_frames_flags_class_id_out_of_range():
    errors = bi.validate_frames([_frame("a", f"4 {BOX}")], 2, "新帧")
    assert len(errors) == 1
    assert "类别 id 4 越界" in errors[0] and "--new-classes" in errors[0]


def test_validate_frames_flags_unnormalized_coords():
    errors = bi.validate_frames([_frame("a", "0 540 1200 100 200")], 1, "新帧")
    assert errors and "归一化" in errors[0]


def test_validate_frames_flags_bad_size():
    assert bi.validate_frames([_frame("a", "0 0.5 0.5 0.0 0.2")], 1, "新帧")
    assert bi.validate_frames([_frame("a", "0 0.5 0.5 1.5 0.2")], 1, "新帧")


def test_validate_frames_reports_malformed_line():
    errors = bi.validate_frames(
        [bi.Frame(stem="a", image=Path("a.png"), label_text="0 0.5 0.5")], 1, "新帧")
    assert errors and "a.txt" in errors[0]


# ---------- 类别表合并 ----------

def test_merge_class_names_appends():
    assert bi.merge_class_names({0: "button", 1: "icon"}, ["dialog", "toast"]) == {
        0: "button", 1: "icon", 2: "dialog", 3: "toast"}


def test_merge_class_names_noop():
    assert bi.merge_class_names({0: "button"}, []) == {0: "button"}


def test_merge_class_names_rejects_duplicate():
    with pytest.raises(ValueError, match="已经有了"):
        bi.merge_class_names({0: "button", 1: "icon"}, ["icon"])


def test_merge_class_names_rejects_empty_name():
    with pytest.raises(ValueError):
        bi.merge_class_names({0: "button"}, ["  "])


def test_merge_class_names_rejects_non_contiguous_base():
    with pytest.raises(ValueError, match="连续"):
        bi.merge_class_names({0: "button", 2: "icon"}, ["dialog"])


# ---------- 划分 ----------

def _seq(prefix: str, n: int, text: str = f"0 {BOX}"):
    return [_frame(f"{prefix}_{i:03d}", text) for i in range(n)]


def test_plan_split_keeps_groups_intact():
    frames = _seq("lvl_a", 6) + _seq("lvl_b", 6) + _seq("lvl_c", 6)
    assignment = bi.plan_split(frames, {}, val_ratio=0.34, seed=0)
    for prefix in ("lvl_a", "lvl_b", "lvl_c"):
        splits = {s for stem, s in assignment.items() if bi.group_key(stem) == prefix}
        assert len(splits) == 1, f"{prefix} 被拆到了 {splits}"
    assert set(assignment.values()) == {"train", "val"}


def test_plan_split_negatives_never_go_to_val(capsys):
    # lvl_a 组混有一帧负样本（lvl_a_100 与 lvl_a_000..002 同组），lvl_c 是纯正样本组。
    frames = _seq("lvl_a", 3) + [_frame("lvl_a_100"), _frame("lvl_b_000")] + _seq("lvl_c", 3)
    assignment = bi.plan_split(frames, {}, val_ratio=0.9, seed=0)
    assert assignment["lvl_a_100"] == "train"
    assert assignment["lvl_b_000"] == "train"
    # val_ratio=0.9 逼得纯组 lvl_c 不够用，混合组 lvl_a 被迫上场：
    # 组内正样本跟着一起进 val（组不被拆散着看待），负样本仍按规则改判 train，
    # 因此必须打 WARN 留痕，不能静默处理。
    assert all(assignment[f"lvl_a_{i:03d}"] == "val" for i in range(3))
    out = capsys.readouterr().out
    assert "[WARN]" in out and "lvl_a" in out


def test_plan_split_respects_forced_groups():
    frames = _seq("lvl_a", 4) + _seq("lvl_b", 4)
    assignment = bi.plan_split(frames, {"lvl_a": "train", "lvl_b": "val"},
                               val_ratio=1.0, seed=0)
    assert all(assignment[f.stem] == "train" for f in frames if f.stem.startswith("lvl_a"))
    assert all(assignment[f.stem] == "val" for f in frames if f.stem.startswith("lvl_b"))


def test_plan_split_forced_val_survives_the_all_val_guard():
    """所有组都被钉死到 val 时，兜底逻辑不许把防泄漏钉子掰回 train。"""
    frames = _seq("lvl_a", 2) + _seq("lvl_b", 2)
    forced = {"lvl_a": "val", "lvl_b": "val"}
    assert set(bi.plan_split(frames, forced, val_ratio=0.1, seed=0).values()) == {"val"}


def test_plan_split_does_not_send_everything_to_val():
    frames = _seq("lvl_a", 3) + _seq("lvl_b", 3)
    assignment = bi.plan_split(frames, {}, val_ratio=1.0, seed=0)
    assert "train" in set(assignment.values())


def test_plan_split_covers_rare_class_in_val():
    """稀有类优先进 val：只有 1 帧含 cls 3 时，那一组必须被选中。"""
    frames = _seq("common_a", 8) + _seq("common_b", 8) + [_frame("rare_000", f"3 {BOX}")]
    assignment = bi.plan_split(frames, {}, val_ratio=0.05, seed=0)
    assert assignment["rare_000"] == "val"


def test_plan_split_is_deterministic():
    frames = _seq("a", 3) + _seq("b", 3) + _seq("c", 3) + _seq("d", 3)
    first = bi.plan_split(frames, {}, val_ratio=0.25, seed=7)
    assert first == bi.plan_split(frames, {}, val_ratio=0.25, seed=7)


def test_plan_split_single_group_is_never_torn_apart():
    """只有一个帧组时宁可 val 拿不到新素材，也不能把近重复帧拆开（那是自欺欺人的指标）。"""
    assignment = bi.plan_split(_seq("only", 5), {}, val_ratio=0.5, seed=0)
    assert len(set(assignment.values())) == 1


def test_plan_split_prefers_pure_group_over_mixed_group():
    """存在纯正样本组时，混合组（正负同组）不该被选进 val——整组留在 train，不拆。"""
    pure = _seq("pure_grp", 4)                                  # 4 帧全正样本
    mixed = _seq("mixed_grp", 3) + [_frame("mixed_grp_100")]    # 3 正 + 1 负，同组
    assignment = bi.plan_split(pure + mixed, {}, val_ratio=0.3, seed=0)
    assert all(assignment[f"mixed_grp_{i:03d}"] == "train" for i in range(3))
    assert assignment["mixed_grp_100"] == "train"
    assert any(assignment[f"pure_grp_{i:03d}"] == "val" for i in range(4))


def test_plan_split_falls_back_to_mixed_group_with_warning(capsys):
    """没有纯正样本组可选时，允许选中混合组进 val，但必须打印 WARN 说明拆分详情。"""
    grp_a = _seq("grp_a", 3) + [_frame("grp_a_100")]   # 3 正 + 1 负
    grp_b = _seq("grp_b", 3) + [_frame("grp_b_100")]   # 3 正 + 1 负
    assignment = bi.plan_split(grp_a + grp_b, {}, val_ratio=0.5, seed=0)
    assert set(assignment.values()) == {"train", "val"}
    assert assignment["grp_a_100"] == "train"
    assert assignment["grp_b_100"] == "train"
    out = capsys.readouterr().out
    assert out.count("[WARN]") >= 1
    assert "grp_a" in out or "grp_b" in out


def test_count_boxes():
    assert bi.count_boxes([_frame("a", f"0 {BOX}\n0 {BOX}"), _frame("b", f"1 {BOX}")]) == {0: 2, 1: 1}


# ---------- 读盘 ----------

def _write_frames(images: Path, labels: Path, spec: dict) -> None:
    images.mkdir(parents=True, exist_ok=True)
    labels.mkdir(parents=True, exist_ok=True)
    for stem, text in spec.items():
        (images / f"{stem}.png").write_bytes(b"\x89PNG-stub")
        (labels / f"{stem}.txt").write_text(text, encoding="utf-8")


def test_read_frames_pairs_images_and_labels(tmp_path):
    _write_frames(tmp_path / "images", tmp_path / "labels", {"a": f"0 {BOX}", "b": ""})
    frames = bi.read_frames(tmp_path / "images", tmp_path / "labels", "新帧")
    assert [f.stem for f in frames] == ["a", "b"]
    assert frames[0].positive and not frames[1].positive


def test_read_frames_skips_and_reports_orphan_images(tmp_path, capsys):
    _write_frames(tmp_path / "images", tmp_path / "labels", {"a": ""})
    (tmp_path / "images" / "orphan.png").write_bytes(b"x")
    frames = bi.read_frames(tmp_path / "images", tmp_path / "labels", "新帧")
    assert [f.stem for f in frames] == ["a"]
    assert "orphan.png" in capsys.readouterr().out


def test_read_frames_missing_dir_exits(tmp_path):
    with pytest.raises(SystemExit):
        bi.read_frames(tmp_path / "nope", tmp_path / "also_nope", "新帧")


# ---------- 端到端 ----------

def _make_base(root: Path, names, train: dict, val: dict) -> Path:
    for split, spec in (("train", train), ("val", val)):
        _write_frames(root / "images" / split, root / "labels" / split, spec)
    (root / "data.yaml").write_text(
        f"path: {root}\ntrain: images/train\nval: images/val\n\nnames:\n"
        + "".join(f"  {i}: {n}\n" for i, n in enumerate(names)), encoding="utf-8")
    return root


@pytest.fixture
def base_dataset(tmp_path):
    return _make_base(
        tmp_path / "base", ["button", "icon"],
        train={f"old_a_{i:03d}": f"0 {BOX}" for i in range(6)} | {"old_neg_000": ""},
        val={"old_b_000": f"1 {BOX}", "old_b_001": f"1 {BOX}"})


def _run(base: Path, new: Path, out: Path, *extra) -> int:
    return bi.main(["--base", str(base), "--new", str(new), "--out", str(out), *extra])


def test_build_merges_and_writes_dataset(tmp_path, base_dataset):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels",
                  {f"lvl_x_{i:03d}": f"0 {BOX}" for i in range(4)}
                  | {f"lvl_y_{i:03d}": f"1 {BOX}" for i in range(4)}
                  | {"lvl_y_100": ""})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out) == 0

    yaml_text = (out / "data.yaml").read_text(encoding="utf-8")
    assert "0: button" in yaml_text and "1: icon" in yaml_text
    assert f"path: {out}" in yaml_text

    train_imgs = {p.stem for p in (out / "images" / "train").iterdir()}
    val_imgs = {p.stem for p in (out / "images" / "val").iterdir()}
    # 旧集划分原样保留
    assert {f"old_a_{i:03d}" for i in range(6)} | {"old_neg_000"} <= train_imgs
    assert {"old_b_000", "old_b_001"} <= val_imgs
    # 新帧全部并入，一张不丢
    assert len(train_imgs | val_imgs) == 9 + 9
    # 图片与标签一一对应
    for split in ("train", "val"):
        assert ({p.stem for p in (out / "images" / split).iterdir()}
                == {p.stem for p in (out / "labels" / split).iterdir()})
    # 负样本不进 val
    assert "lvl_y_100" in train_imgs


def test_build_never_splits_a_group_across_train_and_val(tmp_path, base_dataset):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels",
                  {f"lvl_x_{i:03d}": f"0 {BOX}" for i in range(5)}
                  | {f"lvl_y_{i:03d}": f"1 {BOX}" for i in range(5)})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out, "--val-ratio", "0.4") == 0
    where = {}
    for split in ("train", "val"):
        for p in (out / "images" / split).iterdir():
            where.setdefault(bi.group_key(p.stem), set()).add(split)
    assert all(len(v) == 1 for v in where.values()), where


def test_build_pins_overlapping_group_to_base_split(tmp_path, base_dataset):
    """新帧的帧组旧集里已在 val 出现过 -> 不许溜进 train（防泄漏）。"""
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels",
                  {f"old_b_{i:03d}": f"1 {BOX}" for i in range(2, 8)})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out, "--new-prefix", "n_", "--val-ratio", "0.0") == 0
    train_stems = {p.stem for p in (out / "images" / "train").iterdir()}
    assert not any(s.startswith("n_old_b") for s in train_stems)
    assert len([p for p in (out / "images" / "val").iterdir() if p.stem.startswith("n_")]) == 6


def test_build_adds_new_class(tmp_path, base_dataset):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels",
                  {f"lvl_deer_{i:03d}": f"2 {BOX}" for i in range(4)})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out, "--new-classes", "dialog") == 0
    yaml_text = (out / "data.yaml").read_text(encoding="utf-8")
    assert "2: dialog" in yaml_text
    # 旧类 id 不动，否则旧标签集体错位
    assert "0: button" in yaml_text and "1: icon" in yaml_text


def test_build_rejects_unknown_class_id(tmp_path, base_dataset, capsys):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_deer_000": f"2 {BOX}"})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out) == 1        # 没声明 --new-classes -> 拒绝
    assert "越界" in capsys.readouterr().out
    assert not out.exists()                          # 校验没过就不该写出半成品


def test_build_warns_when_new_class_has_no_boxes(tmp_path, base_dataset, capsys):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_x_000": f"0 {BOX}"})
    assert _run(base_dataset, new, tmp_path / "out", "--new-classes", "dialog") == 0
    out = capsys.readouterr().out
    assert "dialog" in out and "一个框都没有" in out


def test_build_rejects_duplicate_class_name(tmp_path, base_dataset):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_x_000": f"0 {BOX}"})
    with pytest.raises(ValueError):
        _run(base_dataset, new, tmp_path / "out", "--new-classes", "button")


def test_build_refuses_to_overwrite_without_force(tmp_path, base_dataset):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_x_000": f"0 {BOX}"})
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit):
        _run(base_dataset, new, out)
    assert _run(base_dataset, new, out, "--force") == 0


def test_build_renames_colliding_stems(tmp_path, base_dataset, capsys):
    """新帧与旧集重名时不许互相覆盖（默认无前缀，靠自动改名兜底）。"""
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"old_a_000": f"0 {BOX}"})
    out = tmp_path / "out"
    assert _run(base_dataset, new, out) == 0
    stems = {p.stem for split in ("train", "val") for p in (out / "images" / split).iterdir()}
    assert "old_a_000" in stems and "old_a_000__2" in stems
    assert "冲突" in capsys.readouterr().out


def test_build_val_ratio_defaults_to_base_ratio(tmp_path, base_dataset, capsys):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_x_000": f"0 {BOX}"})
    assert _run(base_dataset, new, tmp_path / "out") == 0
    assert "沿用旧集比例" in capsys.readouterr().out


def test_build_reports_thin_classes(tmp_path, base_dataset, capsys):
    new = tmp_path / "new"
    _write_frames(new / "images", new / "labels", {"lvl_x_000": f"0 {BOX}"})
    assert _run(base_dataset, new, tmp_path / "out") == 0
    out = capsys.readouterr().out
    assert "样本过少的类别" in out and "icon" in out


def test_build_requires_new_dir(tmp_path, base_dataset):
    with pytest.raises(SystemExit):
        bi.main(["--base", str(base_dataset), "--out", str(tmp_path / "out")])


def test_build_missing_base_yaml_exits(tmp_path):
    with pytest.raises(SystemExit):
        bi.main(["--base", str(tmp_path / "nope"), "--new", str(tmp_path),
                 "--out", str(tmp_path / "out")])
