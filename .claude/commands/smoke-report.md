---
description: 冒烟汇报 — 把回放 findings 分诊成缺陷，生成固定版式的 HTML + MD 报告
---

# /smoke-report

调用技能 `.claude/skills/smoke-report/SKILL.md`，按固定流程出冒烟汇报。

用户输入（可选）：`$ARGUMENTS`（周期、要收的缺陷范围、或"更新现有报告"）。

下文命令里的 `<python>` 指 Python 3 解释器——本技能脚本只依赖标准库，任意 Python 3 均可（项目整体环境搭建见 README「环境」节；本仓库开发机的解释器路径记在本地 CLAUDE.md，该文件不入库）。

**每轮跑完的体检是自动的**：MCP `run_task` 一返回，PostToolUse 钩子就会跑 `triage.py --hook` 把结果注入上下文；用户用 CLI 自己跑的轮次由 SessionStart 钩子兜底提示。手动查：

```powershell
<python> .claude\skills\smoke-report\triage.py --latest   # 最近一轮
<python> .claude\skills\smoke-report\triage.py --pending  # 所有未分诊轮次
```

体检说"本轮干净"就不用动报告；命中已报缺陷只需更新 `repro_rate` 重跑生成器；**只有新签名才走下面的完整流程**。

执行要点（细节以 SKILL.md 为准，不要凭记忆跳步）：

1. **定范围** — 确认周期、收哪些缺陷、交付给谁；先跑 `--stats-only` 看数据全貌，别一上来读截图。
2. **分诊** — 判据是「人工手动照做也会发生吗」；`error` 级不等于缺陷，watchdog 命中的 warning 常常才是。
3. **核实** — 每条断言回到落盘文件指出行号/截图；`report.json` 的 `message` 是人工填的推断，与 logcat 冲突以 logcat 为准；核不实的写进 `meta.unverified`。
4. **生成** — 改 `bugs.json` 后跑脚本，**不要手改产出的 HTML/MD**：

```powershell
<python> .claude\skills\smoke-report\build_report.py outputs\bug_reports\bugs.json --validate
<python> .claude\skills\smoke-report\build_report.py outputs\bug_reports\bugs.json
```

最后向用户汇报：缺陷条数与分级、统计口径（几轮/几个用例）、产物路径、以及**哪些断言没有文件证据**（附录 C 的内容要在对话里点名，不能只躺在报告里）。
