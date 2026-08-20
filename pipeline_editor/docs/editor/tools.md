# 工具页

顶栏「工具」页是四件套编辑辅助层：**录制会话 → 草稿**、**任务健康度**、**交接固化**、
**回放缓存**，本页逐个说明。

以「任务健康度」Tab 为例：它按 任务 / 节点 聚合 node_stats，
列出见于运行、直接命中、超时恢复、弹窗协助、漂移与 fallback 率。

## 从 AutoPlayQA CLI 迁移的编辑辅助层

- **录制会话 → 草稿**：`outputs/agent_sessions/` 的录制（MCP
  `record_actions_start/stop`）经 `task_editor.action_log_to_draft` 确定性
  转成识别驱动草稿（锚点步骤 → ui_text/ocr + target:recognized，盲点坐标
  标红），逐步预览带截图，命名保存后直接进编辑器补 QA 断言。此函数此前
  在产品里没有任何调用入口，这里是它的首个 UI。
- **任务健康度**：`anchor_health.scan_health` 聚合 node_stats，
  fallback_rate 高 = 锚点腐化嫌疑。
- **交接固化**：`handoff_stats.scan_handoffs`，`solidify_candidate` 节点
  一键取最近交接录制生成草稿（agent 节点 → 确定性节点链）。
- **回放缓存**：状态查看 + 清除（清除后缓存重建前不上报 anchor_drift）。
- 编辑器工具条新增**重排步号**（`write_step_labels` 整文件重写回磁盘，
  有未保存修改时禁用，成功后自动重载）。

配套 agent 技能：仓库里的 `.claude/skills/edit-task/SKILL.md`
（安全编辑协议 / 改名级联清单 / 数据驱动维护手册；与 author-task 分工
为"改旧 vs 写新"）。
