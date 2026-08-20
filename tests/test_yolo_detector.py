from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import mcp_server
from perception.yolo_detector import YoloDetector, YoloRegistry, load_model_manifest
from task.recognizers import RecognizerHub
from task.task_loader import TaskValidationError, validate_task

NAMES = ["crate", "door"]


@pytest.fixture
def detector():
    return YoloDetector(mcp_server._logger, class_names=NAMES)


def _yolo_output():
    """A YOLOv8/v11-style (1, 4+nc, N) output: 2 real boxes + 1 dup + 5 background.

    Rows are [cx, cy, w, h, cls0(crate), cls1(door)]; columns are anchors.
    """
    rows = [
        [100, 100, 40, 40, 0.90, 0.10],   # crate @ center (100,100)
        [300, 400, 60, 20, 0.10, 0.80],   # door @ center (300,400)
        [102, 101, 40, 40, 0.85, 0.10],   # near-dup crate -> NMS suppressed
    ] + [[10, 10, 5, 5, 0.05, 0.05]] * 5  # background, below conf
    arr = np.array(rows, dtype=np.float32).T  # (6 channels, 8 anchors)
    return arr[None]  # (1, 6, 8)


class _FakeSession:
    """Returns a fixed output regardless of the input blob."""

    def __init__(self, output):
        self._output = output

    def run(self, output_names, feed):
        return [self._output]


def _ready_detector(tmp_path, output=None):
    """A YoloDetector with available()==True and a fake session wired in."""
    model = tmp_path / "yolo.onnx"
    model.write_bytes(b"stub")  # makes available() True without a real model
    d = YoloDetector(mcp_server._logger, model_path=model, class_names=NAMES)
    d._session = _FakeSession(output if output is not None else _yolo_output())
    d._input_name = "images"
    d._size = (640, 640)
    return d


# --- preprocess --------------------------------------------------------------

def test_preprocess_letterbox_math(detector):
    img = np.zeros((720, 1280, 3), np.uint8)  # h=720, w=1280
    blob, scale, pad = detector._preprocess(img, (640, 640))
    assert scale == 0.5                       # min(640/1280, 640/720)
    assert pad == (0, 140)                     # 1280*0.5=640 wide, (640-360)//2 tall
    assert blob.shape == (1, 3, 640, 640)
    assert blob.dtype == np.float32
    assert blob.max() <= 1.0                   # normalized


# --- postprocess (the decoding core) -----------------------------------------

def test_postprocess_decodes_scales_and_nms(detector):
    dets = detector._postprocess(
        _yolo_output(), scale=1.0, pad=(0, 0), orig_shape=(640, 640),
        conf=0.25, iou=0.45, class_filter=None,
    )
    assert [d["label"] for d in dets] == ["crate", "door"]  # best score first
    assert dets[0]["bbox"] == [80, 80, 120, 120]
    assert dets[0]["center"] == [100, 100]
    assert dets[0]["score"] == 0.9
    assert dets[1]["center"] == [300, 400]
    assert dets[1]["class_id"] == 1


def test_postprocess_class_filter(detector):
    dets = detector._postprocess(
        _yolo_output(), scale=1.0, pad=(0, 0), orig_shape=(640, 640),
        conf=0.25, iou=0.45, class_filter={1},
    )
    assert len(dets) == 1 and dets[0]["label"] == "door"


def test_postprocess_undoes_letterbox(detector):
    # scale 0.5 + 140px vertical pad: a box at letterbox center (100,240) maps
    # back to original (200,200). (Pad to 8 anchors so squeeze keeps it 2-D, as
    # a real YOLO output with thousands of anchors always is.)
    rows = [[100, 240, 40, 40, 0.9, 0.1]] + [[10, 10, 5, 5, 0.05, 0.05]] * 7
    out = np.array(rows, np.float32).T[None]
    dets = detector._postprocess(
        out, scale=0.5, pad=(0, 140), orig_shape=(720, 1280),
        conf=0.25, iou=0.45, class_filter=None,
    )
    assert dets[0]["center"] == [200, 200]


# --- detect() full path with a mocked session --------------------------------

def test_detect_end_to_end(tmp_path):
    d = _ready_detector(tmp_path)
    dets = d.detect(Image.new("RGB", (640, 640), "black"))
    assert [x["label"] for x in dets] == ["crate", "door"]
    assert dets[0]["center"] == [100, 100]


