#!/usr/bin/env python3
"""数据驱动的项目语义 linter（移植自 Harness 文档 §5.6）。

规则定义在同目录 rules.json（数据），本文件是通用引擎（逻辑，不随规则变化）。
四层过滤流水线，逐层收窄、降低误报：
    1. path_contains / file_context  —— 文件级前置条件，不满足整条规则跳过
    2. pattern                       —— 行级主匹配
    3. exclude_patterns              —— 命中任一则跳过（排除合法写法）
    4. confirm_patterns              —— 须再命中任一才报错（二次确认）

设计原则（文档 §5.6）：成功静默、失败冗余。
    - 无违规  -> 不输出，exit 0
    - 有违规  -> 输出 行号+原因+修复+引用 到 stderr，exit 2（反馈给 Agent 自我纠正）

两种调用方式：
    1. Hook 模式（无参数）：从 stdin 读 Claude Code 工具负载 JSON，取 tool_input.file_path
    2. CLI 模式（带文件参数）：lint 指定文件，便于测试 / 手动跑
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

RULES_PATH = Path(__file__).with_name("rules.json")


def _reconfigure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def load_rules() -> list[dict]:
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("rules", [])


def norm(path: str) -> str:
    return path.replace("\\", "/")


def lint_file(path: str, rules: list[dict]) -> list[str]:
    """返回该文件的违规行（已格式化）。非 .py 或读不到则返回空。"""
    p = Path(path)
    if p.suffix != ".py" or not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    npath = norm(path)
    lines = text.splitlines()
    out: list[str] = []

    for rule in rules:
        contains = rule.get("path_contains")
        if contains and not any(c in npath for c in contains):
            continue
        fctx = rule.get("file_context")
        if fctx and not re.search(fctx, text):
            continue

        pattern = rule.get("pattern")
        if not pattern:
            continue
        excludes = rule.get("exclude_patterns") or []
        confirms = rule.get("confirm_patterns") or []

        for i, line in enumerate(lines, start=1):
            if not re.search(pattern, line):
                continue
            if any(re.search(e, line) for e in excludes):
                continue
            if confirms and not any(re.search(c, line) for c in confirms):
                continue
            msg = rule.get("violation_tpl", "{line}").format(line=line.strip())
            out.append(
                f"  L{i} [{rule.get('rule', rule.get('id', '?'))}] {msg}\n"
                f"      fix: {rule.get('fix', '')}\n"
                f"      ref: {rule.get('ref', '')}"
            )
    return out


def collect_paths() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    # Hook 模式：从 stdin 读工具负载 JSON
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    fp = (payload.get("tool_input") or {}).get("file_path")
    return [fp] if fp else []


def main() -> int:
    _reconfigure_utf8()
    rules = load_rules()
    if not rules:
        return 0
    violations: list[str] = []
    for path in collect_paths():
        found = lint_file(path, rules)
        if found:
            violations.append(f"[project-lint] {path}\n" + "\n".join(found))
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
