"""AutoPlayQA 桥接层：路径注入 + 共享 config/runtime 单例。

必须最先被 import（backend 内其他模块都从这里拿 AutoPlayQA 符号）：
AutoPlayQA 全部资源路径（config.yaml / task/task_definitions / task/templates /
outputs）都是相对 cwd 解析的，所以这里 sys.path 注入 + os.chdir 一步到位。
之后本项目自身的文件访问一律用绝对路径（PIPELINE_ROOT）。

根目录解析：编辑器随框架仓库分发在 `pipeline_editor/` 下，默认按本文件位置
上推两级拿到仓库根（`<repo>/pipeline_editor/backend/autoplayqa.py`）；
环境变量 `AUTOPLAYQA_ROOT` 仍可覆盖（例如把编辑器放到别处时）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUTOPLAYQA_ROOT = PIPELINE_ROOT.parent

AUTOPLAYQA_ROOT = Path(
    os.environ.get("AUTOPLAYQA_ROOT") or DEFAULT_AUTOPLAYQA_ROOT
).resolve()
if not (AUTOPLAYQA_ROOT / "bootstrap.py").is_file():
    raise RuntimeError(
        f"AutoPlayQA not found at {AUTOPLAYQA_ROOT} (set AUTOPLAYQA_ROOT env var)"
    )

if str(AUTOPLAYQA_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOPLAYQA_ROOT))
os.chdir(AUTOPLAYQA_ROOT)

from bootstrap import Runtime, build_runtime, load_app  # noqa: E402

config, logger = load_app("config.yaml")

TASK_DIR = (AUTOPLAYQA_ROOT / "task" / "task_definitions").resolve()
SUITE_DIR = TASK_DIR / "suites"
LAYOUT_DIR = TASK_DIR / ".layout"
TEMPLATE_DIR = (
    AUTOPLAYQA_ROOT / config.get("templates", {}).get("dir", "task/templates")
).resolve()
FINDINGS_DIR = (
    AUTOPLAYQA_ROOT
    / config.get("findings", {}).get("output_dir", "outputs/findings")
).resolve()

_runtime: Runtime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> Runtime:
    """懒构建 AutoPlayQA 的对象图（感知/设备/引擎）。

    纯编辑端点（CRUD/validate/lint/schema）绝不调用它，保证无设备也可编辑。
    绝不在别处自建 ScreenshotCapturer/OcrEngine——OCR warmup 与 scrcpy 的
    初始化顺序约束（bootstrap.py:91-94）只在这一份装配里成立。
    """
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = build_runtime(config, logger)
    return _runtime
