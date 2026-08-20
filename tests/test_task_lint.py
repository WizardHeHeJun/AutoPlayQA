from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import mcp_server
from task.task_lint import LintWarning, lint_task


def _rule_ids(warnings):
    return [w.rule_id for w in warnings]


def _by_rule(warnings, rule_id):
    return [w for w in warnings if w.rule_id == rule_id]


# ---------- W001: non-terminal node missing on_timeout ----------

def test_w001_flags_non_terminal_node_without_on_timeout():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "x"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["b"],
            },
            "b": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    hits = _by_rule(lint_task(task), "W001")
    assert len(hits) == 1
    assert hits[0].node == "a"


def test_w001_pass_when_on_timeout_present():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "x"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["b"],
                "on_timeout": "b",
            },
            "b": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    assert _by_rule(lint_task(task), "W001") == []


def test_w001_exempts_always_recognition():
    """`always` matches instantly (score 1.0, no polling) so it can never
    time out -- see recognizers.RecognizerHub.recognize / WATCHDOG_TYPES."""
    task = {
        "entry": "a",
        "nodes": {
            "a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": ["b"]},
            "b": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    assert _by_rule(lint_task(task), "W001") == []


def test_w001_pass_for_terminal_node():
    task = {
        "entry": "a",
        "nodes": {
            "a": {"recognition": {"type": "ocr", "expected": "x"}, "action": {"type": "none"}, "next": []},
        },
    }
    assert _by_rule(lint_task(task), "W001") == []


# ---------- W002: suspect anomaly branch missing finding ----------

def test_w002_flags_error_keyword_node_without_finding():
    task = {
        "entry": "a",
        "nodes": {
            "a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": ["网络错误"]},
            "网络错误": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    hits = _by_rule(lint_task(task), "W002")
    assert len(hits) == 1
    assert hits[0].node == "网络错误"


def test_w002_pass_when_finding_present():
    task = {
        "entry": "a",
        "nodes": {
            "a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": ["网络错误"]},
            "网络错误": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "finding": "网络错误弹窗",
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W002") == []


def test_w002_flags_watchdog_skip_to_target_without_finding():
    task = {
        "entry": "a",
        "watchdogs": [{"type": "ocr", "expected": "崩溃", "skip_to": "recover"}],
        "nodes": {
            "a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": ["recover"]},
            "recover": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    hits = _by_rule(lint_task(task), "W002")
    assert [h.node for h in hits] == ["recover"]


def test_w002_flags_on_finding_target_without_finding():
    task = {
        "entry": "a",
        "on_finding": "recover",
        "nodes": {
            "a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": ["recover"]},
            "recover": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []},
        },
    }
    hits = _by_rule(lint_task(task), "W002")
    assert [h.node for h in hits] == ["recover"]


# ---------- W003: cold start without popups whitelist ----------

def test_w003_flags_launch_app_without_popups():
    task = {
        "entry": "启动",
        "nodes": {
            "启动": {
                "recognition": {"type": "always"},
                "action": {"type": "custom", "name": "launch_app", "params": {"package": "com.x"}},
                "next": [],
            },
        },
    }
    hits = _by_rule(lint_task(task), "W003")
    assert len(hits) == 1
    assert hits[0].node == "启动"


def test_w003_pass_when_popups_declared():
    task = {
        "entry": "启动",
        "popups": [
            {
                "recognition": {"type": "ocr", "expected": "同意"},
                "action": {"type": "click", "target": "recognized"},
            }
        ],
        "nodes": {
            "启动": {
                "recognition": {"type": "always"},
                "action": {"type": "custom", "name": "launch_app", "params": {"package": "com.x"}},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W003") == []


def test_w003_pass_without_launch_app():
    task = {
        "entry": "a",
        "nodes": {"a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []}},
    }
    assert _by_rule(lint_task(task), "W003") == []


# ---------- W004: hardcoded click coords where a recognized anchor exists ----------

def test_w004_flags_hardcoded_click_with_recognizable_anchor():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "登录"},
                "action": {"type": "click", "params": {"x": 10, "y": 20}},
                "next": [],
            },
        },
    }
    hits = _by_rule(lint_task(task), "W004")
    assert len(hits) == 1
    assert hits[0].node == "a"


def test_w004_pass_when_target_recognized():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "登录"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W004") == []


@pytest.mark.parametrize("rec_type", ["always", "blank_screen"])
def test_w004_pass_for_anchorless_recognition(rec_type):
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": rec_type},
                "action": {"type": "click", "params": {"x": 10, "y": 20}},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W004") == []


# ---------- W007: scene-gated node with target: recognized ----------

def test_w007_flags_plain_scene_recognition_with_click_recognized():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "scene", "expected": "home_main", "min_conf": 0.6},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    hits = _by_rule(lint_task(task), "W007")
    assert len(hits) == 1
    assert hits[0].node == "a"


def test_w007_flags_combo_recognition_that_is_scene_only():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {
                    "type": "or",
                    "any_of": [
                        {"type": "scene", "expected": "home_main"},
                        {"type": "scene", "expected": "shop"},
                    ],
                },
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    hits = _by_rule(lint_task(task), "W007")
    assert len(hits) == 1
    assert hits[0].node == "a"


def test_w007_pass_when_action_is_not_click_recognized():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "scene", "expected": "home_main"},
                "action": {"type": "none"},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W007") == []


