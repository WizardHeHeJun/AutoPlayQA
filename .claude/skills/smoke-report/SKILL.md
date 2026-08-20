---
name: smoke-report
description: 冒烟汇报固定流程：把若干轮自动化回放的 findings 分诊成产品缺陷，抽证据成包，生成固定版式的 HTML + Markdown 缺陷报告（含回放执行汇总）。当用户说"出一份冒烟报告"、"把这批 findings 整理成缺陷报告"、"给开发发缺陷"、"冒烟汇总"、"更新报告"时使用。
---

# 冒烟汇报（smoke-report）

目标：把 `outputs/findings/` 里一堆机器 finding，变成**开发/策划能直接照做复现、不用回问测试**的缺陷报告，且每次产出的口径与版式完全一致。

**分工铁律**：你只做判断，机械活全部交给脚本。

| 你负责（写进 `bugs.json`） | 脚本负责（`build_report.py`） |
| --- | --- |
| 哪条 finding 是真产品缺陷 | 扫 findings 统计回放轮次/状态/时长 |
| 严重级别、归属、根因读法 | 按缺陷清单抽证据文件成包 |
| 人工可照做的复现步骤 | 渲染 HTML（自包含离线）+ Markdown |
| 哪些断言没有文件证据 | 校验所有证据引用，断链即失败 |

**统计数字一律不许手填**——轮次、error/warning 数、平均时长都由脚本从落盘 `report.json` 算，手填的数字迟早和证据打架。

## 运行环境

本技能的两个脚本（`triage.py` / `build_report.py`）**只依赖 Python 标准库**，从项目根目录运行。下文命令里的 `<python>` 指 Python 3 解释器：

- 随仓库分发/迁移到新机器：无需安装任何依赖，任意 Python 3（PATH 里的 `python` 亦可）；项目整体环境搭建见 README「环境」节。
- 本仓库开发机：用项目 conda 环境的解释器（路径记在本地 CLAUDE.md——该文件 gitignore 不入库）。

不要把某台机器的解释器绝对路径写进本目录的入库文件——机器路径只记在本地 CLAUDE.md / 本地配置里（均不随仓库分发）。

另注：下节提到的 PostToolUse / SessionStart 自动体检钩子注册在本地 `.claude/settings.json`（不随仓库分发）。仓库迁到新机器后钩子不会自动生效——要么自行注册，要么每轮跑完手动执行 `triage.py --latest` 等价触发。

## 每轮跑完的固定动作（触发端）

**每跑完一轮真机回放，先做体检，再决定动不动报告。** 这一步由 `triage.py` 自动触发：

| 触发 | 时机 | 行为 |
| --- | --- | --- |
| PostToolUse 钩子 | MCP `run_task` / `start_task` 一返回 | 自动体检刚跑完那轮，结果直接注入上下文 |
| SessionStart 钩子 | 每次会话开始 | 只在有**未分诊候选**时发声（覆盖用户用 CLI 自己跑的轮次） |
| 手动 | 任何时候 | `triage.py --latest [N]` / `--run <目录>` / `--pending` |

分诊器把 findings 分成四堆（机器只分类，判断仍归你）：

| 堆 | 内容 | 你要做的 |
| --- | --- | --- |
| **待分诊（新签名）** | crash/ANR/watchdog 命中，或 error 级异常分支，且签名不认识 | 按下面四步流程分诊；确认是缺陷就进报告 |
| **已报缺陷复现** | 签名命中 `bugs.json` 里某条缺陷 | 只更新该缺陷的 `repro_rate`，重跑生成器；**不要新开一条** |
| **次要待看** | warning/info 级异常分支，既不敢当缺陷也不敢当噪音 | 扫一眼：是脚本适配就登记进 `known_noise`，是缺陷就当新缺陷办 |
| **工具侧噪音** | `timeout_recovery` / `anchor_drift` / `anchor_rot_suspect` / `unknown_popup_backoff`，或已登记的噪音签名 | 不进报告，去加固任务 JSON |

**签名** = `(task, type, node)` 三元组，跨 run 稳定，是"同一个 bug 第 N 次复现"与"新问题"的分界线。三张签名表，语义各不相同，**别混用**：

| 表 | 位置 | 含义 | 命中后 |
| --- | --- | --- | --- |
| `signatures` | `bugs[].signatures` | 已报缺陷的指纹 | 记作"又复现一次"，更新 `repro_rate` |
| `known_noise` | `meta.known_noise` | 判定为**脚本侧问题**，不是缺陷 | 计入噪音，闭嘴 |
| `acknowledged` | `meta.acknowledged` | **是**产品缺陷候选，但已看过、移交他人跟进 | 显示"已移交（owner）"，不再当新问题提示 |

一条缺陷可挂多个签名，一个签名也可对应多条缺陷（同现象拆两条时）。`node` 省略 = 通配该 `(task, type)`；`type` 参与匹配，所以对 `anomaly_node` 消音不会影响同名节点的 `watchdog` 命中。

