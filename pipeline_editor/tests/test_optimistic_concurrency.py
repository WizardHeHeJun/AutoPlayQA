"""保存的乐观并发控制回归（REST 与内嵌 MCP 两条写通道）。

守的是「人 Ctrl+S 与 AI 经 MCP 保存交叉时不再静默 last-write-wins」：

- 读通道给出文件版本令牌：REST GET 的 `mtime_ns`、MCP get_task 的 `_file_mtime_ns`。
- 写通道带上过期的期望版本 → 拒绝写盘（REST 409 / MCP `conflict: True`），文件不动。
- 带上当前版本 → 正常写入并返回新版本。
- 不带 → 保持老行为直接写（向后兼容）。
- `_file_mtime_ns` 是生成键，**绝不落盘**：get→save round-trip 仍与原文件深度相等。

外部写入用 `os.utime` 精确制造，不依赖 sleep / 文件系统时间精度。

跑法（用项目的 conda 环境解释器）：

    python -m pytest pipeline_editor/tests/test_optimistic_concurrency.py -q
    python pipeline_editor/tests/test_optimistic_concurrency.py  # 免 pytest
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Dict, List, TypeVar

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import mcp_embed, taskio  # noqa: E402
from backend.autoplayqa import SUITE_DIR, TASK_DIR  # noqa: E402
from backend.routers.suites import api_get_suite, api_save_suite  # noqa: E402
from backend.routers.tasks import api_get_task, api_save_task  # noqa: E402

from task.task_loader import list_tasks  # noqa: E402

PROBE = "_concurrency_probe"
TASK_PATH = TASK_DIR / f"{PROBE}.json"
SUITE_PATH = SUITE_DIR / f"{PROBE}.json"

PROBE_TASK: Dict[str, Any] = {
    "_comment": "乐观并发探针，测完即删",
    "entry": "A",
    "nodes": {
        "A": {
            "recognition": {"type": "always"},
            "action": {"type": "key", "params": {"keycode": 4}},
            "next": [],
        },
    },
}

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _read(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup() -> None:
    for path in (TASK_PATH, SUITE_PATH):
        if path.exists():
            path.unlink()


def _external_write(path: Path) -> str:
    """模拟「别人刚刚改了这个文件」：把 mtime 往前推 1s，返回新的版本令牌。

    用 utime 而不是 sleep+rewrite：文件系统时间精度不参与，断言恒定。
    """
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    version = taskio.file_version(path)
    assert version is not None
    return version


def _probe_suite() -> Dict[str, Any]:
    case = list_tasks()[0]
    return {"name": PROBE, "cases": [case], "resume_after": "x",
            "case_entry": "y", "landing": None}


# ---------- 1. taskio 的判定层 ----------

def test_file_version_key_is_registered_as_generated() -> None:
    """漏注册 = `_file_mtime_ns` 会被 AI 的 get→save 写进任务文件（红线 #2）。"""
    assert taskio.FILE_VERSION_KEY in taskio.GENERATED_KEYS
    stripped = taskio.strip_for_save({**copy.deepcopy(PROBE_TASK),
                                      taskio.FILE_VERSION_KEY: "123"})
    assert taskio.FILE_VERSION_KEY not in stripped


def test_version_conflict_semantics() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        current = taskio.file_version(TASK_PATH)
        assert current is not None and current.isdigit()
        assert taskio.version_conflict(TASK_PATH, None) is None      # 不给 = 不检测
        assert taskio.version_conflict(TASK_PATH, current) is None   # 一致 = 放行
        assert taskio.version_conflict(TASK_PATH, "1") == current    # 过期 = 冲突
    finally:
        _cleanup()
    # 文件不存在 = 新建，永不冲突
    assert taskio.file_version(TASK_PATH) is None
    assert taskio.version_conflict(TASK_PATH, "1") is None


def test_normalize_version_accepts_str_and_int_rejects_garbage() -> None:
    assert taskio.normalize_version(None) is None
    assert taskio.normalize_version("") is None
    assert taskio.normalize_version(1755498123456789100) == "1755498123456789100"
    assert taskio.normalize_version(" 1755498123456789100 ") == "1755498123456789100"
    for bad in ("abc", "1.5", True, [], {}):
        try:
            taskio.normalize_version(bad)
        except ValueError:
            continue
        raise AssertionError(f"非法版本没被拒绝: {bad!r}")


