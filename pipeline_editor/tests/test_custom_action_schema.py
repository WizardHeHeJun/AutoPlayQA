"""custom action 参数 schema 提取回归。

被测对象：`backend/action_schema_introspect.py` —— 用 AST 从 AutoPlayQA 的
handler 源码里反解参数表。断言优先钉在**随框架分发的内置 handler** 上（不造假
源码），因为这套提取的价值全在于跟得上上游那份代码；上游改了取值写法，这里应该红。

内置 handler 没有用到的取值写法（`_choice` / `_point` 助手、四元 roi 默认值、
`params.get("a", params.get("b", D))` 兜底链等）由本文件里的替身 handler
`_shaped_handler` 覆盖：它按上游同一套约定书写，是提取器的形态目录，而不是对
某个真实 handler 的复制。

跑法（用项目的 conda 环境解释器）：

    python -m pytest pipeline_editor/tests/test_custom_action_schema.py -q
    python pipeline_editor/tests/test_custom_action_schema.py   # 免 pytest
"""
from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.action_schema_introspect as introspect  # noqa: E402
from backend.action_schema_introspect import (  # noqa: E402
    DESCRIPTION_MAX,
    PARAM_TYPES,
    CustomActionParam,
    extract_params,
    parse_param_docs,
)
from backend.routers.meta import api_custom_action_schema  # noqa: E402

from task.custom_actions import get_handler, registered_names  # noqa: E402


def _params(name: str) -> Dict[str, CustomActionParam]:
    handler = get_handler(name)
    assert handler is not None, f"custom action 未注册: {name}"
    return {p.key: p for p in extract_params(handler)}


# ---------- 0. 形态目录：上游约定的取值写法（替身 handler） ----------

DEFAULT_MODE = "fast"
MODE_CHOICES = ("fast", "slow", "precise")
STRATEGY_CHOICES = ("nearest", "topmost", "largest")
DEFAULT_ORIGIN = (540, 1500)
DEFAULT_BUDGET_MS = 12000
DEFAULT_SCAN_ROI = (10, 135, 560, 260)


def _choice(params: Dict, key: str, default: Any, allowed: Sequence[str]) -> Any:
    """上游 handler 里的枚举取值助手（同名同签名，提取器按名字认它）。"""
    value = params.get(key, default)
    if value not in allowed:
        raise ValueError(f"{key} must be one of {allowed}")
    return value


def _point(params: Dict, key: str, default: Any = None) -> Any:
    """上游 handler 里的坐标点取值助手。"""
    return params.get(key, default)


def _shaped_handler(ctx: Any, params: Dict) -> List[Dict]:  # noqa: ANN401
    """Stand-in handler exercising every value-reading shape the extractor knows.

    params:
      mode:  how aggressively to move
             (default "fast")
      gain_x / gain_y: error-to-drag DIVISORS
      scan_roi: [x1,y1,x2,y2] around the on-screen counter
                (default [10,135,560,260])
      probe_roi: [x1,y1,x2,y2] sampled for the progress bar (default None)
    """
    mode = _choice(params, "mode", DEFAULT_MODE, MODE_CHOICES)
    origin = _point(params, "origin", DEFAULT_ORIGIN)
    anchor = _point(params, "anchor")
    attempts = int(params.get("attempts", 6))
    ratio = float(params.get("ratio", 0.35))
    label = str(params.get("label", "row"))
    enabled = bool(params.get("enabled", True))
    scan_roi = params.get("scan_roi", DEFAULT_SCAN_ROI)
    probe_roi = params.get("probe_roi")
    budget = float(params.get("time_budget_ms", params.get("budget_ms", DEFAULT_BUDGET_MS)))
    band = params.get("band", [0.18, 0.78])
    gain_x = float(params.get("gain_x", 4.0))
    gain_y = float(params.get("gain_y", 2.5))
    target = params["target"]
    strategy = params.get("strategy")
    if strategy is not None and strategy not in STRATEGY_CHOICES:
        raise ValueError("strategy")
    return [{
        "ok": "True",
        "stdout": f"{ctx} {mode} {origin} {anchor} {attempts} {ratio} {label} "
                  f"{enabled} {scan_roi} {probe_roi} {budget} {band} "
                  f"{gain_x} {gain_y} {target} {strategy}",
        "stderr": "",
    }]


