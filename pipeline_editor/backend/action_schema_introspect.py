"""从 custom action handler 的**源码**静态提取参数 schema（供前端渲染表单）。

AutoPlayQA 的 custom action 没有声明式参数表——handler 直接从 `params` dict
里取值。本模块用 `inspect.getsource` + `ast` 把那套高度规整的取值写法反解成
参数描述，**不改上游仓库、不执行 handler**。

识别的写法（`task/custom_actions/*.py` 的既有约定）::

    params.get("key")                     # 可选，无默认
    params.get("key", DEFAULT_CONST)      # 可选，默认取模块顶层常量
    int(params.get("key", 3))             # 外包裹转换函数决定类型
    _choice(params, "key", d, ALLOWED)    # 枚举
    _point(params, "key", (x, y))         # 坐标点
    params["key"]                         # 必填
    if v not in ALLOWED: raise ...        # 枚举（手写成员校验，没走 _choice）

**只做尽力而为**：认不出的写法退化成 `type="json"`（前端给 JSON 输入框），
提取失败一律返回空列表——绝不让某个 handler 的怪写法把 `/api/meta` 打挂。
默认值优先从 `fn.__globals__` 取运行期真值（模块已 import，常量就在那儿），
再退回 `ast.literal_eval`。

参数说明取自 handler docstring（英文），再经同目录的 `custom_action_docs_zh.json`
对照表换成中文：对照表存了「英文原文 + 译文」，只有英文原文与当前 docstring **完全
一致**才用译文，上游改了文案就自动回退英文，不会留下一条过期的中文。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from pydantic import BaseModel, Field

# 静态求值兜底：AST 层面认得的字面量之外，只信模块顶层常量的运行期值。
_EXTRACT_ERRORS = (OSError, TypeError, ValueError, SyntaxError, IndentationError,
                   RecursionError, AttributeError, IndexError)

#: 外包裹的转换函数 → 参数类型（优先级最高的类型信号）。
_SCALAR_CASTS: Dict[str, str] = {"int": "int", "float": "float",
                                 "str": "str", "bool": "bool"}
#: 序列型转换：不直接定类型，交给 roi / 默认值启发式。
_SEQ_CASTS: FrozenSet[str] = frozenset({"tuple", "list", "dict"})

_CHOICE_HELPERS: FrozenSet[str] = frozenset({"_choice"})
_POINT_HELPERS: FrozenSet[str] = frozenset({"_point"})

PARAM_TYPES = ("int", "float", "str", "bool", "enum", "roi", "point", "json")


class CustomActionParam(BaseModel):
    """单个 handler 参数的描述。前端据此选控件。"""

    key: str
    #: PARAM_TYPES 之一。
    type: str = "json"
    #: 静态求得的默认值（已转成 JSON 可序列化形态）；求不出时为 None 并置
    #: `default_unresolved`。**只用于前端 placeholder，绝不回写进 params**。
    default: Any = None
    default_unresolved: bool = False
    choices: Optional[List[str]] = None
    required: bool = False
    #: handler docstring 的 `params` 块里这个 key 的说明；命中 `custom_action_docs_zh.json`
    #: 对照表时是中文译文，否则是英文原文。docstring 没写 / 格式不认识时为 None。
    #: 只作提示展示，不参与任何校验。
    description: Optional[str] = None


class CustomActionSchema(BaseModel):
    """`GET /api/custom-actions/{name}/schema` 的响应体。"""

    name: str
    params: List[CustomActionParam] = Field(default_factory=list)


# handler 源码不热更（改了要重启后端），进程级缓存即可。
_CACHE: Dict[Any, List[CustomActionParam]] = {}


def extract_params(handler: Callable[..., Any]) -> List[CustomActionParam]:
    """提取 handler 的参数描述；任何异常都降级成空列表。"""
    try:
        cached = _CACHE.get(handler)
    except TypeError:  # 不可 hash 的可调用对象（理论上不会出现）
        return _safe_extract(handler)
    if cached is None:
        cached = _safe_extract(handler)
        _CACHE[handler] = cached
    return cached


def _safe_extract(handler: Callable[..., Any]) -> List[CustomActionParam]:
    try:
        return _extract(handler)
    except _EXTRACT_ERRORS:
        return []


# ---------------------------------------------------------------------------
# docstring 参数说明
# ---------------------------------------------------------------------------

#: tooltip 里再长也没人读，超出就截断（含省略号后总长 <= 这个值）。
DESCRIPTION_MAX = 500

#: 说明块的起始标记：`params:` / `params (all optional; ...):`（大小写不敏感）。
#: 要求整行以冒号收尾，所以 `params: dict of ...` 这种描述形参的行不会被当成标记。
_PARAMS_MARKER_RE = re.compile(r"^params\b.*:$", re.IGNORECASE)

#: 条目行：`key:` 或 `key1 / key2:`（别名共享一条说明；反斜杠分隔也认）。
#: key 必须是合法标识符——`mode -> speed: ...` 这种续行文字因此不会被误认。
_ENTRY_RE = re.compile(r"^([A-Za-z_]\w*(?:\s*[/\\]\s*[A-Za-z_]\w*)*)\s*:\s*(.*)$")

_ALIAS_SPLIT_RE = re.compile(r"[/\\]")

#: docstring 解析的失败面：格式千奇百怪，一律降级成「没有说明」。
_DOC_ERRORS = (AttributeError, IndexError, RecursionError, TypeError, ValueError, re.error)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_param_docs(doc: Optional[str]) -> Dict[str, str]:
    """从 docstring 的 `params` 块里提取 ``key -> 说明``。

    块的形状（`task/custom_actions/*.py` 的既有写法）::

        params (all optional; ...):        # 标记行，缩进 M
          Geometry                         # 分类标题，缩进 M < i < E，跳过
            origin:  swipe start point     # 条目行，缩进 E
                     (default ...)         # 续行，缩进 > E
            gain_x / gain_y: ...           # 别名条目，两个 key 共享一条说明

    条目缩进 E 由块内**第一条**认得的条目行确定；此后只有缩进正好等于 E 且
    形如 `标识符:` 的行才算新条目，更深的行并进上一条，更浅的行（分类标题）
    直接跳过。缩进 <= M 的非空行 = 块结束。

    认不出格式一律返回空 dict——说明是锦上添花，绝不让它影响 schema 提取。
    """
    try:
        return _parse_param_docs(doc)
    except _DOC_ERRORS:
        return {}


def _parse_param_docs(doc: Optional[str]) -> Dict[str, str]:
    if not doc or not isinstance(doc, str):
        return {}
    lines = doc.splitlines()

    marker_indent: Optional[int] = None
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and _PARAMS_MARKER_RE.match(stripped):
            marker_indent = _indent_of(line)
            start = i + 1
            break
    if marker_indent is None:
        return {}

    out: Dict[str, str] = {}
    entry_indent: Optional[int] = None
    keys: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        nonlocal keys, buf
        text = _clean_description(" ".join(buf))
        if text:
            for key in keys:
                out.setdefault(key, text)
        keys, buf = [], []

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            flush()  # 空行断开续行，但不结束块
            continue
        indent = _indent_of(line)
        if indent <= marker_indent:
            break  # 回到标记行的层级 = 块结束

        match = _ENTRY_RE.match(stripped)
        if entry_indent is None:
            if match is None:
                continue  # 条目开始前的分类标题 / 说明文字
            entry_indent = indent

        if indent == entry_indent:
            flush()
            if match is not None:
                keys = [k.strip() for k in _ALIAS_SPLIT_RE.split(match.group(1))]
                buf = [match.group(2)]
            # 同层级但不像条目的行（分类标题）：丢弃，不并进任何条目
        elif indent > entry_indent and keys:
            buf.append(stripped)
        else:
            flush()  # 更浅的一层 = 分类标题，结束上一条
    flush()
    return out


def _clean_description(text: str) -> str:
    """续行合并后的清洗：压缩空白 + 截断。"""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) > DESCRIPTION_MAX:
        cleaned = cleaned[: DESCRIPTION_MAX - 1].rstrip() + "…"
    return cleaned


# ---------------------------------------------------------------------------
# 中文对照表
# ---------------------------------------------------------------------------

#: 对照表文件，形状 ``{"<handler名>": {"<param key>": {"en": ..., "zh": ...}}}``。
#: handler 名取 `fn.__name__`（`@register("x")` 的注册名与函数名一致）。
#: `en` 存的是**当时提取到的英文原文**：上游改了 docstring 就对不上，译文自动失效
#: 回退英文——宁可显示英文，也不显示一条已经过期的中文。
_ZH_DOCS_PATH = Path(__file__).with_name("custom_action_docs_zh.json")

#: 加载失败面：文件不存在 / 读不动（OSError）、JSON 语法坏或编码坏（ValueError 的
#: 子类 JSONDecodeError / UnicodeDecodeError）。一律静默降级成「不启用中文」。
_ZH_LOAD_ERRORS = (OSError, ValueError)

#: 进程级缓存；None = 尚未加载（对照表是随代码发布的静态资源，不热更）。
_ZH_DOCS: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None


def _load_zh_docs() -> Dict[str, Dict[str, Dict[str, str]]]:
    """读对照表并逐条校验形状；坏掉的条目丢弃，坏掉的文件退化成空表。"""
    try:
        raw = json.loads(_ZH_DOCS_PATH.read_text(encoding="utf-8"))
    except _ZH_LOAD_ERRORS:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for handler_name, entries in raw.items():
        if not isinstance(handler_name, str) or not isinstance(entries, dict):
            continue
        table: Dict[str, Dict[str, str]] = {}
        for key, entry in entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            english, chinese = entry.get("en"), entry.get("zh")
            if (isinstance(english, str) and english
                    and isinstance(chinese, str) and chinese.strip()):
                table[key] = {"en": english, "zh": chinese}
        if table:
            out[handler_name] = table
    return out


def _zh_docs() -> Dict[str, Dict[str, Dict[str, str]]]:
    global _ZH_DOCS
    if _ZH_DOCS is None:
        _ZH_DOCS = _load_zh_docs()
    return _ZH_DOCS


def _localize(entry: Optional[Dict[str, str]], english: str) -> str:
    """对照表命中（`en` 与 docstring 原文**完全一致**）→ 中文；否则英文原样。"""
    if entry is None or entry.get("en") != english:
        return english
    chinese = entry.get("zh")
    if not isinstance(chinese, str):  # 加载器已过滤，这里兜住手工构造的表
        return english
    return _clean_description(chinese) or english


def _apply_docs(handler: Callable[..., Any], params: Dict[str, CustomActionParam]) -> None:
    """把 docstring 说明贴到已提取的参数上（命中对照表则换成中文）。

    只认 AST 已经抓到的 key（docstring 里多写的条目直接忽略），这样解析器万一
    把某行普通叙述当成条目，也落不到任何字段上。
    """
    try:
        doc = inspect.getdoc(handler)
    except _DOC_ERRORS:
        return
    docs = parse_param_docs(doc)
    if not docs:
        return
    zh_table = _zh_docs().get(getattr(handler, "__name__", "") or "", {})
    for key, param in params.items():
        text = docs.get(key)
        if text:
            param.description = _localize(zh_table.get(key), text)


# ---------------------------------------------------------------------------
# 静态求值
# ---------------------------------------------------------------------------

def _jsonable(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (tuple, list)):
        return all(_jsonable(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _jsonable(v) for k, v in value.items())
    return False


def _to_json(value: Any) -> Any:
    """tuple → list，保证 pydantic 序列化后前端拿到的是数组。"""
    if isinstance(value, (tuple, list)):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    return value


def _const(node: Optional[ast.AST], globals_: Dict[str, Any]) -> Tuple[bool, Any]:
    """求 AST 节点的静态值 → (是否求得, 值)。"""
    if node is None:
        return False, None
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        pass
    else:
        return (True, value) if _jsonable(value) else (False, None)
    if isinstance(node, ast.Name) and node.id in globals_:
        value = globals_[node.id]
        if _jsonable(value):
            return True, value
    return False, None


def _str_seq(node: Optional[ast.AST], globals_: Dict[str, Any]) -> Optional[List[str]]:
    """求一个「全是字符串的序列」——枚举 choices 的来源。"""
    ok, value = _const(node, globals_)
    if not ok or not isinstance(value, (tuple, list)) or not value:
        return None
    if not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _is_roi(value: Any) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))


# ---------------------------------------------------------------------------
# AST 提取
# ---------------------------------------------------------------------------

def _params_arg_name(handler: Callable[..., Any]) -> str:
    """handler 签名是 (ctx, params)，取第二个形参名；取不到退回 "params"。"""
    try:
        names = list(inspect.signature(handler).parameters)
    except (TypeError, ValueError):
        return "params"
    return names[1] if len(names) > 1 else "params"


def _is_params(node: ast.AST, params_name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == params_name


def _const_key(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


def _params_get_call(node: ast.AST, params_name: str) -> Optional[Tuple[str, Optional[ast.AST]]]:
    """`params.get("key"[, default])` → (key, default 节点)。"""
    if not isinstance(node, ast.Call) or node.keywords:
        return None
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "get"
            and _is_params(func.value, params_name)):
        return None
    if not node.args:
        return None
    key = _const_key(node.args[0])
    if key is None:
        return None
    return key, (node.args[1] if len(node.args) > 1 else None)


def _helper_call(node: ast.AST, params_name: str,
                 names: FrozenSet[str]) -> Optional[ast.Call]:
    """`_choice(params, "key", ...)` / `_point(params, "key", ...)`。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id not in names or len(node.args) < 2:
        return None
    if not _is_params(node.args[0], params_name) or _const_key(node.args[1]) is None:
        return None
    return node