⚠️ **这两张消音表都别拿来压真缺陷**：
- `known_noise` 通配写太宽会连真症状一起吞掉——曾用 `smoke_test/anomaly_node` 通配，把「登录后 90 秒未进主场景」一并压掉。宁可逐个 node 登记。
- `acknowledged` 是"移交记录"不是"结案"：每条要写清 `owner`、`since` 和 `why`（这现象到底是什么），别用它给自己该处理的缺陷开脱。

体检说"本轮干净"就到此为止，不用动报告；有新签名才进入下面的流程。

## 四步流程

### ① 定范围

跟用户确认三件事，别自己假设：报告**周期**（`meta.period`，闭区间日期）、**收哪些缺陷**、**交付给谁**（决定语气与详略）。

先看数据全貌，不要一上来读截图：

```powershell
<python> .claude\skills\smoke-report\build_report.py outputs\bug_reports\bugs.json --stats-only
```

（还没有 `bugs.json` 时，先把本技能目录的 `bugs.template.json` 拷到 `outputs/bug_reports/bugs.json` 改 `meta` 即可跑 `--stats-only`。）

⚠️ 周期内如果还在继续跑任务，统计会随之变化——**跑完再出报告**，或把 `period` 收窄到已结束的日期。

### ② 分诊：产品缺陷 vs 工具噪音

逐条过 finding，一句话判据：

> **这个现象，人工手动照同样步骤操作时也会发生吗？**
> 会 → 产品缺陷；只在脚本回放时出现 → 自动化脚本自身问题（不进报告，去加固任务 JSON）。

| 典型产品缺陷 | 典型工具噪音（不进报告） |
| --- | --- |
| 服务端报错 / RPC error code | 按钮 OCR 未命中、锚点漂移（`anchor_drift`） |
| 崩溃 / ANR（logcat FATAL） | 造数据内容不固定导致「第 N 项不存在」 |
| 文案穿帮（未替换占位符、错别字） | 节点识别超时后走 `on_timeout` 恢复成功 |
| 功能缺失（承诺的按钮/奖励不存在） | 分辨率/机型导致的 ROI 偏移 |
| 数值/状态错误、UI 错位遮挡 | 脚本自身写错锚点、点错位置 |

两条容易搞反的：

- **`error` 级 ≠ 缺陷，`warning` 级 ≠ 没事**。watchdog 命中（负向断言）多半是真缺陷，哪怕记的是 warning；而 `error` 常常只是脚本跑挂了。
- **一个现象可能是两个缺陷**。典型：服务端接口报错导致功能发不起来（S1）+ 客户端拿到错误后不给玩家任何提示（S2）要**分开报**——因为服务端修好后第二条仍会复现。判断标准：修了 A 之后 B 还在不在？在 → 拆。

### ③ 回到证据文件核实（最容易翻车的一步）

**执行期的口述记忆不是证据。** 每条要写进报告的断言，必须能在落盘文件里指出行号/截图：

```powershell
# logcat 里核错误码，别凭印象写
Select-String -Path outputs\findings\<日期>\<设备>\<run>\*_logcat.log -Pattern "1001|ServerInternal|FATAL"
```

已知的两个坑（真实教训）：

1. **`report.json` 里 finding 的 `message` 是执行期人工填的推断**，可能与同目录的 logcat 证据矛盾（真实教训：`message` 里把某个业务错误码写成根因，而落盘 logcat 里通篇是另一个通用服务端错误码）。**以文件为准**，冲突时两个都写、分别标来源。
2. **logcat 片段是有采样窗口的**，可能没覆盖问题发生的时刻。窗口没覆盖就写「该轮无日志证据，只有界面证据」，不要拿别轮的日志顶上。

核不实的断言不许删掉，标成第三态写进 `meta.unverified`，报告会自动汇总成附录 C：

| `source` | 报告里显示 | 用于 |
| --- | --- | --- |
| `file` | 有文件证据 | 有落盘文件可核对 |
| `observed` | 未捕获到直接证据，为执行期观察 | 人看到了但没留下文件 |
| `inferred` | 为推断 | 由时间相关性等间接推出 |

### ④ 写 bugs.json → 生成 → 自检

```powershell
# 校验（不写文件，看字段和证据引用是否齐）
<python> .claude\skills\smoke-report\build_report.py outputs\bug_reports\bugs.json --validate

# 正式生成（证据抽包 + HTML + MD，自动校验断链）
<python> .claude\skills\smoke-report\build_report.py outputs\bug_reports\bugs.json
```

产物固定落在 `bugs.json` 同级目录，整个目录可打包外发：