def test_shaped_handler_covers_every_param_shape() -> None:
    params = {p.key: p for p in extract_params(_shaped_handler)}

    # _choice(params, key, DEFAULT, ALLOWED) → 枚举 + choices
    mode = params["mode"]
    assert mode.type == "enum"
    assert mode.default == "fast"
    assert mode.choices == ["fast", "slow", "precise"]

    # 「params.get + 手写 not in ALLOWED 校验」也要认出枚举
    strategy = params["strategy"]
    assert strategy.type == "enum"
    assert strategy.choices == ["nearest", "topmost", "largest"]
    assert strategy.default is None, "没有默认值，不该凭空造一个"

    # int(params.get(...)) → int，默认值可以是模块顶层常量
    assert params["attempts"].type == "int" and params["attempts"].default == 6

    # float / str / bool 转换
    assert params["ratio"].type == "float"
    assert abs(float(params["ratio"].default) - 0.35) < 1e-9
    assert params["label"].type == "str" and params["label"].default == "row"
    assert params["enabled"].type == "bool" and params["enabled"].default is True

    # 4 元默认值 / key 以 roi 结尾 → roi
    scan_roi = params["scan_roi"]
    assert scan_roi.type == "roi"
    assert scan_roi.default == [10, 135, 560, 260], "tuple 默认值要转成 JSON 数组"
    assert params["probe_roi"].type == "roi", "无默认值也应按 key 后缀判成 roi"
    assert params["probe_roi"].default is None

    # _point(params, key, DEFAULT) → point
    assert params["origin"].type == "point"
    assert params["origin"].default == [540, 1500]
    assert params["anchor"].type == "point" and params["anchor"].default is None

    # 认不出的形态（序列 / dict）退化成 json，不瞎猜
    assert params["band"].type == "json" and params["band"].default == [0.18, 0.78]

    # params["key"] 直取 → 必填
    assert params["target"].required is True and params["target"].type == "json"


def test_fallback_chain_records_both_keys() -> None:
    """`float(params.get("time_budget_ms", params.get("budget_ms", D)))`。

    两个 key 各记一条，且默认值都穿透到最内层常量——不能把内层 get 表达式
    当成外层的默认值（那会变成 default_unresolved）。
    """
    params = {p.key: p for p in extract_params(_shaped_handler)}
    assert params["time_budget_ms"].default == 12000
    assert params["time_budget_ms"].default_unresolved is False
    assert params["budget_ms"].default == 12000
    # 内层 get 不在 float(...) 的第一个位置参数上，不该被误判成 float
    assert params["budget_ms"].type == "int"


def test_param_order_follows_source() -> None:
    keys = [p.key for p in extract_params(_shaped_handler)]
    assert keys[0] == "mode", keys[:3]
    assert keys.index("attempts") < keys.index("scan_roi")
    assert len(keys) == len(set(keys)), "同一个 key 被记了多条"


# ---------- 1. 随框架分发的内置 handler ----------

def test_builtin_handlers_extract_without_blowing_up() -> None:
    launch = _params("launch_app")
    assert launch["force_stop"].type == "bool" and launch["force_stop"].default is False
    assert launch["settle_ms"].type == "int" and isinstance(launch["settle_ms"].default, int)

    gm = _params("gm_command")
    assert gm["exec_button"].required is True, "params[\"key\"] 直取应标必填"
    assert gm["command"].required is False

    swipe = _params("swipe_until")
    assert swipe["max_swipes"].type == "int" and swipe["max_swipes"].default == 5
    assert swipe["recognition"].type == "json"

    checkbox = _params("ensure_checkbox")
    assert checkbox["tolerance"].type == "int" and checkbox["tolerance"].default == 60
    assert checkbox["checked_rgb"].type == "json", "3 元颜色不是 roi"

    text_field = _params("set_text_field")
    assert text_field["clear"].type == "int" and text_field["clear"].default == 40


