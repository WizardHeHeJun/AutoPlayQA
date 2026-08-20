"""入口：python backend/main.py [--port 8930] [--reload]

自己调 uvicorn.run 而不是 `-m uvicorn`：backend.autoplayqa 在 import 期
chdir 到 AutoPlayQA 根，入口脚本先把 PipelineEditor 根钉进 sys.path，
避开 uvicorn 模块解析受 chdir 影响的问题。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8930)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    if args.reload:
        # reload 子进程重新 import，需要 PYTHONPATH 带上 PipelineEditor 根；
        # 只 watch backend/，否则 AutoPlayQA outputs/ 的写入会触发重载风暴
        os.environ["PYTHONPATH"] = (
            str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
        uvicorn.run("backend.app:app", host="127.0.0.1", port=args.port,
                    reload=True, reload_dirs=[str(ROOT / "backend")])
    else:
        from backend.app import app
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