def _wrapper_cast(node: ast.AST, parents: Dict[int, ast.AST]) -> Optional[str]:
    """取值表达式外包裹的转换函数名（`int(params.get(...))` → "int"）。

    只认「node 是外层 Call 的第一个位置参数」这一种形态，所以
    `float(params.get("a", params.get("b", D)))` 的内层 get 不会误判成 float。
    """
    parent = parents.get(id(node))
    if not isinstance(parent, ast.Call) or not isinstance(parent.func, ast.Name):
        return None
    name = parent.func.id
    if name not in _SCALAR_CASTS and name not in _SEQ_CASTS:
        return None
    if not parent.args or parent.args[0] is not node:
        return None
    return name


def _unwrap_default(node: Optional[ast.AST], params_name: str) -> Optional[ast.AST]:
    """兜底链 `params.get("a", params.get("b", D))` 的真实默认值是最内层的 D。"""
    seen = 0
    while node is not None and seen < 8:
        nested = _params_get_call(node, params_name)
        if nested is None:
            return node
        node = nested[1]
        seen += 1
    return node


def _infer_type(key: str, cast: Optional[str], has_default: bool, default: Any) -> str:
    if cast in _SCALAR_CASTS:
        return _SCALAR_CASTS[cast]
    if key.endswith("roi") or (has_default and _is_roi(default)):
        return "roi"
    if has_default:
        if isinstance(default, bool):
            return "bool"
        if isinstance(default, int):
            return "int"
        if isinstance(default, float):
            return "float"
        if isinstance(default, str):
            return "str"
    return "json"