# ---------- 2. REST 任务 ----------

def test_rest_get_task_returns_mtime_ns() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        payload = api_get_task(PROBE)
        assert payload["error"] is None, payload["error"]
        assert payload["mtime_ns"] == str(TASK_PATH.stat().st_mtime_ns)
        # 版本令牌不进任务体：resolved 仍是纯展示态（round-trip 回归不受影响）
        assert taskio.FILE_VERSION_KEY not in payload["resolved"]
    finally:
        _cleanup()


def test_rest_save_task_rejects_stale_base() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        stale = api_get_task(PROBE)["mtime_ns"]
        current = _external_write(TASK_PATH)
        edited = copy.deepcopy(PROBE_TASK)
        edited["entry"] = "B"
        with pytest.raises(HTTPException) as excinfo:
            api_save_task(PROBE, {"task": edited, "base_mtime_ns": stale})
        exc = excinfo.value
        assert exc.status_code == 409
        assert exc.detail["conflict"] is True
        assert exc.detail["current_mtime_ns"] == current
        assert _read(TASK_PATH) == PROBE_TASK, "409 之后文件被写了"
    finally:
        _cleanup()


def test_rest_save_task_accepts_current_base_and_returns_new_version() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        base = api_get_task(PROBE)["mtime_ns"]
        edited = copy.deepcopy(PROBE_TASK)
        edited["max_steps"] = 42
        result = api_save_task(PROBE, {"task": edited, "base_mtime_ns": base})
        assert result["ok"], result
        assert result["mtime_ns"] == str(TASK_PATH.stat().st_mtime_ns)
        assert _read(TASK_PATH)["max_steps"] == 42
        # 返回的新版本可以直接当下一次保存的基线（连续编辑不必重读）
        again = api_save_task(PROBE, {"task": edited, "base_mtime_ns": result["mtime_ns"]})
        assert again["ok"], again
    finally:
        _cleanup()


def test_rest_save_task_without_base_is_backward_compatible() -> None:
    """老调用方（不带 base）行为完全不变：照旧覆盖。"""
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        _external_write(TASK_PATH)
        edited = copy.deepcopy(PROBE_TASK)
        edited["max_steps"] = 7
        result = api_save_task(PROBE, {"task": edited})
        assert result["ok"], result
        assert _read(TASK_PATH)["max_steps"] == 7
    finally:
        _cleanup()


def test_rest_save_task_rejects_garbage_base() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        with pytest.raises(HTTPException) as excinfo:
            api_save_task(PROBE, {"task": copy.deepcopy(PROBE_TASK),
                                  "base_mtime_ns": "not-a-version"})
        assert excinfo.value.status_code == 400
        assert _read(TASK_PATH) == PROBE_TASK
    finally:
        _cleanup()


def test_rest_save_new_task_with_base_is_not_a_conflict() -> None:
    """文件不存在 = 新建：给了任何基线都不该冲突。"""
    _cleanup()
    try:
        result = api_save_task(PROBE, {"task": copy.deepcopy(PROBE_TASK),
                                       "base_mtime_ns": "1"})
        assert result["ok"], result
        assert TASK_PATH.is_file()
    finally:
        _cleanup()


# ---------- 3. MCP 任务 ----------

def test_mcp_get_task_carries_file_version() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        display = _run(mcp_embed.get_task(PROBE))
        assert display.get("ok") is not False, display
        token = display[taskio.FILE_VERSION_KEY]
        assert token == str(TASK_PATH.stat().st_mtime_ns)
        assert isinstance(token, str) and token.isdigit(), "版本令牌必须是字符串（JS 精度）"
    finally:
        _cleanup()


