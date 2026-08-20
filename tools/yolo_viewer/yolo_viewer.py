# -*- coding: utf-8 -*-
"""YOLO 实时识别验证窗口 — 连真机、抓帧、跑 YOLO 检测(全类)，
在一个 cv2 窗口里叠加画框，供人眼实时判断检出好坏。

这是独立的验证工具(平时用不上，只在需要肉眼验证 YOLO 检出时才拉起)，
不挂在主流程(main.py/mcp_server.py)上，也不被库代码 import。用来给
"某类检出弱"这类问题当尺子看：调低 conf 阈值能不能看见弱检出、框跟得准不准。

用法:
    tools\\yolo_viewer\\run.bat [参数...]
    或
    <python> tools\\yolo_viewer\\yolo_viewer.py [参数...]
    (<python> = 项目 conda 环境的解释器；run.bat 认 PYTHON 环境变量)

参数(全部可选，见 --help):
    --device      真机设备号，默认取 adb 认到的第一台(也可用 ADB_DEVICE 环境变量)
    --model       YOLO 模型路径，默认 <repo>/task/models/yolo.onnx
    --conf        初始 conf 阈值，默认 0.20
    --hide        隐藏这些类的框(可多个)，减少干扰类刷屏

交互键(窗口需处于前台，鼠标点一下窗口再按键):
    q / ESC   退出(干净释放 scrcpy 流 + 关窗口)
    +         conf 阈值 +0.05 (上限 0.80)，立即生效重画
    -         conf 阈值 -0.05 (下限 0.05)，立即生效重画
    s         把当前带标注的整帧存到 outputs\\yolo_viewer\\shots_marked\\<时间戳>.jpg
    h         切换 --hide 指定的类是否显示

窗口内容:
    - 实时画面(自适应缩放到屏幕 90% 内，保持手机原生长宽比，不硬拉伸)
    - 每个 YOLO 检测框 + "类名 conf" 文字，不同类不同颜色
    - 左上角 HUD：conf 阈值 / fps / 每类检出计数
    - 屏幕中心准星十字
    - 顶部文本(OCR，尽力而为，失败不阻塞)
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# --- 从本文件位置推导仓库根目录，换机器/换盘也能跑 ---
# 本文件位于 <repo>/tools/yolo_viewer/yolo_viewer.py
REPO_ROOT = Path(__file__).resolve().parents[2]

# --- 让本脚本能 import 项目的 perception 包 ---
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- adb 不在系统 PATH，先加进去(env ADB_DIR 可覆盖默认位置) ---
DEFAULT_ADB_DIR = os.environ.get(
    "ADB_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools"),
)
os.environ["PATH"] = DEFAULT_ADB_DIR + os.pathsep + os.environ.get("PATH", "")

from perception.ocr_engine import OcrEngine  # noqa: E402
from perception.screenshot_capturer import ScreenshotCapturer  # noqa: E402
from perception.yolo_detector import YoloDetector  # noqa: E402

DEFAULT_MODEL = str(REPO_ROOT / "task" / "models" / "yolo.onnx")
DEFAULT_CONF = 0.20

OUT_DIR = str(REPO_ROOT / "outputs" / "yolo_viewer")
SHOT_DIR = os.path.join(OUT_DIR, "shots_marked")

DISPLAY_MARGIN = 0.90         # 窗口最多占屏幕这么大比例，剩下留边(自适应任意手机/屏幕分辨率)
CONF_MIN, CONF_MAX, CONF_STEP = 0.05, 0.80, 0.05
OBJ_EVERY_N = 15               # 顶部文本 OCR 每 N 帧跑一次
FPS_WINDOW = 20

# 类别颜色 (BGR)：按类名哈希取色，加类不用改代码，同一类每次运行颜色也固定。
PALETTE = [
    (255, 0, 0), (0, 255, 0), (255, 0, 255), (255, 255, 0),
    (0, 128, 255), (128, 0, 255), (0, 255, 128), (255, 128, 0),
]


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yolo_viewer.py",
        description=(
            "YOLO 实时识别验证窗口 — 连真机、抓帧、跑 YOLO 检测(全类)，"
            "在一个 cv2 窗口里叠加画框，供人眼实时判断检出好坏。偶发使用的独立验证工具。"
        ),
        epilog=(
            "交互键(窗口需处于前台，鼠标点一下窗口再按键):\n"
            "  q / ESC   退出(干净释放 scrcpy 流 + 关窗口)\n"
            "  +         conf 阈值 +0.05 (上限 0.80)，立即生效重画\n"
            "  -         conf 阈值 -0.05 (下限 0.05)，立即生效重画\n"
            "  s         把当前带标注的整帧存到 outputs\\yolo_viewer\\shots_marked\\<时间戳>.jpg\n"
            "  h         切换 --hide 指定的类是否显示\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--device", default=None,
        help="真机设备号 (默认取 ADB_DEVICE 环境变量，再退到 adb 认到的第一台)",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"YOLO 模型路径 (默认 {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--conf", type=float, default=DEFAULT_CONF,
        help=f"初始 conf 阈值 (默认 {DEFAULT_CONF})",
    )
    parser.add_argument(
        "--hide", nargs="*", default=[],
        help="隐藏这些类的框(类名，可多个)。用来把刷屏的干扰类先关掉，按 h 可随时切回来",
    )
    parser.add_argument(
        "--backend", default="scrcpy", choices=["scrcpy", "screencap"],
        help="抓帧后端: scrcpy=帧流(快~10fps,H.264有损略糊) / "
             "screencap=逐帧精确无损(清晰但慢~2fps,细看检出用这个)。默认 scrcpy",
    )
    return parser.parse_args(argv)


def _resolve_device(explicit=None) -> str:
    """设备号：命令行 > ADB_DEVICE 环境变量 > `adb devices` 的第一台。

    独立验证工具没有配置文件可读，写死设备号又只能在作者本机跑，所以就地问一次 adb。
    问不到就返回空串，让下游 capture 自己报错(信息比这里瞎猜一个设备号有用)。
    """
    if explicit:
        return explicit
    from_env = os.environ.get("ADB_DEVICE")
    if from_env:
        return from_env
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[device] 无法执行 adb devices ({exc})，请用 --device 指定")
        return ""
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    print("[device] adb 没认到在线设备，请先连接真机或用 --device 指定")
    return ""


def _color_for(label: str):
    idx = int(hashlib.md5(label.encode("utf-8")).hexdigest(), 16) % len(PALETTE)
    return PALETTE[idx]


def _read_top_text(ocr: OcrEngine, img) -> str:
    """尽力读一下画面顶部的文本，失败/无结果返回空串，绝不抛出。"""
    try:
        roi = [0, 0, img.width, max(1, int(img.height * 0.15))]
        results = ocr.recognize(img, roi=roi)
    except Exception as exc:  # noqa: BLE001 - 纯展示用途，OCR 出错就跳过
        print(f"[obj-ocr] failed: {exc}")
        return ""
    if not results:
        return ""
    results = sorted(results, key=lambda r: (r["bbox"][1], r["bbox"][0]))
    text = "".join(r["text"] for r in results)
    return text[:60]


def _screen_size(default=(1920, 1080)):
    """本机屏幕物理分辨率 (w, h)。先置 DPI 感知让读数是物理像素，失败退默认。"""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:  # noqa: BLE001 - 老系统无 shcore，退回 user32
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass
        user32 = ctypes.windll.user32
        w = int(user32.GetSystemMetrics(0))
        h = int(user32.GetSystemMetrics(1))
        if w > 0 and h > 0:
            return w, h
    except Exception as exc:  # noqa: BLE001 - 非 Windows / 无 GUI，退默认
        print(f"[screen] size probe failed ({exc}); fallback {default}")
    return default


def _put_text_outlined(frame, text, org, scale=0.6, color=(255, 255, 255), thickness=1):
    import cv2

    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _draw_label(frame, text, org, color, scale=0.45, thickness=1, alpha=0.55):
    """框顶画一个半透明底色条 + 反差文字：清晰可读又不过度遮挡目标。"""
    import cv2
    import numpy as np

    (tw, th), _base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    fh, fw = frame.shape[:2]
    x = max(0, min(int(org[0]), fw - tw - 6))
    y = max(th + 6, int(org[1]))
    y0, y1 = max(0, y - th - 6), min(fh, y)
    x0, x1 = x, min(fw, x + tw + 6)
    roi = frame[y0:y1, x0:x1]
    if roi.size:
        overlay = np.empty_like(roi)
        overlay[:] = color
        cv2.addWeighted(overlay, alpha, roi, 1.0 - alpha, 0.0, roi)  # 半透明底条
    txt_color = (0, 0, 0) if sum(color) > 380 else (255, 255, 255)  # 亮底黑字 / 暗底白字
    cv2.putText(frame, text, (x + 3, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale,
                txt_color, thickness, cv2.LINE_AA)


def main(argv=None) -> int:
    args = _parse_args(argv)

    import cv2
    import numpy as np

    os.makedirs(SHOT_DIR, exist_ok=True)

    logger = logging.getLogger("yolo_viewer")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    device = _resolve_device(args.device)
    model_path = args.model
    conf_thresh = args.conf
    hidden = {str(name) for name in (args.hide or [])}

    print(f"[init] device={device}")
    print(f"[init] repo_root={REPO_ROOT}")
    ocr = OcrEngine(logger)
    ocr.ensure_loaded()  # onnxruntime 首次初始化必须在 scrcpy 流起来之前预热，硬约束

    det = YoloDetector(logger, model_path=model_path)

    print(f"[init] model available={det.available()} classes={det.class_names()}")

    cap = ScreenshotCapturer(
        logger,
        output_dir=os.path.join(OUT_DIR, "shots"),
        capture_config={"backend": args.backend},
        stream_warmup=ocr.ensure_loaded,
    )
    print(f"[init] capture backend={args.backend}")

    win = "YOLO Viewer"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    screen_w, screen_h = _screen_size()
    print(f"[screen] {screen_w}x{screen_h}, 窗口自适应到屏幕 {int(DISPLAY_MARGIN*100)}% 内(保持手机比例)")
    win_sized = False

    show_hidden = False        # --hide 的类默认藏起来，按 h 可临时显示
    frame_idx = 0
    last_obj_text = ""
    fps_hist: deque = deque(maxlen=FPS_WINDOW)

    print("[ready] q/ESC 退出, +/- 调阈值, s 存图, h 切换被隐藏类的显示")

    try:
        while True:
            t0 = time.time()

            try:
                img = cap.capture_image(device)
            except Exception as exc:  # noqa: BLE001 - 抓帧失败打印原因，跳过这帧继续
                print(f"[frame {frame_idx}] capture failed: {exc}")
                key = cv2.waitKey(30) & 0xFF
                if key in (27, ord("q")):
                    break
                continue

            try:
                dets = det.detect(img, conf=conf_thresh)
            except Exception as exc:  # noqa: BLE001 - 检测失败不崩窗口
                print(f"[frame {frame_idx}] yolo detect failed: {exc}")
                dets = []

            if hidden and not show_hidden:
                dets = [d for d in dets if d.get("label") not in hidden]

            raw = np.ascontiguousarray(np.array(img)[:, :, ::-1])  # RGB -> BGR
            h, w = raw.shape[:2]

            # 关键：先把原图缩到显示尺寸，再在显示图上画框和标签 ——
            # 这样标签字号是真实显示像素、始终清晰，不随窗口缩放一起变糊。
            scale = min(screen_w * DISPLAY_MARGIN / w, screen_h * DISPLAY_MARGIN / h, 1.0)
            disp_w, disp_h = max(1, int(w * scale)), max(1, int(h * scale))
            display = cv2.resize(raw, (disp_w, disp_h), interpolation=cv2.INTER_AREA)

            counts: dict = {}
            for d in dets:
                label = d.get("label", "?")
                counts[label] = counts.get(label, 0) + 1
                x1, y1, x2, y2 = d["bbox"]
                dx1, dy1 = int(x1 * scale), int(y1 * scale)   # 原图坐标 -> 显示坐标
                dx2, dy2 = int(x2 * scale), int(y2 * scale)
                color = _color_for(label)
                cv2.rectangle(display, (dx1, dy1), (dx2, dy2), color, 2)
                _draw_label(display, f"{label} {d['score']:.2f}", (dx1, dy1), color)

            # 准星十字（显示坐标中心）
            cx, cy = disp_w // 2, disp_h // 2
            cv2.line(display, (cx - 18, cy), (cx + 18, cy), (0, 255, 0), 1)
            cv2.line(display, (cx, cy - 18), (cx, cy + 18), (0, 255, 0), 1)

            # fps
            dt = time.time() - t0
            fps_hist.append(1.0 / dt if dt > 0 else 0.0)
            fps = sum(fps_hist) / len(fps_hist)

            if frame_idx % OBJ_EVERY_N == 0:
                last_obj_text = _read_top_text(ocr, img)   # OCR 比检测慢，隔 N 帧读一次

            hidden_state = "-" if not hidden else ("shown" if show_hidden else "hidden")
            hud = [
                f"conf_thresh={conf_thresh:.2f}  fps={fps:.1f}  hidden={hidden_state}",
                "counts: " + (" ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "(none)"),
                f"top: {last_obj_text}" if last_obj_text else "top: (none)",
            ]
            for i, line in enumerate(hud):
                _put_text_outlined(display, line, (10, 24 + i * 24), scale=0.6, color=(255, 255, 255))

            if not win_sized:
                cv2.resizeWindow(win, disp_w, disp_h)
                win_sized = True
            cv2.imshow(win, display)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            elif key in (ord("+"), ord("=")):
                conf_thresh = round(min(CONF_MAX, conf_thresh + CONF_STEP), 2)
                print(f"[conf] -> {conf_thresh:.2f}")
            elif key in (ord("-"), ord("_")):
                conf_thresh = round(max(CONF_MIN, conf_thresh - CONF_STEP), 2)
                print(f"[conf] -> {conf_thresh:.2f}")
            elif key == ord("s"):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                path = os.path.join(SHOT_DIR, f"{ts}.jpg")
                try:
                    cv2.imwrite(path, display)
                    print(f"[save] {path}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[save] failed: {exc}")
            elif key == ord("h"):
                show_hidden = not show_hidden
                print(f"[hide] show_hidden -> {show_hidden} (类: {', '.join(sorted(hidden)) or '无'})")

            frame_idx += 1
    finally:
        cv2.destroyAllWindows()
        print("[exit] window closed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
