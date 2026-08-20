"""任务读写共享层：名字校验 + 展示态构造 + 保存前剥离。

REST（`routers/tasks.py`）与内嵌 MCP（`mcp_embed.py`）共用这一份，**不允许分叉**：
两条通道对同一个任务文件的 get / save 语义必须逐字一致，否则 AI 经 MCP 的
get→改→save 会把 defaults 固化进节点、把 include 节点写进主文件（项目硬红线
#2 / #3），而人经 UI 保存不会——同一个文件被两条路写成两种形态。

四个能力：

- `is_valid_name` / `NAME_RE`：任务名 / 套件名的路径穿越防线（`../x` 直接拒绝）。
- `build_display_task`：合并 includes 但**不展开 defaults** 的展示态。
- `strip_for_save`：服务端版 `serializeForSave`——剔生成键与 include 来源节点。
- `file_version` / `version_conflict`：保存的乐观并发控制（见下方大段说明）。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, Optional

import backend.autoplayqa  # noqa: F401  (path setup must run first)
from backend.autoplayqa import TASK_DIR

from task.step_numbering import compute_step_labels, format_task_outline
from task.task_loader import MAIN_FILE_LABEL, TaskValidationError, resolve_task

#: MCP `get_task` 塞进任务 dict 的文件版本键（REST 走响应外层的 `mtime_ns` 字段，
#: 不进任务体）。它**必须**在 `GENERATED_KEYS` 里 —— 否则 AI 的 get→save 会把它
#: 写进任务文件，破坏 round-trip 无损（项目硬红线 #2）。
FILE_VERSION_KEY = "_file_mtime_ns"

#: 生成键：后端加给编辑器/AI 看的派生信息，绝不落盘。
GENERATED_KEYS = frozenset(("_merge", "_steps", "_step_outline", FILE_VERSION_KEY))

#: 展示态里从 raw 原样补回的顶层声明（resolve_task 会把它们剥掉 / 展开掉）。
RAW_ONLY_KEYS = ("defaults", "includes", "on_conflict")

#: 任务名 / 套件名白名单：无路径分隔符、无盘符冒号、无通配符，且不以 `.` 开头
#: （挡掉 `..`、`.layout` 这类越界与隐藏目录）。
NAME_RE = re.compile(r"^[^\\/:*?\"<>|.][^\\/:*?\"<>|]*$")


def is_valid_name(name: Any) -> bool:
    """名字是否可安全拼进任务/套件目录（唯一的路径穿越防线）。"""
    return isinstance(name, str) and bool(NAME_RE.match(name))


def build_display_task(raw: Dict) -> Dict:
    """raw 任务 → 编辑器/AI 用的展示态（includes 已合并，defaults **不展开**）。

    引擎的 `resolve_task` 顺手展开 defaults：把任务级 defaults 填进每个
    节点，并删掉节点里显式写的 `null`（`null` = 该节点拒绝这条 default）。基于
    那个形态保存会同时固化 defaults 和丢失 null 退出语义，所以这里对**去掉
    defaults 的副本**做合并，再把 raw 的 defaults/includes/on_conflict 原样补回。

    完整校验（含 defaults 展开）仍单独跑一遍，两次调用各自 deepcopy——resolve_task
    不 mutate 入参，但共享对象会让 `_merge` 互相污染。

    校验失败抛 `TaskValidationError`，由调用方决定怎么呈现。
    """
    resolve_task(copy.deepcopy(raw), TASK_DIR)  # 完整校验（含 defaults 展开）
    stripped = {k: v for k, v in raw.items() if k != "defaults"}
    display = resolve_task(copy.deepcopy(stripped), TASK_DIR)
    for key in RAW_ONLY_KEYS:
        if key in raw:
            display[key] = copy.deepcopy(raw[key])
    labels = compute_step_labels(display)
    display["_steps"] = labels
    display["_step_outline"] = format_task_outline(display, labels)
    return display


def include_map_of(task: Dict) -> Dict[str, str]:
    """节点 → 来源文件（`"<task>"` = 任务自己的文件）。

    优先用展示态里现成的 `_merge.include_map`；调用方给的是原始形态（有
    `includes` 但没 `_merge`）时重新 resolve 一次求。求不出来（文件不合法等）
    就返回空表 —— 空表意味着 `strip_for_save` 一个节点都不剔，宁可多留也不误删。
    """
    merge = task.get("_merge")
    if isinstance(merge, dict) and isinstance(merge.get("include_map"), dict):
        return merge["include_map"]
    if not task.get("includes"):
        return {}
    probe = {k: v for k, v in task.items() if k not in GENERATED_KEYS and k != "defaults"}
    try:
        resolved = resolve_task(copy.deepcopy(probe), TASK_DIR)
    except TaskValidationError:
        return {}
    merge = resolved.get("_merge")
    if isinstance(merge, dict) and isinstance(merge.get("include_map"), dict):
        return merge["include_map"]
    return {}


def strip_for_save(task: Dict) -> Dict:
    """展示态/编辑态 → 可写盘的原始形态（服务端版 serializeForSave）。

    剔两类东西：生成键 `_merge`/`_steps`/`_step_outline`，以及 include 来源的
    节点（把它们固化进主文件会破坏共享语义，且下次 resolve 会与 include 撞名）。
    用户自己写的 `_comment` 等下划线键、以及 `includes`/`on_conflict`/`defaults`
    顶层声明**原样保留**。

    顶层键顺序按入参保持（`nodes` 留在原位而不是被挪到末尾），让「不做任何修改
    的 get→save」尽可能逐字节回到原文件。
    """
    include_map = include_map_of(task)
    out: Dict[str, Any] = {}
    for key, value in task.items():
        if key in GENERATED_KEYS:
            continue
        if key == "nodes" and isinstance(value, dict):
            out[key] = {
                name: node for name, node in value.items()
                if include_map.get(name, MAIN_FILE_LABEL) == MAIN_FILE_LABEL
            }
        else:
            out[key] = value
    return out


# ---------- 保存的乐观并发控制 ----------
#
# 问题：REST `PUT /api/tasks/{name}` 与 MCP `save_task` 都是「校验 → 整文件覆盖」。
# 人在画布上 Ctrl+S、AI 经 MCP 保存，两边都不知道对方写过 —— 后写的静默赢，
# 先写的改动无声消失。
#
# 方案：读通道给出文件版本（mtime_ns），写通道接受可选的期望版本；期望版本与磁盘
# 当前版本不符就拒绝写盘，让调用方自己决定重载还是有意覆盖。
#
# 两个刻意的取舍：
#
# 1. **尽力而为，不加文件锁**：`version_conflict` 与随后的 `write_text` 之间存在
#    check-then-write 窗口。单进程后端 + 同步路由（写盘路径全在一个线程里跑完）
#    下这个窗口是微秒级，且真正要防的是「人和 AI 相隔几秒的交叉编辑」，不是
#    并发压测。加锁要跨 REST/MCP/外部编辑器三方，成本远大于收益。
# 2. **不给版本 = 不检测**：老调用方（脚本、旧前端）行为完全不变，仍是
#    last-write-wins。乐观锁是 opt-in 的。

def file_version(path: Path) -> Optional[str]:
    """文件当前版本令牌（`st_mtime_ns` 的十进制字符串）；文件不存在 → None。

    为什么是**字符串**而不是 JSON 数字：`mtime_ns` 量级约 1.7e18，远超 JS 的
    `Number.MAX_SAFE_INTEGER`（9.0e15）。前端和 MCP 客户端都跑在 JS 上，用 JSON
    数字传过去会被就近舍入，回传的期望版本永远对不上磁盘 —— 保存会永久 409。
    字符串是唯一无损的传输形态；服务端只做等值比较，不需要还原成整数。
    """
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return None


def normalize_version(value: Any) -> Optional[str]:
    """入参里的期望版本 → 规范令牌字符串；None / 空 = 不做并发检测。

    容忍整数（Python 侧调用方 / 测试直接传 `st_mtime_ns`），非法值抛 `ValueError`
    由调用方转成 400 —— 静默忽略会让调用方以为自己开了乐观锁其实没开。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"base_mtime_ns 必须是 mtime_ns 的十进制字符串或整数: {value!r}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    raise ValueError(f"base_mtime_ns 必须是 mtime_ns 的十进制字符串或整数: {value!r}")


def version_conflict(path: Path, base_version: Optional[str]) -> Optional[str]:
    """冲突检测：返回磁盘当前版本表示「有人插队了，别写」，返回 None 表示可以写。

    - `base_version is None`（调用方没给期望版本）→ 不检测，保持向后兼容。
    - 文件不存在 → 新建，永不冲突。
    - 版本一致 → 从调用方读到现在没人动过，放行。
    """
    if base_version is None:
        return None
    current = file_version(path)
    if current is None or current == base_version:
        return None
    return current


def structured_error(message: str) -> Dict[str, Optional[str]]:
    """从 loader 的错误文本里尽力提取出错节点（错误文本格式稳定）。"""
    node = None
    m = re.search(r"Node '(.+?)'", message)
    if m:
        node = m.group(1)
    scope = "node" if node else "task"
    for kw, s in (("Watchdog #", "watchdog"), ("Popup #", "popup"), ("Suite ", "suite")):
        if kw in message:
            scope = s
            break
    return {"scope": scope, "node": node, "message": message}
