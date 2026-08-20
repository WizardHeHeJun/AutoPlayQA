# 测试与框架依赖

回归怎么跑，以及编辑器依赖框架（AutoPlayQA）的哪些能力。

## 测试

`<python>` = AutoPlayQA 所用环境的 Python 解释器；命令在**仓库根**执行。

```powershell
# 后端回归（无需设备）：REST+MCP 双通道 round-trip、路径穿越、乐观并发、
# custom action 参数 schema 提取
<python> -m pytest pipeline_editor\tests -q

# 框架全量回归（编辑器改动不应影响它）
<python> -m pytest tests -q

# 前端
cd pipeline_editor\frontend; npm run typecheck; npm test
```

## 编辑器依赖框架的这些能力

编辑器不改框架、也不复刻框架的任何规则，它直接 import 下面这些能力：

- `task/task_loader.py` 的 `resolve_task` —— **校验真值**。编辑器的「真值校验」
  就是拿当前 doc 干跑它，所以编辑器里永远不会出现一份和引擎不一致的校验规则。
- `task/task_lint.py` 的 `lint_task` —— 保存时随响应返回的规则性提示。
- `task/custom_actions` 注册表 —— 属性面板的 custom action 下拉与参数表单，
  参数 schema 由 `backend/action_schema_introspect.py` 从 handler 源码静态提取。
- `task/task_engine.py` 的 `TaskEngine`，含 `request_stop()` 协作式干净停止
  （节点边界 + 轮询循环两处检查点；停止时不触发 popup 扫除 / BACK 兜底），
  错误串为 `TaskEngine.STOP_ERROR` —— 运行面板的「停止」走的就是它。
- `perception/` 的截图与 OCR —— 截图取 ROI / 裁模板 / 试识别。
- 框架 `mcp_server.py` 上的编辑面薄工具（`validate_task` / `list_custom_actions` /
  `lint_saved_task` / `get_step_labels` / `list_includes`）：供外部智能体使用，
  编辑器自身不依赖；编辑器后端在 `/mcp` 另有一套同名工具（见
  [AI / MCP 协同编辑](/editor/ai-mcp)）。
