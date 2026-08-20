"""training/preannotate.py 的单测：不依赖真模型（YoloDetector 全程 mock）、不依赖真机。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from training import preannotate as pre

W, H = 200, 100
CLASSES = {0: "button", 1: "icon"}


# ---------- 坐标换算 ----------

def test_pixel_boxes_round_trip():
    boxes = [[0, 20.0, 10.0, 60.0, 50.0], [1, 100.0, 0.0, 180.0, 40.0]]
    lines = pre.pixel_boxes_to_lines(boxes, W, H)
    assert lines[0].split()[0] == "0"
    back = pre.lines_to_pixel_boxes("\n".join(lines), W, H)
    assert len(back) == 2
    for original, restored in zip(boxes, back):
        assert restored[0] == original[0]
        assert restored[1:] == pytest.approx(original[1:], abs=1e-3)


def test_lines_to_pixel_boxes_skips_malformed():
    assert pre.lines_to_pixel_boxes("", W, H) == []
    assert pre.lines_to_pixel_boxes("0 0.5 0.5 0.1\n\n1 0.5 0.5 0.2 0.2\n", W, H) == [
        [1, 80.0, 40.0, 120.0, 60.0]
    ]


def test_clamp_box_clips_and_drops_degenerate():
    assert pre.clamp_box([0, -30, -30, 500, 500], W, H) == [0, 0.0, 0.0, float(W), float(H)]
    # 坐标写反了也能救回来
    assert pre.clamp_box([1, 60, 50, 20, 10], W, H) == [1, 20.0, 10.0, 60.0, 50.0]
    assert pre.clamp_box([0, 10, 10, 11, 11], W, H) is None      # 太小 -> 丢
    assert pre.clamp_box([0, 300, 10, 400, 50], W, H) is None    # 完全在画面外 -> 丢


def test_pixel_boxes_to_lines_drops_degenerate():
    assert pre.pixel_boxes_to_lines([[0, 10, 10, 10.5, 10.5]], W, H) == []


def test_detections_to_pixel_boxes():
    dets = [{"class_id": 1, "bbox": [10, 20, 30, 40], "score": 0.9, "label": "icon"}]
    assert pre.detections_to_pixel_boxes(dets) == [[1, 10.0, 20.0, 30.0, 40.0]]


# ---------- 编辑表语义 ----------

PRE_BOXES = [[0, 10, 10, 50, 50], [1, 60, 10, 90, 40], [0, 100, 10, 150, 60]]


def test_apply_edit_table_replaces_whole_frame():
    edits = {"f": [[1, 1, 2, 3, 4]]}
    assert pre.apply_edit_table("f", PRE_BOXES, edits, {}) == [[1, 1, 2, 3, 4]]


def test_apply_edit_table_empty_edit_means_negative_frame():
    assert pre.apply_edit_table("f", PRE_BOXES, {"f": []}, {}) == []


def test_apply_edit_table_drops_by_index():
    assert pre.apply_edit_table("f", PRE_BOXES, {}, {"f": [0, 2]}) == [PRE_BOXES[1]]


def test_apply_edit_table_edits_win_over_drop():
    out = pre.apply_edit_table("f", PRE_BOXES, {"f": [[1, 5, 5, 9, 9]]}, {"f": [0, 1, 2]})
    assert out == [[1, 5, 5, 9, 9]]


def test_apply_edit_table_untouched_frame_passes_through():
    assert pre.apply_edit_table("other", PRE_BOXES, {"f": []}, {"f": [0]}) == PRE_BOXES


def test_load_edit_table(tmp_path):
    path = tmp_path / "edits.py"
    path.write_text("EDITS = {'a': [[0, 1, 2, 3, 4]]}\nDROP = {'b': [1]}\n", encoding="utf-8")
    edits, drop = pre.load_edit_table(path)
    assert edits == {"a": [[0, 1, 2, 3, 4]]}
    assert drop == {"b": [1]}


def test_load_edit_table_tolerates_missing_tables(tmp_path):
    path = tmp_path / "edits.py"
    path.write_text("EDITS = {}\n", encoding="utf-8")
    assert pre.load_edit_table(path) == ({}, {})


def test_load_edit_table_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        pre.load_edit_table(tmp_path / "nope.py")


def test_edits_template_is_loadable_python(tmp_path):
    path = tmp_path / "edits_template.py"
    path.write_text(pre.edits_template_text(CLASSES, ["frame_a", "frame_b"]), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "0 = button" in text and "BUTTON = 0" in text and "ICON = 1" in text
    assert "frame_a" in text
    assert pre.load_edit_table(path) == ({}, {})  # 模板里的示例全是注释，加载出来是空表


def test_edits_template_survives_empty_class_table(tmp_path):
    path = tmp_path / "t.py"
    path.write_text(pre.edits_template_text({}, []), encoding="utf-8")
    assert pre.load_edit_table(path) == ({}, {})


# ---------- 帧枚举 / 统计 ----------

def test_iter_frames_sorted_and_limited(tmp_path):
    for name in ("b.png", "a.jpg", "c.jpeg", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in pre.iter_frames(tmp_path)] == ["a.jpg", "b.png", "c.jpeg"]
    assert [p.name for p in pre.iter_frames(tmp_path, limit=2)] == ["a.jpg", "b.png"]


def test_summarize_boxes():
    stats = pre.summarize_boxes(
        {"a": [[0, 1, 1, 9, 9], [0, 2, 2, 8, 8]], "b": [[1, 1, 1, 9, 9]], "c": []}, CLASSES)
    assert stats["frames"] == 3
    assert stats["positives"] == 2
    assert stats["negatives"] == 1
    assert stats["boxes"] == {0: 2, 1: 1}
    assert stats["frames_with"] == {0: 1, 1: 1}


def test_color_of_cycles():
    assert pre.color_of(0) == pre.color_of(len(pre.PALETTE))


# ---------- 模型解析 ----------

def test_resolve_detector_from_onnx_path(tmp_path):
    model = tmp_path / "stub.onnx"
    model.write_bytes(b"stub")
    det = pre.resolve_detector(str(model), 0.2)
    assert det.model_path == model
    assert det.conf == pytest.approx(0.2)


def test_resolve_detector_missing_onnx_exits(tmp_path):
    with pytest.raises(SystemExit):
        pre.resolve_detector(str(tmp_path / "missing.onnx"), 0.2)


def test_resolve_detector_unknown_registry_name_exits():
    with pytest.raises(SystemExit):
        pre.resolve_detector("definitely_not_a_model", 0.2)


# ---------- 端到端（假 detector）----------

class _FakeDetector:
    """替身：available()/class_names()/detect() 三件套，按帧名返回预设检出。"""

    def __init__(self, per_frame):
        self.per_frame = per_frame
        self.model_path = Path("fake.onnx")
        self.seen = []

    def available(self):
        return True

    def class_names(self):
        return dict(CLASSES)

    def detect(self, image, conf=None):
        self.seen.append(conf)
        return self.per_frame.pop(0)


def _write_frames(directory: Path, names) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        Image.new("RGB", (W, H), (30, 60, 30)).save(directory / f"{name}.png")


def _det(class_id, label, bbox, score=0.8):
    return {"class_id": class_id, "label": label, "bbox": bbox, "score": score}


@pytest.fixture
def annotated(tmp_path, monkeypatch):
    """跑一遍预标注模式，返回 (out_dir, frames_dir, fake)。"""
    frames_dir = tmp_path / "frames"
    _write_frames(frames_dir, ["f1", "f2"])
    fake = _FakeDetector([
        [_det(0, "button", [20, 10, 60, 50], 0.91), _det(1, "icon", [100, 20, 150, 70], 0.42)],
        [],  # f2 一个都没检出 -> 负样本
    ])
    monkeypatch.setattr(pre, "resolve_detector", lambda model, conf: fake)
    out_dir = tmp_path / "out"
    assert pre.main(["--model", "fake", "--frames", str(frames_dir),
                     "--out", str(out_dir), "--conf", "0.15"]) == 0
    return out_dir, frames_dir, fake


def test_annotate_writes_labels_preview_summary_and_template(annotated):
    out_dir, frames_dir, fake = annotated
    assert fake.seen == [0.15, 0.15]

    f1 = (out_dir / "labels" / "f1.txt").read_text(encoding="utf-8")
    assert len(f1.splitlines()) == 2
    assert pre.lines_to_pixel_boxes(f1, W, H)[0] == pytest.approx([0, 20, 10, 60, 50], abs=1e-3)
    assert (out_dir / "labels" / "f2.txt").read_text(encoding="utf-8") == ""  # 负样本 = 空文件

    assert (out_dir / "preview" / "f1.jpg").is_file()
    assert (out_dir / "preview" / "f2.jpg").is_file()

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["frames_dir"] == str(frames_dir.resolve())
    assert summary["classes"] == {"0": "button", "1": "icon"}
    assert summary["conf"] == pytest.approx(0.15)
    assert [f["n"] for f in summary["frames"]] == [2, 0]
    assert summary["stats"]["positives"] == 1

    assert (out_dir / "edits_template.py").is_file()


def test_annotate_requires_frames(tmp_path):
    with pytest.raises(SystemExit):
        pre.main(["--model", "fake", "--out", str(tmp_path / "out")])


def test_annotate_missing_frames_dir_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(pre, "resolve_detector", lambda model, conf: _FakeDetector([]))
    with pytest.raises(SystemExit):
        pre.main(["--model", "fake", "--frames", str(tmp_path / "nope"),
                  "--out", str(tmp_path / "out")])


def test_annotate_empty_frames_dir_exits(tmp_path, monkeypatch):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    monkeypatch.setattr(pre, "resolve_detector", lambda model, conf: _FakeDetector([]))
    with pytest.raises(SystemExit):
        pre.main(["--model", "fake", "--frames", str(frames_dir), "--out", str(tmp_path / "o")])


def test_apply_edits_writes_all_labels_and_only_touched_previews(annotated, tmp_path):
    out_dir, _frames_dir, _fake = annotated
    edits = tmp_path / "edits.py"
    edits.write_text("EDITS = {'f2': [[1, 10, 10, 90, 90]]}\nDROP = {'f1': [1]}\n",
                     encoding="utf-8")
    assert pre.main(["--apply-edits", str(edits), "--out", str(out_dir)]) == 0

    final = out_dir / "labels_final"
    assert len((final / "f1.txt").read_text(encoding="utf-8").splitlines()) == 1  # 删掉了 #1
    f2 = pre.lines_to_pixel_boxes((final / "f2.txt").read_text(encoding="utf-8"), W, H)
    assert f2 == [pytest.approx([1, 10, 10, 90, 90], abs=1e-3)]
    # 全量写出：没被编辑表碰过的帧也要有最终标签，下游 build_increment 才能整目录消费
    assert sorted(p.name for p in final.iterdir()) == ["f1.txt", "f2.txt"]
    # 预览默认只画改过的帧
    assert sorted(p.name for p in (out_dir / "preview_final").iterdir()) == ["f1.jpg", "f2.jpg"]


def test_apply_edits_preview_all(annotated, tmp_path):
    out_dir, _frames_dir, _fake = annotated
    edits = tmp_path / "edits.py"
    edits.write_text("EDITS = {}\nDROP = {}\n", encoding="utf-8")
    assert pre.main(["--apply-edits", str(edits), "--out", str(out_dir)]) == 0
    assert list((out_dir / "preview_final").iterdir()) == []   # 没改动 -> 不画

    assert pre.main(["--apply-edits", str(edits), "--out", str(out_dir), "--preview-all"]) == 0
    assert sorted(p.name for p in (out_dir / "preview_final").iterdir()) == ["f1.jpg", "f2.jpg"]
    # 没有编辑表条目时最终标签 == 预标注
    assert ((out_dir / "labels_final" / "f1.txt").read_text(encoding="utf-8")
            == (out_dir / "labels" / "f1.txt").read_text(encoding="utf-8"))


def test_apply_edits_warns_on_unknown_stem(annotated, tmp_path, capsys):
    out_dir, _frames_dir, _fake = annotated
    edits = tmp_path / "edits.py"
    edits.write_text("EDITS = {'typo_stem': []}\nDROP = {}\n", encoding="utf-8")
    assert pre.main(["--apply-edits", str(edits), "--out", str(out_dir)]) == 0
    out = capsys.readouterr().out
    assert "typo_stem" in out and "WARN" in out


def test_apply_edits_without_summary_exits(tmp_path):
    edits = tmp_path / "edits.py"
    edits.write_text("EDITS = {}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        pre.main(["--apply-edits", str(edits), "--out", str(tmp_path / "empty")])