def test_w007_pass_when_combo_mixes_scene_with_anchor_type():
    """A scene branch alongside an ocr branch can still produce a real click
    anchor via the ocr side, so this is not statically doomed."""
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {
                    "type": "or",
                    "any_of": [
                        {"type": "scene", "expected": "home_main"},
                        {"type": "ocr", "expected": "登录"},
                    ],
                },
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W007") == []


def test_w007_pass_for_non_scene_recognition():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "登录"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W007") == []


# ---------- W005: no QA assertions at all ----------

def test_w005_flags_task_without_watchdogs_or_finding_nodes():
    task = {
        "entry": "a",
        "nodes": {"a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []}},
    }
    hits = _by_rule(lint_task(task), "W005")
    assert len(hits) == 1
    assert hits[0].node is None


def test_w005_pass_with_watchdog():
    task = {
        "entry": "a",
        "watchdogs": [{"type": "ocr", "expected": "错误"}],
        "nodes": {"a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []}},
    }
    assert _by_rule(lint_task(task), "W005") == []


def test_w005_pass_with_finding_node():
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "finding": "异常分支",
                "next": [],
            },
        },
    }
    assert _by_rule(lint_task(task), "W005") == []


# ---------- W006: included fragment nobody can reach ----------

def _include_task(**overrides):
    """A merged task shaped like resolve_task's output: main node 'a' plus two
    fragment nodes, with `_merge.include_map` recording where each came from."""
    task = {
        "entry": "a",
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "x"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
                "on_timeout": "a",
                "finding": "锚点丢失",
            },
            "关闭弹窗": {
                "recognition": {"type": "ocr", "expected": "关闭"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["a"],
                "on_timeout": "a",
            },
            "回主界面": {
                "recognition": {"type": "always"},
                "action": {"type": "none"},
                "next": ["a"],
            },
        },
        "_merge": {
            "includes": ["/abs/common/popups.json"],
            "conflicts": [],
            "include_map": {
                "关闭弹窗": "common/popups.json",
                "回主界面": "common/popups.json",
                "a": "<task>",
            },
        },
    }
    task.update(overrides)
    return task


def test_w006_flags_include_with_no_reachable_node():
    hits = _by_rule(lint_task(_include_task()), "W006")
    assert len(hits) == 1
    assert hits[0].node is None
    assert "common/popups.json" in hits[0].message


def test_w006_pass_when_one_fragment_node_is_reachable():
    task = _include_task()
    task["nodes"]["a"]["next"] = ["关闭弹窗"]
    assert _by_rule(lint_task(task), "W006") == []


def test_w006_reachability_follows_on_timeout():
    task = _include_task()
    task["nodes"]["a"]["on_timeout"] = "回主界面"
    assert _by_rule(lint_task(task), "W006") == []


def test_w006_counts_bug_skip_landing_pads_as_entry_points():
    """A recovery branch reached only via watchdog skip_to / on_finding is live
    flow, not dead weight -- the same seeds W002 already treats as real."""
    task = _include_task(watchdogs=[{"type": "ocr", "expected": "错误", "skip_to": "回主界面"}])
    assert _by_rule(lint_task(task), "W006") == []

    task = _include_task(on_finding="回主界面")
    assert _by_rule(lint_task(task), "W006") == []


