"""round-trip 回归：任务读写链路的强制门（REST 与内嵌 MCP 两条通道）。

断言的是项目硬红线 #2 / #3 在**服务端**成立：

- REST：`GET /api/tasks/{name}` 的展示态经前端 serializeForSave 回到磁盘形态，
  与 raw 深度相等（本机 `task/task_definitions/` 下的每个任务都过一遍）。
- MCP：`get_task` → 原样 `save_task`，落盘内容与原文件深度相等——AI 最自然的
  get→改→save 工作流不会固化 defaults、不会丢 `includes` 声明、不会把 include
  节点写进主文件。
- 名字白名单：所有接受 name 的 MCP 工具拒绝 `../` 之类路径穿越，且不产生越界文件。

任务目录是本机资产（仓库只随附一个示例任务），所以 `defaults` / 节点级 `null`
opt-out / `includes` 这三种形态一律由临时合成任务 + 临时 include 片段覆盖
（写进 TASK_DIR 的探针文件，finally 清理），不依赖本机存在哪些任务。

跑法（用项目的 conda 环境解释器）：

    python -m pytest pipeline_editor/tests/test_task_roundtrip.py -q
    python pipeline_editor/tests/test_task_roundtrip.py   # 免 pytest
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable, Dict, Iterator, List, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import mcp_embed  # noqa: E402
from backend.autoplayqa import AUTOPLAYQA_ROOT, SUITE_DIR, TASK_DIR  # noqa: E402
from backend.routers.tasks import api_get_task, api_save_task  # noqa: E402

from task.task_loader import list_tasks  # noqa: E402

MAIN_FILE_LABEL = "<task>"
GENERATED_KEYS = ("_merge", "_steps", "_step_outline")
PROBE_NAME = "_roundtrip_probe"
PROBE_PATH = TASK_DIR / f"{PROBE_NAME}.json"

# include 片段放进子目录：list_tasks 只 glob 顶层 *.json，不会把它当成一个任务。
INCLUDE_DIR = TASK_DIR / "_roundtrip_probe_includes"
INCLUDE_REF = "_roundtrip_probe_includes/shared.json"
INCLUDE_NODES: Dict[str, Any] = {
    "共享_确认弹窗": {
        "recognition": {"type": "ui_text", "expected": "确定"},
        "action": {"type": "click", "target": "recognized"},
        "next": ["共享_回到首页"],
    },
    "共享_回到首页": {
        "recognition": {"type": "always"},
        "action": {"type": "key", "params": {"keycode": 3}},
        "next": [],
    },
}

T = TypeVar("T")


@contextmanager
def _include_fixture() -> Iterator[str]:
    """临时写一个 include 片段，返回它的相对引用路径；退出时连目录一起清掉。"""
    INCLUDE_DIR.mkdir(parents=True, exist_ok=True)
    path = INCLUDE_DIR / "shared.json"
    path.write_text(
        json.dumps({"nodes": INCLUDE_NODES}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        yield INCLUDE_REF
    finally:
        if path.exists():
            path.unlink()
        if INCLUDE_DIR.is_dir() and not any(INCLUDE_DIR.iterdir()):
            INCLUDE_DIR.rmdir()


def _task_with_includes(include_ref: str) -> Dict[str, Any]:
    return {
        "_comment": "round-trip 探针，测完即删",
        "entry": "用例开始",
        "includes": [include_ref],
        "nodes": {
            "用例开始": {
                "recognition": {"type": "always"},
                "action": {"type": "key", "params": {"keycode": 4}},
                "next": ["共享_确认弹窗"],
            },
        },
    }


def _run(coro: Awaitable[T]) -> T:
    """MCP 工具现在是 async（阻塞部分 to_thread），测试里同步跑一遍。"""
    return asyncio.run(coro)  # type: ignore[arg-type]


def serialize_for_save(doc: Dict[str, Any]) -> Dict[str, Any]:
    """前端 `frontend/src/graph/serialize.ts` 的独立复刻。

    **故意不复用 `backend.taskio.strip_for_save`**——回归要证明「后端展示态能被
    前端那套剔除规则还原成磁盘形态」，用被测对象自己去还原就成了自证。
    """
    out: Dict[str, Any] = {}
    for key, value in doc.items():
        if key in GENERATED_KEYS or key == "nodes":
            continue
        out[key] = value
    include_map = (doc.get("_merge") or {}).get("include_map") or {}
    nodes: Dict[str, Any] = {}
    for name, node in (doc.get("nodes") or {}).items():
        src = include_map.get(name)
        if src is not None and src != MAIN_FILE_LABEL:
            continue
        nodes[name] = node
    out["nodes"] = nodes
    return out


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_probe(task: Dict[str, Any]) -> None:
    PROBE_PATH.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_probe() -> None:
    if PROBE_PATH.exists():
        PROBE_PATH.unlink()


def _mcp_roundtrip(task: Dict[str, Any]) -> Dict[str, Any]:
    """把 task 放进探针文件 → MCP get_task → 原样 save_task → 返回落盘内容。

    永远只写探针文件，本机既有任务一个字节都不动。
    """
    try:
        _write_probe(task)
        display = _run(mcp_embed.get_task(PROBE_NAME))
        assert "error" not in display or display.get("ok") is not False, display
        saved = _run(mcp_embed.save_task(PROBE_NAME, json.dumps(display, ensure_ascii=False)))
        assert saved.get("ok"), saved
        return _read(PROBE_PATH)
    finally:
        _cleanup_probe()


# ---------- 1. REST 展示态 round-trip（本机全部任务） ----------

def test_rest_display_roundtrip_all_tasks() -> None:
    names = list_tasks()
    assert names, "任务目录是空的，至少应有随仓库分发的示例任务"
    for name in names:
        payload = api_get_task(name)
        assert payload["error"] is None, (name, payload["error"])
        back = serialize_for_save(payload["resolved"])
        assert back == payload["raw"], f"{name}: 展示态还原后与 raw 不等"


# ---------- 2. MCP get_task → save_task round-trip ----------

def test_mcp_roundtrip_all_tasks() -> None:
    for name in list_tasks():
        raw = _read(TASK_DIR / f"{name}.json")
        saved = _mcp_roundtrip(copy.deepcopy(raw))
        assert saved == raw, f"{name}: MCP get→save 后内容变了"


def test_mcp_roundtrip_keeps_includes_declaration() -> None:
    """带 includes 的任务：include 节点不入主文件，includes 声明留住。"""
    with _include_fixture() as include_ref:
        raw = _task_with_includes(include_ref)
        saved = _mcp_roundtrip(copy.deepcopy(raw))
    assert saved.get("includes") == raw["includes"]
    assert set(saved.get("nodes") or {}) == set(raw.get("nodes") or {})


def test_mcp_roundtrip_defaults_and_null_optout() -> None:
    """合成任务：任务级 defaults + 节点级显式 null（拒绝该 default）。

    走 load_task/resolve_task 的老路会把 defaults 填进每个节点、并删掉 null，
    这条断言就是那条红线的守门人。
    """
    task = {
        "_comment": "round-trip 探针，测完即删",
        "entry": "A",
        "defaults": {"timeout_ms": 12345, "post_delay_ms": 700},
        "nodes": {
            "A": {
                "recognition": {"type": "ui_text", "expected": "设置"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["B"],
                "timeout_ms": None,
            },
            "B": {
                "recognition": {"type": "always"},
                "action": {"type": "key", "params": {"keycode": 4}},
                "next": [],
                "post_delay_ms": 100,
            },
        },
    }
    saved = _mcp_roundtrip(copy.deepcopy(task))
    assert saved == task
    assert saved["nodes"]["A"]["timeout_ms"] is None, "显式 null 的 opt-out 被吃掉了"
    assert "timeout_ms" not in saved["nodes"]["B"], "defaults 被固化进节点"


def test_mcp_roundtrip_include_nodes_never_inlined() -> None:
    """合成任务 + include 片段：AI 把展示态原样回传，include 节点必须被剔掉。"""
    with _include_fixture() as include_ref:
        task = _task_with_includes(include_ref)
        try:
            _write_probe(task)
            display = _run(mcp_embed.get_task(PROBE_NAME))
            assert len(display["nodes"]) > len(task["nodes"]), "展示态没有合并 include 节点"
            assert display["_merge"]["include_map"]["用例开始"] == MAIN_FILE_LABEL
            result = _run(
                mcp_embed.save_task(PROBE_NAME, json.dumps(display, ensure_ascii=False))
            )
            assert result.get("ok"), result
            assert result["include_nodes_skipped"], "没有报告被剔除的 include 节点"
            saved = _read(PROBE_PATH)
        finally:
            _cleanup_probe()
    assert saved == task
    assert set(saved["nodes"]) == {"用例开始"}


# ---------- 3. REST 保存的服务端兜底 ----------

def test_rest_save_strips_display_form() -> None:
    """PUT 收到展示态（前端没剔干净的极端情况）也不许把生成键/include 节点落盘。"""
    with _include_fixture() as include_ref:
        raw = _task_with_includes(include_ref)
        try:
            _write_probe(copy.deepcopy(raw))
            display = api_get_task(PROBE_NAME)["resolved"]
            result = api_save_task(PROBE_NAME, {"task": copy.deepcopy(display)})
            assert result.get("ok"), result
            saved = _read(PROBE_PATH)
        finally:
            _cleanup_probe()
    assert saved == raw
    for key in GENERATED_KEYS:
        assert key not in saved


# ---------- 4. 路径穿越 ----------

EVIL_NAMES = ("../../evil", r"..\..\evil", "sub/evil", ".hidden", "C:evil", "")


def test_mcp_tools_reject_path_traversal() -> None:
    payload = json.dumps({"entry": "A", "nodes": {}})
    suite_payload = json.dumps({"cases": []})
    for evil in EVIL_NAMES:
        for label, result in (
            ("get_task", _run(mcp_embed.get_task(evil))),
            ("save_task", _run(mcp_embed.save_task(evil, payload))),
            ("renumber_task", _run(mcp_embed.renumber_task(evil))),
            ("lint_saved_task", _run(mcp_embed.lint_saved_task(evil))),
            ("save_suite", _run(mcp_embed.save_suite(evil, suite_payload))),
        ):
            assert result.get("ok") is False, f"{label}({evil!r}) 没被拦住: {result}"
            assert "Invalid" in str(result.get("error")), (label, evil, result)


def test_no_file_escaped_the_task_dir() -> None:
    """穿越尝试不得在任务目录之外留下文件。"""
    for stray in (
        AUTOPLAYQA_ROOT / "evil.json",
        AUTOPLAYQA_ROOT.parent / "evil.json",
        TASK_DIR.parent / "evil.json",
        SUITE_DIR.parent.parent / "evil.json",
        PROBE_PATH,
    ):
        assert not stray.exists(), f"越界文件被创建: {stray}"


def main() -> int:
    tests: List[Any] = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
