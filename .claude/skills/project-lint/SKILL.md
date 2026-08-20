---
name: project-lint
description: 数据驱动的项目语义 linter——抓 ruff/mypy 查不到的 AutoPlayQA 项目级违规（库模块 print、测试直连 subprocess、LLM 依赖、裸 except）。PostToolUse 自动跑，也可手动跑。
---

# project-lint

## 解决什么

ruff/mypy 管通用规范；本 skill 管**项目语义**——「在这个项目里不该这么写」的违规：

| 规则 id | 抓什么 |
| --- | --- |
| `bare-except` | 裸 `except:` |
| `print-in-library` | 库模块（core/perception/task/...）里用 `print`（应走 core.logger） |
| `subprocess-in-tests` | 测试直连 subprocess（应 mock，测试不依赖真机） |
| `llm-dependency` | 引入 LLM SDK（本项目定位无 LLM 依赖） |

## 结构（数据与逻辑分离）

- `rules.json` — 规则数据。**新增规则只改这里**，四层过滤降误报：
  `path_contains`/`file_context`(文件级) → `pattern`(行级) → `exclude_patterns`(排除) → `confirm_patterns`(二次确认)。
- `lint.py` — 通用引擎，不随规则变化。**成功静默、失败冗余**（违规给 行号+原因+修复+引用，exit 2 反馈给 Agent 自纠）。

## 调用方式

1. **Hook 模式**（在 `.claude/settings.json` 注册 PostToolUse）：编辑 `.py` 后自动跑。注意 settings.json 是本地配置、不随仓库分发，仓库迁到新机器后需自行注册该 hook。
2. **CLI 模式**（测试/手动）：
   ```bash
   <python> .claude/skills/project-lint/lint.py <file.py>
   ```
   `lint.py` 只依赖 Python 标准库，`<python>` 用任意 Python 3 解释器即可（本仓库开发机习惯用项目环境解释器，路径记在本地 CLAUDE.md，不入库）。

## 新增规则流程（配合 /learn）

1. 教训能用正则行级检测 → 在 `rules.json` 加一条（id/rule/pattern/exclude/violation_tpl/fix/ref）。
2. 构造正例（应报）与反例（不应报）各一，CLI 模式验证。
3. 对现有代码全量跑一遍确认无误报（新规则不应让现状报红）。
