from __future__ import annotations

import ast
import io
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from core.logger import LOGGER_NAME, log_event

# YOLO object detection: the fourth perception channel. Where template matching
# breaks on scale/rotation/occlusion and the text channels can't read sprites at
# all, a trained detector locates and *classifies* buildings/units robustly.
#
# Inference runs on onnxruntime (already a dep via rapidocr) — no PyTorch. The
# model is a local, opt-in asset: train with ultralytics, `yolo export
# format=onnx`, drop the .onnx in. available() is False until a model exists, so
# the channel stays inert (and tasks fall back to the other channels) otherwise.
#
# 钉死约定：本模块推理只走 onnxruntime InferenceSession，禁止改用 ultralytics 的
# predict API——其默认 verbose=True 会往 stdout 打进度条/日志，而本项目是 stdio
# MCP 服务器，stdout 是 JSON-RPC 通道，会被直接打坏。ultralytics 仅允许出现在
# training/ 下的离线训练/导出脚本里，不得进入本模块（或任何运行时推理路径）。
DEFAULT_MODEL_PATH = "task/models/yolo.onnx"
DEFAULT_MODEL_NAME = "default"
#: Version-tracking manifest that lives next to the .onnx files (see
#: task/models/README.md). Purely additive metadata — never gates loading a model.
MODEL_MANIFEST_NAME = "models.json"
DEFAULT_INPUT_SIZE = 640
DEFAULT_CONF = 0.25
DEFAULT_IOU = 0.45
LETTERBOX_PAD = 114  # ultralytics' canonical grey padding value
# The one execution provider every onnxruntime build ships. Kept as the tail of
# whatever provider list is configured, so a config written for a GPU box (or a
# provider that got uninstalled) still runs on plain CPU instead of throwing.
CPU_PROVIDER = "CPUExecutionProvider"
DEFAULT_PROVIDERS: Tuple[str, ...] = (CPU_PROVIDER,)

ImageLike = Union[bytes, bytearray, "object"]

#: Distinct class names listed in one detect() log line before it is truncated.
LOG_CLASS_LIMIT = 6