class _Occurrence:
    """一次参数取值的原始记录（排序后按 key 首次出现去重）。"""

    __slots__ = ("pos", "param")

    def __init__(self, pos: Tuple[int, int], param: CustomActionParam) -> None:
        self.pos = pos
        self.param = param


def _extract(handler: Callable[..., Any]) -> List[CustomActionParam]:
    source = textwrap.dedent(inspect.getsource(handler))
    tree = ast.parse(source)
    globals_: Dict[str, Any] = getattr(handler, "__globals__", {}) or {}
    params_name = _params_arg_name(handler)

    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    occurrences: List[_Occurrence] = []
    #: 局部变量名 → 参数 key，供「`if v not in ALLOWED: raise`」的枚举推断用。
    var_to_key: Dict[str, str] = {}

    def pos_of(node: ast.AST) -> Tuple[int, int]:
        return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))

    for node in ast.walk(tree):
        got = _params_get_call(node, params_name)
        if got is not None:
            key, default_node = got
            resolved = _unwrap_default(default_node, params_name)
            has_default, default = _const(resolved, globals_)
            cast = _wrapper_cast(node, parents)
            occurrences.append(_Occurrence(pos_of(node), CustomActionParam(
                key=key,
                type=_infer_type(key, cast, has_default, default),
                default=_to_json(default) if has_default else None,
                default_unresolved=(resolved is not None and not has_default),
            )))
            continue

        choice = _helper_call(node, params_name, _CHOICE_HELPERS)
        if choice is not None:
            key = _const_key(choice.args[1]) or ""
            has_default, default = _const(
                choice.args[2] if len(choice.args) > 2 else None, globals_)
            choices = _str_seq(choice.args[3] if len(choice.args) > 3 else None, globals_)
            occurrences.append(_Occurrence(pos_of(node), CustomActionParam(
                key=key,
                type="enum" if choices else "str",
                default=_to_json(default) if has_default else None,
                default_unresolved=(len(choice.args) > 2 and not has_default),
                choices=choices,
            )))
            continue

        point = _helper_call(node, params_name, _POINT_HELPERS)
        if point is not None:
            key = _const_key(point.args[1]) or ""
            has_default, default = _const(
                point.args[2] if len(point.args) > 2 else None, globals_)
            occurrences.append(_Occurrence(pos_of(node), CustomActionParam(
                key=key,
                type="point",
                default=_to_json(default) if has_default else None,
                default_unresolved=(len(point.args) > 2 and not has_default
                                    and default is not None),
            )))
            continue

        if isinstance(node, ast.Subscript) and _is_params(node.value, params_name):
            key = _const_key(node.slice)
            if key is not None:
                occurrences.append(_Occurrence(pos_of(node), CustomActionParam(
                    key=key, type="json", required=True)))
            continue

        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                key = _assigned_param_key(node.value, params_name, var_to_key)
                if key is not None:
                    var_to_key[target.id] = key

    params = _dedupe(occurrences)
    _apply_membership_enums(tree, globals_, var_to_key, params)
    _apply_docs(handler, params)
    return list(params.values())


