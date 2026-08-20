#!/usr/bin/env python3
"""回放轮次分诊器：跑完一轮就把 findings 分成三堆，提醒该不该出报告。

冒烟汇报流程的**触发端**。build_report.py 负责"已经想清楚之后怎么出报告"，
本文件负责"刚跑完这一轮，有没有新东西需要你判断"：

    产品缺陷候选   crash / native_crash / anr / watchdog / anomaly_node(error+)
    已报缺陷复现   签名命中 bugs.json 里某条缺陷的 signatures -> 只需更新复现率
    工具侧噪音     timeout_recovery / anchor_drift / anchor_rot_suspect /
                   unknown_popup_backoff，以及 meta.known_noise 里点名的签名

**签名** = (task, type, node) 三元组，跨 run 稳定，用来把"同一个 bug 第 N 次
复现"和"新冒出来的问题"分开——否则每轮都会把老缺陷重新报一遍。
bugs.json 里 node 省略表示通配该 type。

机器只做分类，不做判断：候选是否真是产品缺陷，仍由智能体按 SKILL.md 的判据定。

用法：
    python triage.py --latest [N]        分诊最近 N 轮（默认 1）
    python triage.py --run <run 目录>     分诊指定轮次
    python triage.py --pending           列出所有"含未知签名候选"的轮次
    python triage.py --hook              PostToolUse 钩子模式（读 stdin，输出 JSON）
    python triage.py --session-hook      SessionStart 钩子模式（仅有待分诊时发声）

    --bugs <path>   缺陷数据源，默认 outputs/bug_reports/bugs.json
    --findings <d>  findings 根目录，默认 outputs/findings

钩子模式永远 exit 0 且静默失败：分诊挂了不该打断跑测。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 产品缺陷候选：这些 type 说明"游戏出问题了"，不论级别都必须人工过一遍
CANDIDATE_TYPES = {"crash", "native_crash", "anr", "watchdog"}
# 这些 type 要看级别：异常分支自我上报，error 及以上直接算候选
LEVEL_GATED_TYPES = {"anomaly_node", "task_failure"}
CANDIDATE_SEVERITIES = {"error", "critical"}
# 工具侧噪音：脚本自身的识别加固问题，不进缺陷报告
NOISE_TYPES = {"timeout_recovery", "anchor_drift", "anchor_rot_suspect", "unknown_popup_backoff"}
# warning 级的异常分支既可能是脚本适配问题，也可能是真缺陷（例如"该发的奖励
# 没到账"往往只记成 warning 级 anomaly_node）。不许静默归噪音，单列一档让人扫一眼；确认是噪音后
# 写进 bugs.json 的 meta.known_noise 才会闭嘴。

DEFAULT_BUGS = "outputs/bug_reports/bugs.json"
DEFAULT_FINDINGS = "outputs/findings"


def _reconfigure() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass


def signature(task: str, finding: Dict) -> Tuple[str, str, Optional[str]]:
    return (task, finding.get("type") or "", finding.get("node"))


def load_known(bugs_path: Path) -> Tuple[Dict[Tuple, List[str]], List[Tuple], Dict[Tuple, str]]:
    """读 bugs.json，返回三张签名表。文件不存在则全为空。

    三张表语义互不相同，别混用：
      known  bugs[].signatures  —— 已报缺陷的指纹，命中 = 又复现一次
      noise  meta.known_noise   —— 判定为脚本侧问题，不是缺陷
      acked  meta.acknowledged  —— **是**候选，但已看过并移交他人跟进，不再提示

    一个签名可对应多条缺陷——同一现象拆成两条独立缺陷时（服务端接口报错 +
    客户端拿到错误不提示玩家），它们共用同一条 finding 的签名。
    """
    known: Dict[Tuple, List[str]] = {}
    noise: List[Tuple] = []
    acked: Dict[Tuple, str] = {}
    if not bugs_path.is_file():
        return known, noise, acked
    try:
        spec = json.loads(bugs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return known, noise, acked
    for bug in spec.get("bugs") or []:
        for sig in bug.get("signatures") or []:
            known.setdefault((sig.get("task"), sig.get("type"), sig.get("node")), []).append(bug["id"])
    meta = spec.get("meta") or {}
    for sig in meta.get("known_noise") or []:
        noise.append((sig.get("task"), sig.get("type"), sig.get("node")))
    for sig in meta.get("acknowledged") or []:
        acked[(sig.get("task"), sig.get("type"), sig.get("node"))] = sig.get("owner") or "已移交"
    return known, noise, acked


def _match(sig: Tuple, table) -> Optional[Tuple]:
    """精确匹配优先；node 为 None 的登记项通配该 (task, type) 下所有 node。"""
    task, ftype, node = sig
    for key in (sig, (task, ftype, None)):
        if isinstance(table, dict):
            if key in table:
                return key
        elif key in table:
            return key
    return None


def triage_run(
    run_dir: Path,
    known: Dict[Tuple, List[str]],
    noise: List[Tuple],
    acked: Optional[Dict[Tuple, str]] = None,
) -> Optional[Dict]:
    """分诊一个 run 目录，返回分类结果；没有 report.json 则返回 None。"""
    report = run_dir / "report.json"
    if not report.is_file():
        return None
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    task = data.get("task") or "(unknown)"
    result = {
        "run_dir": run_dir,
        "task": task,
        "status": data.get("status"),
        "started_at": data.get("started_at"),
        "duration": _duration(data),
        "new": [],        # 未知签名的候选：必须人工分诊
        "reported": [],   # 命中已报缺陷：更新复现率即可
        "minor": [],      # warning 级异常分支：可能是缺陷也可能是脚本适配，扫一眼
        "acked": [],      # 已移交他人跟进：看过了，不再当新问题提示
        "noise": {},      # type -> 条数
    }
    for finding in data.get("findings") or []:
        ftype = finding.get("type") or ""
        severity = finding.get("severity") or ""
        sig = signature(task, finding)
        entry = {
            "severity": severity,
            "type": ftype,
            "node": finding.get("node"),
            "message": (finding.get("message") or "").strip(),
            "evidence": finding.get("evidence") or {},
        }

        # ① 已登记为某条缺陷 —— 不论 type/级别，都是那条缺陷又复现了一次
        hit = _match(sig, known)
        if hit:
            entry["bug_ids"] = known[hit]
            result["reported"].append(entry)
            continue
        # ② 已看过并移交他人跟进 —— 仍是候选，但不再当新问题提示
        hit = _match(sig, acked or {})
        if hit:
            entry["owner"] = (acked or {})[hit]
            result["acked"].append(entry)
            continue
        # ③ 已登记为噪音 —— 人工确认过的脚本侧问题，闭嘴
        if _match(sig, noise):
            result["noise"][ftype] = result["noise"].get(ftype, 0) + 1
            continue
        # ④ 产品缺陷候选
        if ftype in CANDIDATE_TYPES or (ftype in LEVEL_GATED_TYPES and severity in CANDIDATE_SEVERITIES):
            result["new"].append(entry)
            continue
        # ⑤ 工具侧噪音（按 type 归类）
        if ftype in NOISE_TYPES:
            result["noise"][ftype] = result["noise"].get(ftype, 0) + 1
            continue
        # ⑥ 剩下的（warning/info 级异常分支）既不敢当缺陷也不敢当噪音，单列
        result["minor"].append(entry)
    return result


def _duration(data: Dict) -> str:
    try:
        delta = datetime.fromisoformat(data["finished_at"]) - datetime.fromisoformat(data["started_at"])
        seconds = int(delta.total_seconds())
        return f"{seconds // 60}m{seconds % 60:02d}s"
    except (KeyError, TypeError, ValueError):
        return "—"


def find_runs(findings_root: Path) -> List[Path]:
    return sorted(
        (p.parent for p in findings_root.glob("*/*/*/report.json")),
        key=lambda d: (d.parents[1].name, d.name),
    )


def format_run(result: Dict, verbose: bool = True) -> str:
    """把一轮的分诊结果排成给人（和智能体）看的几行。"""
    rel = "/".join(result["run_dir"].parts[-3:])
    lines = [
        f"【本轮体检】{result['task']} @ {rel} — {result['status']}，{result['duration']}"
    ]
    if result["new"]:
        lines.append(f"  待分诊（新签名）{len(result['new'])} 条：")
        for item in result["new"][:6]:
            node = f" {item['node']}" if item["node"] else ""
            msg = item["message"].replace("\n", " ")[:110]
            lines.append(f"    ! [{item['severity']}] {item['type']}{node} — {msg}")
        if len(result["new"]) > 6:
            lines.append(f"    …… 另有 {len(result['new']) - 6} 条")
    if result["reported"]:
        counts: Dict[str, int] = {}
        for item in result["reported"]:
            for bug_id in item["bug_ids"]:
                counts[bug_id] = counts.get(bug_id, 0) + 1
        detail = "、".join(f"{bug}×{n}" for bug, n in sorted(counts.items()))
        lines.append(f"  已报缺陷复现：{detail} —— 更新 bugs.json 的 repro_rate 后重跑生成器即可")
    if result["acked"]:
        owners = "、".join(sorted({i.get("owner", "已移交") for i in result["acked"]}))
        lines.append(f"  已移交跟进 {len(result['acked'])} 条（{owners}）——本报告不收，不再提示")
    if result["minor"]:
        lines.append(f"  次要待看（warning 级异常分支，可能是缺陷也可能是脚本适配）{len(result['minor'])} 条：")
        for item in result["minor"][:5]:
            node = f" {item['node']}" if item["node"] else ""
            msg = item["message"].replace("\n", " ")[:100]
            lines.append(f"    ? [{item['severity']}] {item['type']}{node} — {msg}")
        if len(result["minor"]) > 5:
            lines.append(f"    …… 另有 {len(result['minor']) - 5} 条")
    if result["noise"] and verbose:
        detail = " ".join(f"{k}×{v}" for k, v in sorted(result["noise"].items(), key=lambda kv: -kv[1]))
        lines.append(f"  工具侧噪音 {sum(result['noise'].values())} 条：{detail}（不进缺陷报告，去加固任务 JSON）")
    if result["new"]:
        lines.append(
            "  下一步：按 .claude/skills/smoke-report/SKILL.md 分诊这些新签名"
            "（判据：人工手动照做也会发生吗），确认是缺陷就写进 bugs.json 并补 signatures，再跑 build_report.py。"
        )
    elif result["minor"]:
        lines.append(
            "  下一步：无高优候选，扫一眼上面的次要项——确认是脚本适配问题就写进 bugs.json 的 "
            "meta.known_noise（下次自动闭嘴），是缺陷就当新缺陷处理。"
        )
    elif not result["reported"]:
        lines.append("  本轮干净：无产品缺陷候选，不需要动报告。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 钩子模式


def hook_mode(project_root: Path, bugs_path: Path, findings_root: Path, event: str) -> int:
    """PostToolUse：跑完一轮就地体检。失败一律静默，不打断跑测。"""
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    if event == "PostToolUse":
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        tool = payload.get("tool_name") or ""
        if "run_task" not in tool and "start_task" not in tool:
            return 0

    tables = load_known(bugs_path)
    runs = find_runs(findings_root)
    if not runs:
        return 0

    if event == "PostToolUse":
        result = triage_run(runs[-1], *tables)
        if not result:
            return 0
        context = format_run(result)
    else:  # SessionStart：只在有待分诊轮次时发声
        pending = []
        for run in runs[-30:]:
            result = triage_run(run, *tables)
            if result and result["new"]:
                pending.append(result)
        if not pending:
            return 0
        head = f"【冒烟分诊】有 {len(pending)} 轮回放存在未分诊的产品缺陷候选："
        body = "\n".join(
            f"  - {r['task']} @ {'/'.join(r['run_dir'].parts[-3:])}：{len(r['new'])} 条新签名"
            for r in pending[-8:]
        )
        context = head + "\n" + body + "\n  需要出/更新缺陷报告时走 /smoke-report。"

    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))
    return 0


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="回放轮次分诊：findings -> 待分诊 / 已报复现 / 工具噪音")
    parser.add_argument("--run", help="分诊指定 run 目录")
    parser.add_argument("--latest", nargs="?", type=int, const=1, help="分诊最近 N 轮（默认 1）")
    parser.add_argument("--pending", action="store_true", help="列出所有含未分诊候选的轮次")
    parser.add_argument("--hook", action="store_true", help="PostToolUse 钩子模式")
    parser.add_argument("--session-hook", action="store_true", help="SessionStart 钩子模式")
    parser.add_argument("--bugs", default=DEFAULT_BUGS)
    parser.add_argument("--findings", default=DEFAULT_FINDINGS)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()

    _reconfigure()
    project_root = Path(args.project_root).resolve()
    bugs_path = (project_root / args.bugs).resolve()
    findings_root = (project_root / args.findings).resolve()

    if args.hook or args.session_hook:
        try:
            return hook_mode(
                project_root, bugs_path, findings_root,
                "PostToolUse" if args.hook else "SessionStart",
            )
        except Exception:
            return 0  # 钩子永不打断跑测

    tables = load_known(bugs_path)
    if not findings_root.is_dir():
        print(f"[错误] findings 目录不存在：{findings_root}", file=sys.stderr)
        return 1

    if args.run:
        results = [triage_run(Path(args.run).resolve(), *tables)]
    elif args.pending:
        results = [r for r in (triage_run(d, *tables) for d in find_runs(findings_root)) if r and r["new"]]
        if not results:
            print("[OK] 没有待分诊的轮次：所有产品缺陷候选都已在 bugs.json 里登记。")
            return 0
        print(f"共 {len(results)} 轮存在未分诊的候选：\n")
    else:
        count = args.latest or 1
        results = [triage_run(d, *tables) for d in find_runs(findings_root)[-count:]]

    for result in results:
        if result:
            print(format_run(result))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