def load_model_manifest(models_dir: Union[str, Path],
                        logger: Optional[logging.Logger] = None) -> Dict[str, Dict]:
    """Read task/models/models.json: filename -> {version, date, notes, ...}.

    Purely additive metadata for YoloRegistry.model_info()/manifest() — it never
    gates whether a model loads. A missing file is the common case (fresh clone
    before anyone wrote one, or a tmp test dir) and only logs at DEBUG; a file
    that exists but fails to parse is an authoring mistake worth a WARNING.
    Either way this never raises — a hand-edited JSON typo must not take the
    YOLO channel down.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    path = Path(models_dir) / MODEL_MANIFEST_NAME
    if not path.is_file():
        log.debug("No model manifest at %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Model manifest at %s is unreadable (%s); ignoring", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("Model manifest at %s is not a JSON object; ignoring", path)
        return {}
    return data


def _class_summary(dets: List[Dict]) -> Optional[str]:
    """`button:3,icon:1` — what was detected, as one short log token."""
    if not dets:
        return None
    counts = Counter(str(d.get("label", "?")) for d in dets)
    parts = [f"{name}:{n}" for name, n in counts.most_common(LOG_CLASS_LIMIT)]
    if len(counts) > LOG_CLASS_LIMIT:
        parts.append("...")
    return ",".join(parts)


class YoloDetector:
    """Run a YOLOv8/v11 .onnx model via onnxruntime; return classified boxes.

    Mirrors OcrEngine/TemplateMatcher: onnxruntime is lazy-imported and the
    session lazy-built, so startup stays fast and the channel is optional.
    available() reports False when onnxruntime is missing OR no model file is
    present, and callers fall back to the text/template channels.

    detect() returns, best score first:
      [{label, class_id, score, bbox: [x1,y1,x2,y2], center: [x,y]}]
    in absolute screen pixels. Class names come from the model's embedded
    metadata (ultralytics writes them) unless overridden via class_names.

    providers: onnxruntime execution providers, best first (config
    `yolo.providers`). Defaults to CPU only — the portable choice that needs no
    extra install, so a fresh clone runs identically everywhere. A machine with
    the hardware can opt into e.g. ["DmlExecutionProvider", "CPUExecutionProvider"]
    for a scenario that infers on every frame; anything this onnxruntime build
    doesn't have is dropped with a warning and CPU always remains as the tail
    fallback.
    """

    def __init__(self, logger, model_path: Union[str, Path] = DEFAULT_MODEL_PATH,
                 class_names: Optional[Union[Sequence[str], Dict[int, str]]] = None,
                 conf: float = DEFAULT_CONF, iou: float = DEFAULT_IOU,
                 input_size: int = DEFAULT_INPUT_SIZE,
                 providers: Optional[Sequence[str]] = None):
        self.logger = logger
        self.model_path = Path(model_path)
        self.conf = float(conf)
        self.iou = float(iou)
        self.input_size = int(input_size)
        # None / [] both mean "unconfigured" -> the CPU-only default.
        self.providers: List[str] = (
            [str(p) for p in providers] if providers else list(DEFAULT_PROVIDERS)
        )
        self._import_failed = False
        self._session = None
        self._input_name: Optional[str] = None
        self._size: Tuple[int, int] = (self.input_size, self.input_size)  # (w, h)
        self._class_names: Dict[int, str] = self._coerce_names(class_names)
        self._names_overridden = bool(self._class_names)

    # ---------- availability ----------

    def available(self) -> bool:
        """True only when onnxruntime imports AND a model file is present."""
        if self._import_failed:
            return False
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self._import_failed = True
            self.logger.warning("onnxruntime not installed; YOLO channel disabled")
            return False
        return self.model_path.is_file()

    def class_names(self) -> Dict[int, str]:
        """Class id -> name map (loads the model once to read embedded names)."""
        if self.available():
            self._ensure_session()
        return dict(self._class_names)

    # ---------- public API ----------

    def detect(
        self,
        image: ImageLike,
        conf: Optional[float] = None,
        iou: Optional[float] = None,
        classes: Optional[Sequence[Union[str, int]]] = None,
        roi: Optional[Sequence[int]] = None,
    ) -> List[Dict]:
        """Detect objects in a screenshot (PIL Image / PNG bytes / BGR ndarray).

        classes: optional whitelist of class names or ids to keep. roi: optional
        [x1,y1,x2,y2] to detect within; boxes are reported in full-screen pixels.
        """
        if not self.available():
            return []
        self._ensure_session()
        conf = self.conf if conf is None else float(conf)
        iou = self.iou if iou is None else float(iou)
        started = time.perf_counter()

        bgr = self._to_bgr(image)
        off_x, off_y = 0, 0
        if roi:
            x1, y1, x2, y2 = (int(v) for v in roi)
            bgr = bgr[y1:y2, x1:x2]
            off_x, off_y = x1, y1
        if bgr.size == 0:
            return []

        class_filter = self._resolve_class_filter(classes)
        if class_filter is not None and not class_filter:
            return []  # caller asked for classes the model doesn't have

        blob, scale, pad = self._preprocess(bgr, self._size)
        try:
            output = self._session.run(None, {self._input_name: blob})[0]
        except Exception as exc:  # noqa: BLE001 - surface as empty, never crash a run
            self.logger.warning("YOLO inference failed: %s", exc)
            return []

        dets = self._postprocess(output, scale, pad, bgr.shape[:2], conf, iou, class_filter)
        if off_x or off_y:
            for d in dets:
                d["bbox"] = [d["bbox"][0] + off_x, d["bbox"][1] + off_y,
                             d["bbox"][2] + off_x, d["bbox"][3] + off_y]
                d["center"] = [d["center"][0] + off_x, d["center"][1] + off_y]
        # One summary line per inference — never one per box: a crowded frame
        # (a dozen detections) would otherwise bury the flow in run.log.
        log_event(
            self.logger, "yolo_detect", n=len(dets), classes=_class_summary(dets),
            best=dets[0]["score"] if dets else None,
            ms=int((time.perf_counter() - started) * 1000),
        )
        return dets

    # ---------- session ----------

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort

        providers = self._resolve_providers(ort)
        self._session = ort.InferenceSession(os.fspath(self.model_path), providers=providers)
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        shape = inp.shape  # typically [1, 3, H, W]; H/W may be dynamic strings/-1
        h = shape[2] if isinstance(shape[2], int) and shape[2] > 0 else self.input_size
        w = shape[3] if isinstance(shape[3], int) and shape[3] > 0 else self.input_size
        self._size = (int(w), int(h))
        if not self._names_overridden:
            self._class_names = self._read_names_from_meta(self._session)
        # Report what ORT actually enabled, not what was asked for: a provider
        # can be present yet refuse the model and silently drop to the next one.
        getter = getattr(self._session, "get_providers", None)
        active = list(getter()) if callable(getter) else providers
        self.logger.info("YOLO model loaded: %s (input %dx%d, %d classes, providers: %s)",
                         self.model_path, self._size[0], self._size[1],
                         len(self._class_names), ", ".join(active))

    def _resolve_providers(self, ort) -> List[str]:
        """Configured providers, filtered to what this onnxruntime build has.

        A provider list is a portability hazard: `yolo.providers` may name a
        GPU provider that this box (or this ORT wheel) doesn't have, and
        InferenceSession raises on an unknown name instead of skipping it —
        which would take the whole YOLO channel down. So filter against
        get_available_providers(), say out loud what was dropped (a silently
        ignored GPU setting looks like "the GPU is just slow"), and keep CPU at
        the tail so there is always something left to run on.
        """
        try:
            available = list(ort.get_available_providers())
        except Exception as exc:  # noqa: BLE001 - unexpected ORT build: use CPU
            self.logger.warning("Cannot query onnxruntime providers (%s); using %s",
                                exc, CPU_PROVIDER)
            return [CPU_PROVIDER]

        resolved: List[str] = []
        for name in self.providers:
            if name in resolved:
                continue
            if name not in available:
                self.logger.warning(
                    "YOLO provider %s is not available in this onnxruntime install "
                    "(available: %s); skipping it", name, ", ".join(available) or "none")
                continue
            resolved.append(name)

        if not resolved:
            self.logger.warning("No configured YOLO provider is available; falling back to %s",
                                CPU_PROVIDER)
            return [CPU_PROVIDER]
        if CPU_PROVIDER not in resolved:
            resolved.append(CPU_PROVIDER)  # tail fallback, always
        return resolved

    @staticmethod
    def _read_names_from_meta(session) -> Dict[int, str]:
        """ultralytics stores class names as a stringified dict in ONNX metadata."""
        try:
            raw = session.get_modelmeta().custom_metadata_map.get("names")
            if not raw:
                return {}
            parsed = ast.literal_eval(raw)
            return {int(k): str(v) for k, v in parsed.items()}
        except Exception:  # noqa: BLE001 - missing/odd metadata: just have no names
            return {}

    def _name_of(self, class_id: int) -> str:
        return self._class_names.get(class_id, f"class_{class_id}")

    def _resolve_class_filter(
        self, classes: Optional[Sequence[Union[str, int]]]
    ) -> Optional[set]:
        if not classes:
            return None
        name_to_id = {v: k for k, v in self._class_names.items()}
        ids = set()
        for c in classes:
            if isinstance(c, int):
                ids.add(c)
            elif isinstance(c, str) and c in name_to_id:
                ids.add(name_to_id[c])
        return ids

    # ---------- pre / post processing (separable for testing) ----------

    def _preprocess(self, bgr, size: Tuple[int, int]):
        """Letterbox-resize to (w,h), BGR->RGB, /255, HWC->NCHW float32.

        Returns (blob, scale, (pad_x, pad_y)) so postprocess can invert it.
        """
        import cv2
        import numpy as np

        target_w, target_h = size
        h, w = bgr.shape[:2]
        scale = min(target_w / w, target_h / h)
        rw, rh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(bgr, (rw, rh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((target_h, target_w, 3), LETTERBOX_PAD, dtype=np.uint8)
        pad_x, pad_y = (target_w - rw) // 2, (target_h - rh) // 2
        canvas[pad_y:pad_y + rh, pad_x:pad_x + rw] = resized
        blob = canvas[:, :, ::-1].astype(np.float32) / 255.0  # BGR->RGB, normalize
        blob = np.ascontiguousarray(blob.transpose(2, 0, 1)[None])  # HWC->NCHW
        return blob, scale, (pad_x, pad_y)

    def _postprocess(self, output, scale, pad, orig_shape, conf, iou, class_filter):
        """Decode a YOLOv8/v11 ONNX output into screen-space detections.

        Handles output laid out as (4+nc, N) or (N, 4+nc): boxes are cx,cy,w,h in
        letterbox pixels, the remaining channels are per-class confidences (the
        ultralytics export bakes in decoding + sigmoid). Undoes the letterbox,
        clips to the frame, and runs per-class NMS.
        """
        import cv2
        import numpy as np

        out = np.squeeze(np.asarray(output))
        if out.ndim != 2:
            return []
        if out.shape[0] < out.shape[1]:  # (4+nc, N) -> (N, 4+nc); boxes >> channels
            out = out.T
        nc = out.shape[1] - 4
        if nc <= 0:
            return []

        boxes = out[:, :4]
        cls_scores = out[:, 4:]
        class_ids = np.argmax(cls_scores, axis=1)
        confs = cls_scores[np.arange(len(cls_scores)), class_ids]

        keep = confs >= conf
        if class_filter is not None:
            keep &= np.isin(class_ids, list(class_filter))
        boxes, class_ids, confs = boxes[keep], class_ids[keep], confs[keep]
        if len(boxes) == 0:
            return []

        pad_x, pad_y = pad
        cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - bw / 2 - pad_x) / scale
        y1 = (cy - bh / 2 - pad_y) / scale
        x2 = (cx + bw / 2 - pad_x) / scale
        y2 = (cy + bh / 2 - pad_y) / scale
        H, W = orig_shape
        x1 = np.clip(x1, 0, W); y1 = np.clip(y1, 0, H)
        x2 = np.clip(x2, 0, W); y2 = np.clip(y2, 0, H)

        dets: List[Dict] = []
        for c in np.unique(class_ids):
            idx = np.where(class_ids == c)[0]
            wh = [[float(x1[i]), float(y1[i]), float(x2[i] - x1[i]), float(y2[i] - y1[i])]
                  for i in idx]
            sc = [float(confs[i]) for i in idx]
            nms = cv2.dnn.NMSBoxes(wh, sc, conf, iou)
            for j in np.asarray(nms).flatten():
                i = int(idx[int(j)])
                bx1, by1, bx2, by2 = int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i])
                dets.append({
                    "label": self._name_of(int(c)),
                    "class_id": int(c),
                    "score": round(float(confs[i]), 4),
                    "bbox": [bx1, by1, bx2, by2],
                    "center": [(bx1 + bx2) // 2, (by1 + by2) // 2],
                })
        dets.sort(key=lambda d: d["score"], reverse=True)
        return dets

    # ---------- helpers ----------

    @staticmethod
    def _coerce_names(class_names) -> Dict[int, str]:
        if not class_names:
            return {}
        if isinstance(class_names, dict):
            return {int(k): str(v) for k, v in class_names.items()}
        return {i: str(n) for i, n in enumerate(class_names)}

    @staticmethod
    def _to_bgr(image: ImageLike):
        """PIL Image / PNG bytes / ndarray -> contiguous BGR uint8 array."""
        import numpy as np
        from PIL import Image

        if isinstance(image, (bytes, bytearray)):
            image = Image.open(io.BytesIO(image))
        if isinstance(image, Image.Image):
            return np.ascontiguousarray(np.array(image.convert("RGB"))[:, :, ::-1])
        return np.ascontiguousarray(np.asarray(image))  # assume already BGR


class YoloRegistry:
    """Named YOLO models, one lazily-built detector each.

    Detection domains stay in separate model files on purpose. A model whose
    classes gate a shipped task and a model serving some other scenario are
    trained on different footage and iterate on different schedules, so folding
    them into one checkpoint would put the task-critical classes at risk on
    every unrelated retrain — and would additionally require re-auditing every
    frame of the first domain for unlabelled objects of the second, since an
    unlabelled object is negative supervision.

    The default model keeps the historical config keys (`yolo.model`, `conf`,
    ...), so every existing call site and task JSON behaves exactly as before.

    Extra models are **discovered from the model directory**, like templates in
    task/templates/: every other `*.onnx` next to the default model registers
    under its filename stem (`objects.onnx` -> "objects"). config.yaml is
    optional in this project — a run with no config file at all still has to see
    a model that was dropped into task/models/, or the named-model path would
    only work on machines that happen to have written a config. `yolo.models.
    <name>` then only exists to *tune* a model (conf, input_size, ...) or to
    point one at a path outside that directory.

    Detectors are constructed eagerly (cheap — no ORT access) but their
    onnxruntime sessions stay lazy, so an unused model costs nothing.
    """

    def __init__(self, logger, config: Optional[Dict] = None,
                 model_dir: Optional[Union[str, Path]] = None):
        self.logger = logger
        cfg = dict(config or {})
        extra = dict(cfg.pop("models", None) or {})
        default = self._build(logger, cfg, DEFAULT_MODEL_PATH, {})
        self._detectors: Dict[str, YoloDetector] = {DEFAULT_MODEL_NAME: default}

        root = Path(model_dir) if model_dir else default.model_path.parent
        # Reading models.json is a small local file read, not model loading —
        # fine to do eagerly here without breaking the lazy-session contract.
        self._manifest: Dict[str, Dict] = load_model_manifest(root, logger)
        self._info: Dict[str, Dict] = {DEFAULT_MODEL_NAME: self._make_info(default)}

        for path in sorted(root.glob("*.onnx")) if root.is_dir() else []:
            if path.name == default.model_path.name:
                continue
            det = self._build(logger, {"model": str(path)}, str(path), cfg)
            self._detectors[path.stem] = det
            self._info[path.stem] = self._make_info(det)

        for name, sub in extra.items():
            if name == DEFAULT_MODEL_NAME:
                self.logger.warning(
                    "yolo.models.%s is ignored; configure the default model with the "
                    "top-level yolo.* keys", DEFAULT_MODEL_NAME)
                continue
            det = self._build(logger, dict(sub or {}), str(root / f"{name}.onnx"), cfg)
            self._detectors[str(name)] = det
            self._info[str(name)] = self._make_info(det)

    def _make_info(self, detector: "YoloDetector") -> Dict:
        """{path, manifest} for one registered detector (manifest may be {})."""
        return {
            "path": str(detector.model_path),
            "manifest": dict(self._manifest.get(detector.model_path.name, {})),
        }

    @staticmethod
    def _build(logger, cfg: Dict, default_path: str, fallback: Dict) -> YoloDetector:
        """Build one detector; unset keys inherit the default model's settings."""
        def pick(key, default):
            return cfg.get(key, fallback.get(key, default))

        return YoloDetector(
            logger,
            model_path=cfg.get("model", default_path),
            class_names=cfg.get("classes"),
            conf=pick("conf", DEFAULT_CONF),
            iou=pick("iou", DEFAULT_IOU),
            input_size=pick("input_size", DEFAULT_INPUT_SIZE),
            # Execution providers are a property of the machine, not of a model,
            # so an extra model inherits them unless it says otherwise.
            providers=pick("providers", None),
        )

    def get(self, name: Optional[str] = None) -> Optional[YoloDetector]:
        """Detector for `name` (None/"" -> default). None when the name is unknown."""
        key = name or DEFAULT_MODEL_NAME
        det = self._detectors.get(key)
        if det is None:
            self.logger.warning("Unknown YOLO model '%s'; known: %s",
                                key, ", ".join(sorted(self._detectors)))
        return det

    @property
    def default(self) -> YoloDetector:
        return self._detectors[DEFAULT_MODEL_NAME]

    def names(self) -> List[str]:
        return sorted(self._detectors)

    def available(self) -> Dict[str, bool]:
        return {n: d.available() for n, d in self._detectors.items()}

    def model_info(self, name: Optional[str] = None) -> Dict:
        """{path, manifest} for a registered model; {} when `name` is unknown.

        `manifest` is the model's models.json entry (version/date/notes/...),
        or {} when the model has no manifest entry — this never gates
        detection, it is metadata for callers like list_yolo_classes().
        """
        key = name or DEFAULT_MODEL_NAME
        info = self._info.get(key)
        return dict(info) if info is not None else {}

    def manifest(self) -> Dict[str, Dict]:
        """The raw filename -> manifest-entry map, as loaded from models.json."""
        return dict(self._manifest)