def test_mcp_roundtrip_never_writes_file_version_key() -> None:
    """AI 最自然的 get→原样 save：`_file_mtime_ns` 必须被剔掉，文件逐字不变。"""
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        display = _run(mcp_embed.get_task(PROBE))
        saved = _run(mcp_embed.save_task(
            PROBE, json.dumps(display, ensure_ascii=False),
            display[taskio.FILE_VERSION_KEY],
        ))
        assert saved.get("ok"), saved
        on_disk = _read(TASK_PATH)
        assert taskio.FILE_VERSION_KEY not in on_disk, "版本令牌落盘了"
        assert on_disk == PROBE_TASK, "get→save 后内容变了"
    finally:
        _cleanup()


def test_mcp_save_task_reports_conflict() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        display = _run(mcp_embed.get_task(PROBE))
        stale = display[taskio.FILE_VERSION_KEY]
        current = _external_write(TASK_PATH)
        display["entry"] = "B"
        result = _run(mcp_embed.save_task(
            PROBE, json.dumps(display, ensure_ascii=False), stale))
        assert result["ok"] is False
        assert result["conflict"] is True
        assert result["current_mtime_ns"] == current
        assert _read(TASK_PATH) == PROBE_TASK, "冲突之后文件被写了"
    finally:
        _cleanup()


def test_mcp_save_task_without_base_is_backward_compatible() -> None:
    try:
        _write(TASK_PATH, copy.deepcopy(PROBE_TASK))
        display = _run(mcp_embed.get_task(PROBE))
        _external_write(TASK_PATH)
        display["max_steps"] = 5
        result = _run(mcp_embed.save_task(PROBE, json.dumps(display, ensure_ascii=False)))
        assert result.get("ok"), result
        assert result["mtime_ns"] == str(TASK_PATH.stat().st_mtime_ns)
        assert _read(TASK_PATH)["max_steps"] == 5
    finally:
        _cleanup()


# ---------- 4. 套件（REST + MCP） ----------

def test_rest_suite_version_and_conflict() -> None:
    suite = _probe_suite()
    try:
        _write(SUITE_PATH, copy.deepcopy(suite))
        payload = api_get_suite(PROBE)
        assert payload["mtime_ns"] == str(SUITE_PATH.stat().st_mtime_ns)

        stale = payload["mtime_ns"]
        current = _external_write(SUITE_PATH)
        edited = copy.deepcopy(suite)
        edited["max_retries"] = 3
        with pytest.raises(HTTPException) as excinfo:
            api_save_suite(PROBE, {"suite": edited, "base_mtime_ns": stale})
        assert excinfo.value.status_code == 409
        assert excinfo.value.detail["current_mtime_ns"] == current
        assert _read(SUITE_PATH) == suite, "409 之后套件文件被写了"

        ok = api_save_suite(PROBE, {"suite": edited, "base_mtime_ns": current})
        assert ok["ok"], ok
        assert ok["mtime_ns"] == str(SUITE_PATH.stat().st_mtime_ns)
        assert _read(SUITE_PATH)["max_retries"] == 3

        # 不带 base：老行为，直接覆盖
        _external_write(SUITE_PATH)
        edited["max_retries"] = 4
        assert api_save_suite(PROBE, {"suite": edited})["ok"]
        assert _read(SUITE_PATH)["max_retries"] == 4
    finally:
        _cleanup()


def test_mcp_save_suite_reports_conflict() -> None:
    suite = _probe_suite()
    try:
        _write(SUITE_PATH, copy.deepcopy(suite))
        stale = taskio.file_version(SUITE_PATH)
        current = _external_write(SUITE_PATH)
        edited = copy.deepcopy(suite)
        edited["max_retries"] = 9
        result = _run(mcp_embed.save_suite(
            PROBE, json.dumps(edited, ensure_ascii=False), stale))
        assert result["ok"] is False
        assert result["conflict"] is True
        assert result["current_mtime_ns"] == current
        assert _read(SUITE_PATH) == suite

        ok = _run(mcp_embed.save_suite(
            PROBE, json.dumps(edited, ensure_ascii=False), current))
        assert ok.get("ok"), ok
        assert ok["mtime_ns"] == str(SUITE_PATH.stat().st_mtime_ns)
    finally:
        _cleanup()


def test_probe_files_are_cleaned_up() -> None:
    assert not TASK_PATH.exists(), f"探针任务文件残留: {TASK_PATH}"
    assert not SUITE_PATH.exists(), f"探针套件文件残留: {SUITE_PATH}"


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