def test_builtin_membership_enum_and_roi_suffix() -> None:
    """click_topmost_text：手写成员校验 → 枚举；`roi` 后缀 → roi 控件。"""
    params = _params("click_topmost_text")

    order = params["order"]
    assert order.type == "enum"
    assert order.default == "top"
    assert set(order.choices or []) == {"top", "bottom"}

    assert params["roi"].type == "roi" and params["roi"].default is None
    assert params["threshold"].type == "float"
    assert abs(float(params["threshold"].default) - 0.65) < 1e-9


def test_every_registered_handler_is_safe_to_introspect() -> None:
    """提取对所有已注册 handler 都不抛异常，且产出形状合法。"""
    assert registered_names(), "一个 custom action 都没注册"
    for name in registered_names():
        handler = get_handler(name)
        assert handler is not None
        for param in extract_params(handler):
            assert param.key and isinstance(param.key, str), name
            assert param.type in PARAM_TYPES, (name, param.key, param.type)
            if param.type == "enum":
                assert param.choices, (name, param.key)
            if param.choices is not None:
                assert all(isinstance(c, str) for c in param.choices), (name, param.key)


def test_extraction_is_cached() -> None:
    handler = get_handler("swipe_until")
    assert extract_params(handler) is extract_params(handler)


def test_bad_handler_degrades_to_empty_list() -> None:
    """拿不到源码的可调用对象：返回空列表，不抛。"""
    builtin = len  # C 实现，inspect.getsource 抛 TypeError
    assert extract_params(builtin) == []  # type: ignore[arg-type]
    assert extract_params(lambda ctx, params: []) == []


# ---------- 2. docstring 参数说明 ----------

def test_descriptions_come_from_docstring() -> None:
    """`params (...)` 块：普通条目 / 别名条目 / 带续行的条目。

    断言打在**解析层**（英文原文）：`param.description` 上还叠了一层中文对照表，
    那一层单独由 test_zh_* 覆盖。
    """
    docs = parse_param_docs(inspect.getdoc(_shaped_handler))

    mode = docs["mode"]
    assert "how aggressively to move" in mode
    # 续行合并进同一条，且 (default ...) 原样保留
    assert 'default "fast"' in mode

    # `gain_x / gain_y:` 别名条目 —— 两个 key 拿到同一条说明
    assert docs["gain_x"] == docs["gain_y"]
    assert "DIVISORS" in docs["gain_x"]

    # 多行续行压缩成单行（不留换行 / 连续空格）
    scan_roi = docs["scan_roi"]
    assert "on-screen counter" in scan_roi
    assert "[10,135,560,260]" in scan_roi
    assert "\n" not in scan_roi and "  " not in scan_roi

    # 解析结果确实贴到了参数上（别名两条仍相等；坐标之类的原文细节不丢）
    params = {p.key: p for p in extract_params(_shaped_handler)}
    assert params["gain_x"].description == params["gain_y"].description
    assert "[10,135,560,260]" in (params["scan_roi"].description or "")


def test_real_handler_descriptions_are_attached() -> None:
    """内置 handler 的说明确实来自它自己的 docstring（含续行合并）。"""
    docs = parse_param_docs(inspect.getdoc(get_handler("gm_command")))
    assert "no single quotes" in docs["command"], "续行没并进同一条"
    assert "\n" not in docs["command"]

    assert "checkmark color" in parse_param_docs(
        inspect.getdoc(get_handler("ensure_checkbox")))["probe"]


