---
description: 看护跑 — 后台起 run、轮询进度、接 agent 交接、续跑到底并汇总所有分段 findings
argument-hint: <任务名> [设备]
---

# /babysit

读 `.claude/skills/babysit-run/SKILL.md`，按其中的看护循环执行：`$ARGUMENTS`。

参数解析：第一个是任务名，第二个（可选）是 `device_id`（省略时用 `list_devices` 确认唯一在线设备，多台则问用户）。

不要跳步——起跑冲突处理、交接归档、**跨分段的 findings 汇总**、收尾的 `handoff_stats` 固化建议，都以 SKILL.md 为准。