def _assigned_param_key(value: ast.AST, params_name: str,
                        var_to_key: Dict[str, str]) -> Optional[str]:
    """`x = params.get("k")` / `x = str(x)` → x 仍绑定参数 k。"""
    got = _params_get_call(value, params_name)
    if got is not None:
        return got[0]
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in _SCALAR_CASTS and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)):
        return var_to_key.get(value.args[0].id)
    if isinstance(value, ast.Name):
        return var_to_key.get(value.id)
    return None


def _dedupe(occurrences: List[_Occurrence]) -> Dict[str, CustomActionParam]:
    """按源码位置排序，同名 key 以首次出现为准（required 可被后续补上）。"""
    out: Dict[str, CustomActionParam] = {}
    for occ in sorted(occurrences, key=lambda o: o.pos):
        existing = out.get(occ.param.key)
        if existing is None:
            out[occ.param.key] = occ.param
        elif occ.param.required:
            existing.required = True
    return out


def _apply_membership_enums(tree: ast.AST, globals_: Dict[str, Any],
                            var_to_key: Dict[str, str],
                            params: Dict[str, CustomActionParam]) -> None:
    """`if order not in ORDER_CHOICES: raise ...` → order 是枚举。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.In, ast.NotIn)):
            continue
        left = node.left
        if not isinstance(left, ast.Name):
            continue
        key = var_to_key.get(left.id)
        param = params.get(key) if key else None
        if param is None or param.choices:
            continue
        choices = _str_seq(node.comparators[0], globals_)
        if not choices:
            continue
        param.choices = choices
        param.type = "enum"