def test_descriptions_are_bounded_and_never_empty_strings() -> None:
    for name in registered_names():
        for param in extract_params(get_handler(name)):
            if param.description is None:
                continue
            assert param.description.strip(), (name, param.key)
            assert len(param.description) <= DESCRIPTION_MAX, (name, param.key)


def test_docstring_without_params_block_yields_no_descriptions() -> None:
    def handler(ctx, params):  # noqa: ANN001
        """Do a thing.

        Talks to the device, returns executor-style results. Nothing here is a
        parameter table: threshold and timeout are just words in a sentence.
        """
        return [params.get("threshold", 0.5), params.get("timeout", 3)]

    extracted = extract_params(handler)
    assert {p.key for p in extracted} == {"threshold", "timeout"}
    assert all(p.description is None for p in extracted), \
        "普通叙述文字不该被当成参数条目"


def test_undocumented_params_stay_none() -> None:
    """替身 handler 的 docstring 只列了一部分参数，漏掉的不能凭空造说明。"""
    params = {p.key: p for p in extract_params(_shaped_handler)}
    assert "counter" in (params["scan_roi"].description or "")
    assert params["attempts"].description is None
    assert params["time_budget_ms"].description is None


def test_parse_param_docs_tolerates_junk() -> None:
    assert parse_param_docs(None) == {}
    assert parse_param_docs("") == {}
    assert parse_param_docs("   \n\n  ") == {}
    assert parse_param_docs("no params block here at all") == {}
    assert parse_param_docs("params:") == {}  # 标记行后什么都没有
    assert parse_param_docs(123) == {}  # type: ignore[arg-type]


def test_parse_param_docs_shapes() -> None:
    docs = parse_param_docs(
        "Summary line.\n"
        "\n"
        "params (all optional):\n"
        "  Detection\n"
        "    alpha:  first param\n"
        "            wrapped onto a second line\n"
        "    beta / gamma: shared by both keys\n"
        "  Geometry\n"
        "    delta:  ratio a:b, colons in the text are fine\n"
        "\n"
        "Returns nothing useful.\n"
    )
    assert docs["alpha"] == "first param wrapped onto a second line"
    assert docs["beta"] == "shared by both keys"
    assert docs["gamma"] == docs["beta"]
    assert docs["delta"] == "ratio a:b, colons in the text are fine"
    assert "Detection" not in docs and "Returns" not in docs


def test_parse_param_docs_truncates() -> None:
    docs = parse_param_docs("params:\n  key: " + "x" * (DESCRIPTION_MAX * 2))
    assert len(docs["key"]) == DESCRIPTION_MAX
    assert docs["key"].endswith("…")


# ---------- 3. 中文对照表 ----------

@contextmanager
def _zh_docs(table: Optional[Dict[str, Dict[str, Dict[str, str]]]]) -> Iterator[None]:
    """临时替换对照表（顺带清提取缓存，免得读到上一次贴好说明的结果）。

    手写 try/finally 而不是 pytest 的 monkeypatch：本文件还支持免 pytest 的
    `main()` 跑法。
    """
    original = introspect._ZH_DOCS
    introspect._ZH_DOCS = table
    introspect._CACHE.clear()
    try:
        yield
    finally:
        introspect._ZH_DOCS = original
        introspect._CACHE.clear()


def _doc_handler(ctx: Any, params: Dict) -> List[Dict]:  # noqa: ANN401
    """Local stand-in for a real handler.

    params:
      alpha:  first param, documented in English
    """
    return [params.get("alpha", 1)]


_ALPHA_EN = "first param, documented in English"


def test_zh_table_localizes_real_handler() -> None:
    """对照表命中 → description 换成中文；技术细节（默认值/术语）不丢。"""
    threshold = _params("click_topmost_text")["threshold"].description or ""
    assert "相似度" in threshold, threshold
    assert "similarity gate" not in threshold
    assert "（默认 0.65）" in threshold, "(default X) 要译成（默认 X）且保留取值"
    assert "ocr" in threshold, "术语保留英文"

    assert "ms" in (_params("swipe_until")["settle_ms"].description or ""), "单位保留英文"
    assert "App" in (_params("launch_app")["force_stop"].description or "")