def test_detect_roi_offsets_to_full_screen(tmp_path):
    d = _ready_detector(tmp_path)
    # 640x640 crop from a 1000x640 frame at x=360 -> scale 1, pad 0; detections
    # in crop space get the roi origin added back.
    dets = d.detect(Image.new("RGB", (1000, 640), "black"), roi=[360, 0, 1000, 640])
    assert dets[0]["center"] == [460, 100]   # 100 + 360


def test_detect_unknown_class_returns_empty(tmp_path):
    d = _ready_detector(tmp_path)
    assert d.detect(Image.new("RGB", (640, 640)), classes=["nonexistent"]) == []


def test_detect_inert_without_model():
    d = YoloDetector(mcp_server._logger, model_path="task/models/does_not_exist.onnx")
    assert d.available() is False
    assert d.detect(Image.new("RGB", (640, 640))) == []


def test_class_names_override_without_model(detector):
    assert detector.class_names() == {0: "crate", 1: "door"}


# --- execution providers (config yolo.providers) -----------------------------

CPU = "CPUExecutionProvider"
DML = "DmlExecutionProvider"
CUDA = "CUDAExecutionProvider"


class _FakeOrtSession(_FakeSession):
    """Enough of an ORT session for _ensure_session: inputs, meta, providers."""

    def __init__(self, enabled):
        super().__init__(_yolo_output())
        self._enabled = list(enabled)

    def get_inputs(self):
        return [SimpleNamespace(name="images", shape=[1, 3, 640, 640])]

    def get_modelmeta(self):
        return SimpleNamespace(custom_metadata_map={})

    def get_providers(self):
        return list(self._enabled)


class _FakeOrt:
    """Stand-in for the onnxruntime module; records what the session was asked for.

    `enabled` lets a test make ORT report something other than the request, the
    way a real provider that refuses the model would.
    """

    def __init__(self, available, enabled=None):
        self._available = list(available)
        self._enabled = enabled
        self.requested = None

    def get_available_providers(self):
        return list(self._available)

    def InferenceSession(self, path, providers=None):  # noqa: N802 - ORT's name
        self.requested = list(providers or [])
        return _FakeOrtSession(self.requested if self._enabled is None else self._enabled)


def _load(monkeypatch, tmp_path, available, providers=None, enabled=None):
    """Build a session through _ensure_session against a fake onnxruntime."""
    ort = _FakeOrt(available, enabled)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    model = tmp_path / "yolo.onnx"
    model.write_bytes(b"stub")  # available() only checks the file exists
    detector = YoloDetector(mcp_server._logger, model_path=model, class_names=NAMES,
                            providers=providers)
    detector._ensure_session()
    return ort


def test_providers_default_to_cpu_only(monkeypatch, tmp_path):
    # Unconfigured must stay CPU even on a box where a GPU provider is installed:
    # a fresh clone infers identically everywhere.
    ort = _load(monkeypatch, tmp_path, available=[DML, CPU])
    assert ort.requested == [CPU]


def test_configured_providers_pass_through_in_order(monkeypatch, tmp_path):
    ort = _load(monkeypatch, tmp_path, available=[DML, CPU], providers=[DML, CPU])
    assert ort.requested == [DML, CPU]