def test_w006_reports_each_unreachable_include_separately():
    task = _include_task()
    task["nodes"]["a"]["next"] = ["关闭弹窗"]
    task["_merge"]["include_map"]["回主界面"] = "common/home.json"
    hits = _by_rule(lint_task(task), "W006")
    assert len(hits) == 1
    assert "common/home.json" in hits[0].message


def test_w006_silent_without_includes():
    task = {
        "entry": "a",
        "nodes": {"a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []}},
    }
    assert _by_rule(lint_task(task), "W006") == []


# ---------- general shape ----------

def test_lint_task_returns_lintwarning_instances_with_to_dict():
    task = {
        "entry": "a",
        "nodes": {"a": {"recognition": {"type": "always"}, "action": {"type": "none"}, "next": []}},
    }
    warnings = lint_task(task)
    assert warnings and all(isinstance(w, LintWarning) for w in warnings)
    d = warnings[0].to_dict()
    assert set(d) == {"rule_id", "node", "message", "suggestion"}


def test_lint_task_tolerates_missing_nodes():
    assert lint_task({}) == []
    assert lint_task({"entry": "a"}) == []


# ---------- save_task integration: lint_warnings surfaced, strict blocks ----------

def test_save_task_attaches_lint_warnings(tmp_path, sample_task):
    with patch.object(mcp_server, "DEFAULT_TASK_DIR", tmp_path), \
            patch.object(mcp_server, "get_task_path", lambda name: tmp_path / f"{name}.json"):
        result = mcp_server.save_task("demo", json.dumps(sample_task))
    assert result["ok"] is True
    assert "lint_warnings" in result
    assert _rule_ids([LintWarning(**w) for w in result["lint_warnings"]])
    assert (tmp_path / "demo.json").is_file()


def test_save_task_strict_refuses_when_warnings_present(tmp_path, sample_task):
    with patch.object(mcp_server, "DEFAULT_TASK_DIR", tmp_path), \
            patch.object(mcp_server, "get_task_path", lambda name: tmp_path / f"{name}.json"), \
            patch.object(mcp_server, "_config", {"lint": {"strict": True}}):
        result = mcp_server.save_task("demo", json.dumps(sample_task))
    assert result["ok"] is False
    assert result["lint_warnings"]
    assert not (tmp_path / "demo.json").exists()


def test_save_task_keeps_includes_instead_of_inlining_fragment(tmp_path):
    """Fragment nodes must NOT be written into the task file: the whole point of
    an include is that editing the fragment updates every task that uses it."""
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "popups.json").write_text(json.dumps({
        "description": "通用弹窗",
        "nodes": {
            "关闭弹窗": {
                "recognition": {"type": "ocr", "expected": "关闭"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }, ensure_ascii=False), encoding="utf-8")
    task = {
        "entry": "a",
        "includes": ["common/popups.json"],
        "watchdogs": [{"type": "ocr", "expected": "错误"}],
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "x"},
                "action": {"type": "click", "target": "recognized"},
                "next": ["关闭弹窗"],
                "on_timeout": "a",
            },
        },
    }
    with patch.object(mcp_server, "DEFAULT_TASK_DIR", tmp_path), \
            patch.object(mcp_server, "get_task_path", lambda name: tmp_path / f"{name}.json"):
        result = mcp_server.save_task("demo", json.dumps(task))

    assert result["ok"] is True
    assert result["nodes"] == 2  # merged view is validated/counted
    saved = json.loads((tmp_path / "demo.json").read_text(encoding="utf-8"))
    assert saved["includes"] == ["common/popups.json"]
    assert set(saved["nodes"]) == {"a"}
    assert _by_rule([LintWarning(**w) for w in result["lint_warnings"]], "W006") == []


def test_save_task_strict_allows_when_no_warnings(tmp_path):
    clean_task = {
        "entry": "a",
        "watchdogs": [{"type": "ocr", "expected": "错误"}],
        "nodes": {
            "a": {
                "recognition": {"type": "ocr", "expected": "x"},
                "action": {"type": "click", "target": "recognized"},
                "next": [],
            },
        },
    }
    with patch.object(mcp_server, "DEFAULT_TASK_DIR", tmp_path), \
            patch.object(mcp_server, "get_task_path", lambda name: tmp_path / f"{name}.json"), \
            patch.object(mcp_server, "_config", {"lint": {"strict": True}}):
        result = mcp_server.save_task("demo", json.dumps(clean_task))
    assert result["ok"] is True
    assert result["lint_warnings"] == []
    assert (tmp_path / "demo.json").is_file()