def test_zh_falls_back_when_english_drifted() -> None:
    """对照表里的 en 与当前 docstring 不一致（上游改过文案）→ 回退英文。"""
    name = _doc_handler.__name__

    with _zh_docs({name: {"alpha": {"en": _ALPHA_EN, "zh": "第一个参数，英文有文档"}}}):
        hit = {p.key: p for p in extract_params(_doc_handler)}
        assert hit["alpha"].description == "第一个参数，英文有文档"

    stale = {name: {"alpha": {"en": _ALPHA_EN + " (changed upstream)", "zh": "过期译文"}}}
    with _zh_docs(stale):
        drifted = {p.key: p for p in extract_params(_doc_handler)}
        assert drifted["alpha"].description == _ALPHA_EN, "en 对不上必须回退英文"


def test_missing_zh_table_keeps_english() -> None:
    """对照表缺失（空表）→ 全英文，且不抛。"""
    with _zh_docs({}):
        params = {p.key: p for p in extract_params(_doc_handler)}
        assert params["alpha"].description == _ALPHA_EN

        pick = {p.key: p for p in extract_params(get_handler("click_topmost_text"))}
        assert "similarity gate" in (pick["threshold"].description or "")


def test_zh_table_loader_tolerates_bad_files() -> None:
    """文件不存在 / 不是 JSON / 结构不对 → 空表，不抛。"""
    original = introspect._ZH_DOCS_PATH
    try:
        introspect._ZH_DOCS_PATH = REPO_ROOT / "definitely" / "not" / "here.json"
        assert introspect._load_zh_docs() == {}
        introspect._ZH_DOCS_PATH = Path(__file__)  # 是文件但不是 JSON
        assert introspect._load_zh_docs() == {}
    finally:
        introspect._ZH_DOCS_PATH = original

    # 形状不对的条目逐条丢弃，不污染其余条目
    assert introspect._localize(None, "en text") == "en text"
    assert introspect._localize({"en": "en text"}, "en text") == "en text"
    assert introspect._localize({"en": "en text", "zh": "中文"}, "en text") == "中文"


def test_zh_table_is_in_sync_with_current_docstrings() -> None:
    """对照表的 en 必须等于当前 docstring 的原文——不等就是上游改了文案。

    运行期会静默回退英文（用户看不到坏译文），但仓库里的过期译文该被这条测试
    抓出来更新。
    """
    table = introspect._load_zh_docs()
    assert table, "对照表读不出来（文件缺失或结构坏了）"

    stale: List[str] = []
    for name in registered_names():
        handler = get_handler(name)
        docs = parse_param_docs(inspect.getdoc(handler))
        for key, entry in table.get(getattr(handler, "__name__", ""), {}).items():
            if docs.get(key) != entry["en"]:
                stale.append(f"{name}.{key}")
            assert len(entry["zh"]) <= 400, f"{name}.{key} 译文超长"
    assert not stale, f"英文原文已变，译文需更新: {stale}"

    # 对照表只覆盖随框架分发的内置 handler；本机自加的 handler 走英文原文。
    assert set(table) <= set(registered_names()), "对照表里有已经不存在的 handler"
    covered = sum(len(v) for v in table.values())
    assert covered == 25, f"对照表条目数变了（{covered}），确认是否有 handler 增删"


# ---------- 4. 路由 ----------

def test_route_returns_schema_and_404() -> None:
    payload = api_custom_action_schema("swipe_until")
    assert payload.name == "swipe_until"
    assert any(p.key == "max_swipes" for p in payload.params)

    try:
        api_custom_action_schema("definitely_not_registered")
    except Exception as exc:  # HTTPException
        assert getattr(exc, "status_code", None) == 404, exc
    else:
        raise AssertionError("未注册的 name 应该 404")


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