```text
outputs/bug_reports/
├── <basename>.html    交付主件（自包含、双击打开、深浅色自适应、可打印）
├── <basename>.md      同内容 Markdown（贴飞书/工单用）
├── bugs.json          数据源，改完重跑即可更新报告
└── evidence/<BUG-ID>/<run_id>/...
```

改一条描述、加一条缺陷、周期变了 → **改 bugs.json 重跑**，不要手改 HTML（手改的下次就被覆盖）。

## bugs.json 契约

顶层 `{"meta": {...}, "bugs": [...]}`。空模板见本技能目录 `bugs.template.json`——拷到 `outputs/bug_reports/bugs.json` 后按下表填。模板里每个字段都带一句用途说明，照着填即可；填过一轮的 `bugs.json` 本身就是下一轮最好的范例（它落在 `outputs/`，不入库）。

**meta 关键字段**

| 字段 | 说明 |
| --- | --- |
| `title` `basename` `date` | 必填。`basename` 决定产物文件名 |
| `period` | `["起","止"]` 日期闭区间，决定统计哪些回放 |
| `env` | 页眉环境 chips（服务器/账号/版本/设备…） |
| `task_labels` | 用例脚本 → 中文模块名，汇总表用 |
| `summary_cards` | 执行摘要里的卡片（结论、最需处理的事） |
| `unverified` | 无文件证据的断言，渲染成附录 C |

**bug 关键字段**

| 字段 | 说明 |
| --- | --- |
| `id` `module` `title` `severity` | 必填。severity ∈ S1/S2/S3/S4 |
| `steps` `evidence` | 必填——**没有复现步骤和证据的缺陷不许进报告** |
| `tasks` | 该缺陷来自哪些用例脚本，汇总表据此挂徽标 |
| `runs` | `{key: {path, include, exclude}}`，证据来源 run 目录 + 抽哪些文件（glob） |
| `env` | 数组（逐项标 source）或一句话字符串（写"同 BUG-00X"） |
| `shots` | 正文内联的截图/录屏 `{run, file, title, caption}` |
| `severity_note` `sections` `footnotes` | 可选：待确认项、换对手情况、体积裁剪说明等 |

**内容块**（`actual` / `expected` / 卡片等自由段落）用块数组表达，HTML 与 MD 共用：
`{"p": "段落"}`、`{"ul": [...]}`、`{"ol": [...]}`、`{"code": "日志原文"}`、`{"note": "警示框"}`、`{"quote": "引述"}`、`{"caption": "小字注"}`。
行内支持 `**粗体**`、`` `代码` ``、`[文字](路径)`，其余一律转义（游戏文案和日志里全是 `<>&`）。

**证据引用**两种写法：`{"run": "main", "file": "01_anomaly_node.png"}`（从该 run 抽）或 `{"ref": "evidence/BUG-001/.../ga_rec_1.mp4"}`（复用别的缺陷已抽进包的文件，避免几十 MB 录屏重复占空间）。引用的文件必须在该 run 的 `include` 范围内，否则校验直接失败。

**体积控制**：`video/` 动辄上百 MB，`include` 里点名具体 mp4（`video/ga_rec_0.mp4`），别写 `video/*`。整包控制在 ~150MB 以内。

## 严重级别

| 级别 | 判据 | 例 |
| --- | --- | --- |
| **S1 致命** | 核心玩法完全不可用 / 崩溃 / 卡死 / 数据丢失，下游功能被阻塞无法测试 | 某核心玩法 100% 进不去 |
| **S2 严重** | 主要功能异常但有替代路径；**通用错误处理缺失**；承诺未兑现（奖励领不到） | 报错后客户端零反馈 |
| **S3 一般** | 不阻塞功能的体验/文案问题，玩家可见 | 未替换的 `${占位符}` |
| **S4 轻微** | 细微瑕疵，玩家基本不可感知 | 1px 错位 |

定级不确定时**就高不就低**，并在 `severity_note` 写明「待确认 X 后可能调整」，`sections` 里说清要确认什么。玩家直接可见的品质问题，注明「对外发布前须升级」。

## 检查清单

- [ ] 统计数字全部来自脚本，报告里没有手写的轮次/条数。
- [ ] 每条缺陷都有人工可照做的 `steps`，不含任何本工具专有名词（节点名、锚点、ROI）。
- [ ] 每条断言要么有文件证据，要么标进 `unverified`——没有第三种状态。
- [ ] finding 的 `message` 与 logcat 冲突时，以 logcat 为准并写明差异。
- [ ] 一个现象拆成了几个独立缺陷（修了 A 之后 B 还在 → 拆）。
- [ ] 工具噪音（OCR 未命中/锚点漂移/造数据不固定）没混进缺陷，但在汇总表下方有说明。
- [ ] `--validate` 通过、正式生成无断链、`evidence/` 体积可控。
- [ ] 新进报告的缺陷补了 `signatures`，确认的脚本问题补了 `known_noise`——否则下轮体检会把它们当新问题重报一遍。