def test_unavailable_provider_is_dropped_with_a_warning(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        ort = _load(monkeypatch, tmp_path, available=[CPU], providers=[CUDA, CPU])

    assert ort.requested == [CPU]  # CPU survives, so the channel still runs
    assert any(CUDA in r.getMessage() and "not available" in r.getMessage()
               for r in caplog.records)


def test_cpu_is_appended_when_only_a_gpu_provider_is_configured(monkeypatch, tmp_path):
    ort = _load(monkeypatch, tmp_path, available=[DML, CPU], providers=[DML])
    assert ort.requested == [DML, CPU]  # tail fallback added for the caller


def test_every_configured_provider_unavailable_falls_back_to_cpu(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        ort = _load(monkeypatch, tmp_path, available=[CPU], providers=[DML])

    assert ort.requested == [CPU]
    assert any("falling back" in r.getMessage() for r in caplog.records)


def test_load_log_reports_what_ort_actually_enabled(monkeypatch, tmp_path, caplog):
    # Asked for DML, ORT enabled CPU only: the log must show reality, otherwise
    # a silent drop to CPU reads as "the GPU is just slow".
    with caplog.at_level(logging.INFO):
        _load(monkeypatch, tmp_path, available=[DML, CPU], providers=[DML, CPU],
              enabled=[CPU])

    loaded = [r.getMessage() for r in caplog.records if "YOLO model loaded" in r.getMessage()]
    assert loaded and loaded[0].endswith(f"providers: {CPU})")


# --- RecognizerHub integration ----------------------------------------------

class _StubCapturer:
    def __init__(self, image):
        self._image = image

    def capture_image(self, device_id):
        return self._image


class _StubYolo:
    def __init__(self, dets, avail=True):
        self._dets = dets
        self._avail = avail
        self.last_call = None

    def available(self):
        return self._avail

    def detect(self, frame, conf=0.25, classes=None, roi=None):
        self.last_call = {"conf": conf, "classes": classes, "roi": roi}
        return self._dets


def _det(label, center, score=0.8, cid=0):
    return {"label": label, "class_id": cid, "score": score,
            "bbox": [center[0] - 10, center[1] - 10, center[0] + 10, center[1] + 10],
            "center": list(center)}


def test_recognizer_yolo_hit():
    yolo = _StubYolo([_det("crate", (120, 340), cid=0)])
    hub = RecognizerHub(None, None, _StubCapturer(Image.new("RGB", (200, 400))),
                        mcp_server._logger, yolo_detector=yolo)
    hit = hub.recognize("dev", {"type": "yolo", "label": "crate", "conf": 0.3})
    assert hit["channel"] == "yolo"
    assert hit["center"] == (120, 340)
    assert hit["text"] == "crate"
    assert yolo.last_call == {"conf": 0.3, "classes": ["crate"], "roi": None}


def test_recognizer_yolo_miss_returns_none():
    hub = RecognizerHub(None, None, _StubCapturer(Image.new("RGB", (200, 400))),
                        mcp_server._logger, yolo_detector=_StubYolo([]))
    assert hub.recognize("dev", {"type": "yolo", "label": "crate"}) is None


def test_recognizer_yolo_disabled_when_no_detector():
    hub = RecognizerHub(None, None, _StubCapturer(Image.new("RGB", (10, 10))),
                        mcp_server._logger, yolo_detector=None)
    assert hub.recognize("dev", {"type": "yolo", "label": "x"}) is None


def test_recognizer_yolo_uses_passed_frame():
    yolo = _StubYolo([_det("door", (50, 60), cid=1)])

    class _Boom:
        def capture_image(self, device_id):
            raise AssertionError("should not capture when image= is given")

    hub = RecognizerHub(None, None, _Boom(), mcp_server._logger, yolo_detector=yolo)
    hit = hub.recognize("dev", {"type": "yolo"}, image=Image.new("RGB", (100, 100)))
    assert hit["center"] == (50, 60)
    assert yolo.last_call["classes"] is None   # no label -> any object


# --- task_loader validation --------------------------------------------------

def _task(recognition):
    return {"entry": "n",
            "nodes": {"n": {"recognition": recognition, "action": {"type": "none"}, "next": []}}}


def test_validate_yolo_recognition_ok_without_label():
    validate_task(_task({"type": "yolo"}))
    validate_task(_task({"type": "yolo", "label": "crate", "conf": 0.3}))


def test_validate_yolo_recognition_rejects_empty_label():
    with pytest.raises(TaskValidationError, match="'label' must be a non-empty string"):
        validate_task(_task({"type": "yolo", "label": ""}))


def test_validate_yolo_watchdog_rejects_bad_label():
    task = _task({"type": "always"})
    task["watchdogs"] = [{"type": "yolo", "label": 123}]
    with pytest.raises(TaskValidationError, match="'label' must be a non-empty string"):
        validate_task(task)


# --- MCP tools ---------------------------------------------------------------

def _modelless_yolo(tmp_path):
    """A real YoloDetector pointed at a path with no model file.

    The module-level mcp_server._yolo reflects whatever this machine happens to
    have in task/models/, so the no-model branch has to be tested against a
    detector we control — otherwise the test silently inverts on a box where a
    trained model exists.
    """
    return YoloDetector(mcp_server._logger, model_path=tmp_path / "absent.onnx")


def test_detect_objects_no_model(monkeypatch, tmp_path):
    yolo = _modelless_yolo(tmp_path)
    monkeypatch.setattr(mcp_server, "_yolo", yolo)
    assert yolo.available() is False

    result = mcp_server.detect_objects("dev")

    assert result["found"] is False
    assert result["count"] == 0
    assert result["detections"] == []
    assert "No YOLO model" in result["error"]


def test_list_yolo_classes_no_model(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_yolo", _modelless_yolo(tmp_path))

    result = mcp_server.list_yolo_classes()

    assert result["available"] is False
    assert result["classes"] == {}
    assert "No YOLO model" in result["error"]


def test_detect_objects_tool_with_model(monkeypatch):
    yolo = _StubYolo([_det("crate", (72, 82))])
    monkeypatch.setattr(mcp_server, "_yolo", yolo)
    monkeypatch.setattr(mcp_server._capturer, "capture_image",
                        lambda dev: Image.new("RGB", (200, 200)))
    result = mcp_server.detect_objects("dev", classes=["crate"])
    assert result["found"] is True
    assert result["count"] == 1
    assert result["detections"][0]["center"] == [72, 82]


def test_list_yolo_classes_tool_with_model(monkeypatch):
    class _Named(_StubYolo):
        def class_names(self):
            return {0: "crate", 1: "door"}

    monkeypatch.setattr(mcp_server, "_yolo", _Named([]))
    result = mcp_server.list_yolo_classes()
    assert result["available"] is True
    assert result["classes"] == {0: "crate", 1: "door"}


# --- named models (YoloRegistry) ---------------------------------------------
#
# Two detection domains, two model files: the default model (whose classes gate
# shipped tasks) and a named extra one for another scenario. These tests pin the
# contract that keeps the default model safe — a named model never touches it,
# and an unknown name degrades to "no hit", never an exception mid-run.

def test_registry_default_is_the_legacy_config(tmp_path):
    reg = YoloRegistry(mcp_server._logger,
                       {"model": str(tmp_path / "b.onnx"), "conf": 0.4, "input_size": 320},
                       model_dir=tmp_path)
    assert reg.names() == ["default"]
    assert reg.get() is reg.default
    assert reg.default.conf == 0.4
    assert reg.default.input_size == 320
    assert str(reg.default.model_path) == str(tmp_path / "b.onnx")


def test_registry_named_model_is_a_separate_detector(tmp_path):
    reg = YoloRegistry(mcp_server._logger, {
        "model": str(tmp_path / "primary.onnx"), "conf": 0.25,
        "models": {"objects": {"model": str(tmp_path / "objects.onnx"), "input_size": 960}},
    }, model_dir=tmp_path)
    assert reg.names() == ["default", "objects"]
    objects = reg.get("objects")
    assert objects is not reg.default
    assert str(objects.model_path) == str(tmp_path / "objects.onnx")
    assert objects.input_size == 960
    assert objects.conf == 0.25          # unset keys inherit the default model's


def test_registry_named_model_defaults_to_conventional_path(tmp_path):
    reg = YoloRegistry(mcp_server._logger, {"models": {"objects": {}}}, model_dir=tmp_path)
    assert reg.get("objects").model_path == tmp_path / "objects.onnx"


def test_registry_unknown_name_returns_none(tmp_path):
    reg = YoloRegistry(mcp_server._logger, {}, model_dir=tmp_path)
    assert reg.get("nope") is None


def test_registry_discovers_dropped_in_models(tmp_path):
    """config.yaml is optional here, so a model file alone must be enough."""
    (tmp_path / "yolo.onnx").write_bytes(b"stub")
    (tmp_path / "objects.onnx").write_bytes(b"stub")
    reg = YoloRegistry(mcp_server._logger, {"model": str(tmp_path / "yolo.onnx")})

    assert reg.names() == ["default", "objects"]
    assert reg.get("objects").model_path == tmp_path / "objects.onnx"
    assert reg.get("objects") is not reg.default


def test_registry_config_tunes_a_discovered_model(tmp_path):
    (tmp_path / "yolo.onnx").write_bytes(b"stub")
    (tmp_path / "objects.onnx").write_bytes(b"stub")
    reg = YoloRegistry(mcp_server._logger,
                       {"model": str(tmp_path / "yolo.onnx"), "conf": 0.25,
                        "models": {"objects": {"conf": 0.5}}})

    assert reg.get("objects").conf == 0.5
    assert reg.default.conf == 0.25


def test_registry_providers_reach_every_detector():
    # Execution providers describe the machine, not a model, so a named model
    # inherits `yolo.providers` unless it deliberately says otherwise.
    reg = YoloRegistry(mcp_server._logger, {
        "providers": [DML, CPU],
        "models": {"objects": {}, "props": {"providers": [CPU]}},
    })
    assert reg.default.providers == [DML, CPU]
    assert reg.get("objects").providers == [DML, CPU]
    assert reg.get("props").providers == [CPU]


def test_registry_without_providers_is_cpu_only():
    reg = YoloRegistry(mcp_server._logger, {"models": {"objects": {}}})
    assert reg.default.providers == [CPU]
    assert reg.get("objects").providers == [CPU]


# --- model manifest (models.json) --------------------------------------------
#
# The manifest is purely-additive version metadata: it must never gate whether
# a model loads (missing/malformed manifest -> {} and a log line, never raised).

def test_load_model_manifest_reads_json(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({"yolo.onnx": {"version": "v2", "date": "2026-08-07", "notes": "x"}}),
        encoding="utf-8")
    manifest = load_model_manifest(tmp_path, logging.getLogger("test"))
    assert manifest == {"yolo.onnx": {"version": "v2", "date": "2026-08-07", "notes": "x"}}


def test_load_model_manifest_missing_file_returns_empty(tmp_path):
    assert load_model_manifest(tmp_path, logging.getLogger("test")) == {}


def test_load_model_manifest_bad_json_returns_empty_and_warns(tmp_path, caplog):
    (tmp_path / "models.json").write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="test"):
        manifest = load_model_manifest(tmp_path, logging.getLogger("test"))
    assert manifest == {}
    assert any("unreadable" in r.message for r in caplog.records)


def test_load_model_manifest_non_object_json_returns_empty(tmp_path):
    (tmp_path / "models.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_model_manifest(tmp_path, logging.getLogger("test")) == {}


def test_registry_model_info_carries_manifest_entry(tmp_path):
    (tmp_path / "yolo.onnx").write_bytes(b"stub")
    (tmp_path / "objects.onnx").write_bytes(b"stub")
    (tmp_path / "models.json").write_text(json.dumps({
        "yolo.onnx": {"version": "v2", "date": "2026-08-07", "notes": "buildings"},
    }), encoding="utf-8")
    reg = YoloRegistry(mcp_server._logger, {"model": str(tmp_path / "yolo.onnx")},
                       model_dir=tmp_path)

    default_info = reg.model_info()
    assert default_info["path"] == str(tmp_path / "yolo.onnx")
    assert default_info["manifest"] == {"version": "v2", "date": "2026-08-07",
                                        "notes": "buildings"}
    # objects.onnx has no manifest entry -> empty, not missing/raised.
    assert reg.model_info("objects")["manifest"] == {}
    assert reg.manifest() == {"yolo.onnx": {"version": "v2", "date": "2026-08-07",
                                            "notes": "buildings"}}


def test_registry_model_info_unknown_name_is_empty_dict(tmp_path):
    reg = YoloRegistry(mcp_server._logger, {}, model_dir=tmp_path)
    assert reg.model_info("nope") == {}


def test_registry_without_manifest_file_has_empty_manifest_entries(tmp_path):
    (tmp_path / "yolo.onnx").write_bytes(b"stub")
    reg = YoloRegistry(mcp_server._logger, {"model": str(tmp_path / "yolo.onnx")},
                       model_dir=tmp_path)
    assert reg.model_info()["manifest"] == {}
    assert reg.manifest() == {}


def test_list_yolo_classes_tool_includes_manifest_fields(monkeypatch, tmp_path):
    (tmp_path / "yolo.onnx").write_bytes(b"stub")
    (tmp_path / "models.json").write_text(json.dumps({
        "yolo.onnx": {"version": "v2", "date": "2026-08-07", "notes": "buildings"},
    }), encoding="utf-8")
    reg = YoloRegistry(mcp_server._logger, {"model": str(tmp_path / "yolo.onnx")},
                       model_dir=tmp_path)

    class _Named(_StubYolo):
        def class_names(self):
            return {0: "crate"}

    monkeypatch.setattr(mcp_server, "_yolo", _Named([]))
    monkeypatch.setattr(mcp_server, "_yolo_registry", reg)

    result = mcp_server.list_yolo_classes()
    assert result["available"] is True
    assert result["version"] == "v2"
    assert result["date"] == "2026-08-07"
    assert result["notes"] == "buildings"


def test_list_yolo_classes_tool_omits_manifest_fields_when_absent(monkeypatch, tmp_path):
    reg = YoloRegistry(mcp_server._logger, {}, model_dir=tmp_path)

    class _Named(_StubYolo):
        def class_names(self):
            return {0: "crate"}

    monkeypatch.setattr(mcp_server, "_yolo", _Named([]))
    monkeypatch.setattr(mcp_server, "_yolo_registry", reg)

    result = mcp_server.list_yolo_classes()
    assert result["available"] is True
    assert "version" not in result
    assert "date" not in result
    assert "notes" not in result


def test_recognizer_yolo_routes_to_named_model():
    default_model = _StubYolo([_det("crate", (10, 10))])
    objects = _StubYolo([_det("crate", (400, 900), cid=1)])

    class _Reg:
        def get(self, name=None):
            return {"objects": objects}.get(name)

    hub = RecognizerHub(None, None, _StubCapturer(Image.new("RGB", (200, 400))),
                        mcp_server._logger, yolo_detector=default_model,
                        yolo_registry=_Reg())

    hit = hub.recognize("dev", {"type": "yolo", "model": "objects", "label": "crate"})
    assert hit["center"] == (400, 900)
    assert hit["text"] == "crate"
    assert objects.last_call["classes"] == ["crate"]
    assert default_model.last_call is None   # the default model was never asked


def test_recognizer_yolo_unknown_model_is_a_miss_not_a_crash():
    class _Reg:
        def get(self, name=None):
            return None

    hub = RecognizerHub(None, None, _StubCapturer(Image.new("RGB", (10, 10))),
                        mcp_server._logger,
                        yolo_detector=_StubYolo([_det("crate", (1, 1))]),
                        yolo_registry=_Reg())
    assert hub.recognize("dev", {"type": "yolo", "model": "ghost"}) is None


def test_validate_yolo_recognition_accepts_model():
    validate_task(_task({"type": "yolo", "model": "objects", "label": "crate"}))


def test_validate_yolo_recognition_rejects_empty_model():
    with pytest.raises(TaskValidationError, match="'model' must be a non-empty string"):
        validate_task(_task({"type": "yolo", "model": ""}))


def test_watchdog_spec_forwards_model_to_the_hub():
    from task.task_engine import WATCHDOG_SPEC_KEYS
    assert "model" in WATCHDOG_SPEC_KEYS


def test_detect_objects_tool_routes_by_model_name(monkeypatch):
    default_model = _StubYolo([_det("crate", (1, 1))])
    objects = _StubYolo([_det("crate", (300, 800))])

    class _Reg:
        def get(self, name=None):
            return {"objects": objects}.get(name)

        def names(self):
            return ["default", "objects"]

    monkeypatch.setattr(mcp_server, "_yolo", default_model)
    monkeypatch.setattr(mcp_server, "_yolo_registry", _Reg())
    monkeypatch.setattr(mcp_server._capturer, "capture_image",
                        lambda dev: Image.new("RGB", (200, 200)))

    result = mcp_server.detect_objects("dev", model="objects")
    assert result["model"] == "objects"
    assert result["detections"][0]["label"] == "crate"
    assert default_model.last_call is None

    unknown = mcp_server.detect_objects("dev", model="ghost")
    assert unknown["found"] is False
    assert "Unknown YOLO model" in unknown["error"]


def test_validate_yolo_watchdog_rejects_empty_model():
    """An empty `model` would silently fall back to the default model."""
    task = _task({"type": "always"})
    task["watchdogs"] = [{"type": "yolo", "label": "crate", "model": ""}]
    with pytest.raises(TaskValidationError, match="'model' must be a non-empty string"):
        validate_task(task)
