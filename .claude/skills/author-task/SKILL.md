---
name: author-task
description: 编写任务流程 JSON：把一段操作流程组织成识别门控状态机任务（recognition→action→next），选对识别通道、加 QA 断言（watchdog/finding/on_finding/popups）、回放迭代加固到稳定通过。当用户说"帮我写个任务"、"怎么写任务 JSON"、"把这个流程写成任务"、"手写任务流程"、"探路生成任务"、"加固这个任务"时使用。若每一步点哪/锚点是什么还说不清，需要先观察用户真机操作采集，配合 live-record 技能。
---

# 编写任务流程 JSON（author-task）

目标：把一段操作流程写成 `task/task_definitions/*.json` 里的**识别门控状态机**——每个节点先识别确认到达预期界面、命中才动作，再轮询 `next` 跳转——并按本项目"异常即发现"的定位加上 QA 断言，最后回放迭代到稳定通过。

**字段语法以 MCP `get_task_schema`（即 `action/action_schema.py` 的 TASK_SCHEMA_DOC）为准**；本技能讲的是 schema 之外的**判断**：通道怎么选、断言加哪些、怎么迭代加固。

## 与 live-record 的分工（两个技能配合用）

- **`live-record` = 采集**：用户在真机上手动演示，你经 MCP 同步观察，得到每一步的"操作前画面 / 锚点文本 / 点击坐标"。
- **`author-task`（本技能）= 编写**：把意图或采集到的观察，组织成结构化、带 QA 断言、回放稳定的任务 JSON。与节点从哪来无关——观察录制、智能体探路、纯手写都用本技能收口。

什么时候先去 `live-record`：用户会操作但**说不清每一步点哪、锚点是什么**，或流程长 / 有滑动手势。采集回来后回到本技能组织 JSON。
什么时候直接用本技能：用户能用语言说清目标（你 `screenshot`/`ui_dump`/`find_text` 自己探路确认锚点），或在改写 / 加固一个已有任务。

## 1. 骨架：识别门控状态机

```json
{
  "entry": "<起始节点>",
  "nodes": {
    "<节点名>": {
      "recognition": { "type": "...", "...": "..." },
      "action": { "type": "...", "...": "..." },
      "next": ["<候选节点>", "..."],
      "on_timeout": "<识别超时的恢复节点>"
    }
  }
}
```

- `entry` 是入口节点；`next: []` 表示任务正常结束。
- `next` 是**候选列表**：动作后轮询，谁先识别命中走谁——天然支持弹窗 / 多结局分支（把正常结局和异常弹窗都列进 `next`）。
- 引擎逐节点：识别（门控）→ 命中才执行动作 → 轮询 `next`。识别超时走该节点的 `on_timeout`，没有就判失败。

## 2. 选识别通道（这是写任务最关键的判断）

| 通道 | 用在哪 | 关键字段 |
|---|---|---|
| `ui_text` | 系统界面 / 有 uiautomator dump 节点的原生 UI（优先） | `expected` |
| `ocr` | 游戏单 Surface 渲染、dump 取不到节点的文字 | `expected`、`roi`（缩小范围提速/防误命中） |
| `template` | 纯贴图图标 / 建筑等没有文字的元素 | `template`（用 MCP `capture_template` 先截图存到 `task/templates/`）、`scales`、`grayscale` |
| `feature` | 纹理丰富、**常被小改版**（换皮/微缩放）的贴图锚点——ORB 特征匹配抗形变；纯色/低纹理图标仍用 `template` | `template`、`min_matches`（默认 4）、`ratio`（默认 0.75） |
| `yolo` | 形变 / 遮挡严重、模板匹配失效的目标（需接入方训练好的模型放进 `task/models/`） | `label`、`conf` |
| `blank_screen` | loading / 黑屏的**等待**或**断言**（帧接近纯色才命中） | `threshold`（灰度 stddev 上限） |
| `scene` | **"我现在在哪个界面"**——整屏场景分类（主界面 / 关卡内 / 弹窗 / GM 面板…）。用在跑飞后确认位置、异常分支断言（如 watchdog `expected: "popup.crash"`），不是用来找可点元素（不返回坐标） | `expected`（场景标签，**点号前缀匹配**：`"popup"` 命中 `popup.error`，`"menu"` 命中 `menu.settings`）、`min_conf`（默认 0.5）。**标签集由接入项目提供**（见下方硬约定）；当前生效清单看 MCP `classify_scene` 返回的 `taxonomy`；`unknown` 永不命中，认不出就走 `on_timeout` |
| `and` | 单通道会误命中、要多条件叠加才算到对界面（如图标 **且** 旁边文字） | `all_of`（子识别列表，全命中才算命中）、`box_index`（用第几个子命中的框做 `target: recognized`，默认 0） |
| `or` | 同一界面有多种等价特征、命中任一即可（如"确定"或"OK"） | `any_of`（子识别列表，按序试，首个命中即走，用它的框） |
| `always` | 确认页等"到了就该动手、无需识别特征"的节点 | 无 |

**硬约定（scene）**：scene 只做「我在哪」断言（起点/收尾/分支确认），不做锚点定位——不产坐标（`center` 恒 `None`，`action` 配 `target: "recognized"` 会在运行期抛 ValueError，`task_lint` W007 会拦），比 `ocr` 贵 5~14 倍；scene 节点 `timeout_ms` 至少 60000（首个 scene 节点还要吃 onnxruntime 冷启动，更紧）。

**标签体系是可插拔的**：框架**只内置一个场景标签 `blank`**（近黑 / 息屏 / 空帧），外加非场景信号 `other_app` 与永不猜的 `unknown`——除此之外的每一个标签都由**接入的游戏项目**注册：

```python
from perception.scene_classifier import register_scene_probe

register_scene_probe("menu.settings", probe_fn, description="设置页", order=100.0)
```

配套还有 `unregister_scene_probe` / `clear_scene_probes` / `registered_scene_probes`（`order` 小的先判，同一帧首个命中的探针胜出）。写任务前**先确认目标标签已注册**（`classify_scene` 的 `taxonomy` 里有）；没注册的标签写进 `expected` 只会永远读到 `unknown`，节点直接走 `on_timeout`。

经验：游戏内优先 `ocr`+`roi`，避开 `ui_text`（dump 在单 Surface 上常为空，且 dump 比 OCR 慢得多）。锚点选**稳定、唯一**的文字；易变的数字/昵称别当锚点。

组合识别（`and`/`or`）的诀窍：一次评估**共享同一帧**，子识别不会各抓各的画面而"看到不同瞬间"；子项里**不许放 `always`**（`and` 里是废子项、`or` 里会让整个门控恒真），加载即报错；最多嵌套 2 层。**成本提醒**：共享帧只省"截图"不省"dump"——每个 `ui_text` 子项各触发一次 uiautomator dump（~4.33s），别用 2 个以上 `ui_text` 子识别组合，需要多条件时优先 `ocr`+`roi`。子锚点若从回放缓存位置漂移，**任一子锚点漂移都会上报 `anchor_drift`**（不止 `box_index` 选中的那个）。

模板制作两个诀窍：**PNG 的 alpha 通道就是掩码**——把模板里易变的子区域（数字、头像、等级角标）抠成透明，只留稳定美术参与匹配（`template`/`feature` 都生效）；模板跨分辨率/UI 缩放时给 `template` 配 `"scales": [0.9, 1.0, 1.1]` 扫尺寸（matchTemplate 本身不抗缩放），命中的 scale 会回写在 hit 里。

## 3. 写动作

- 类型见 schema：`click / drag / input_text / wait / key / gesture / agent / none / custom`。
- **点击优先 `{"type": "click", "target": "recognized"}`**——点识别命中处，绝不写死坐标（坐标随分辨率/机型漂）。实在没有可识别锚点（纯图标且无模板）才退化为 `params` 坐标。
- 需要**智能判断**的步骤（动态手势、看情况决策）用 `agent`：引擎挂起返回 `status=agent_required`+指令文本，外部智能体完成后 `run_task(start_after=<节点>)` / CLI `task resume` 续跑。
- 等待用对字段：固定小间隔用 `post_delay_ms`；**过场/加载这种不定长等待**用节点级 `"wait_still": {"timeout_ms":5000,"interval_ms":200,"threshold":0.01}`——画面连续两帧不动才继续，超时只是停等不判失败。
- **连打/QTE 用动作级 `repeat`**（params）：`{"repeat": 8, "repeat_delay_ms": 0, "repeat_wait_freezes_ms": 0}`——一个节点内连发 N 次，中间不重跑识别（节点之间那点空隙正是吞掉亚秒 QTE 窗口的元凶）。要"打一下等画面响应再打"就把 `repeat_wait_freezes_ms` 设成等待上限（与 wait_still 同语义，超时只停等不判失败）。只能挂在 click/drag/input_text/key/gesture 上；某发失败不中止，按最后一发判成败。
- **全局调参写任务级 `defaults`**：`{"defaults": {"timeout_ms": 15000, "poll_interval_ms": 500, "post_delay_ms": 300, "wait_still": {...}}}` 给所有节点垫默认值（含 includes 进来的节点），节点自己写的字段优先，节点写 `null` 即退回引擎默认。白名单只有这四个键，写错键加载报错。
- 多步**确定性**逻辑（无需判断）写成 `custom`：内置 `launch_app / gm_command / ensure_checkbox / set_text_field / swipe_until`（见 `task/custom_actions/builtins.py`）；不够用就 `@register("名字")` 加 handler——新建 `task/custom_actions/<模块>.py` 放进去即可，包会自动发现并 import（无需改 `__init__.py`；模块 import 失败会直接报错，不静默跳过）。例：列表里滑到目标出现用 `swipe_until`，别用一堆固定 `drag`。

## 4. 加 QA 断言（本项目的灵魂：异常即发现，别静默绕过）

写任务**不是**让它跑通就行——要让它在出问题时**上报留证**。每个任务都应考虑这四件事：

- **节点 `finding`**：异常分支节点（如"战斗失败弹窗""错误对话框"）一进去就自我上报。值为字符串或 `{"severity": "warning|error|critical", "message": "..."}`。把这种节点列进相关步骤的 `next`，让它和正常结局赛跑。
- **任务级 `watchdogs`**：负向断言，每步动作后 + 识别超时时都检查。命中即记 finding。
  - `skip_to: <节点>` → 命中后跳到恢复节点**继续测**（上报后跳过，bug-skip），优先级最高，压过 `fail_task`。
  - `fail_task: true` → 命中即中止任务。
  - 用于"任何界面都不该出现"的东西：报错 toast、`网络错误`、`blank_screen` 黑屏卡死等。
- **任务级 `on_finding: <节点>`**：全局兜底恢复目标。覆盖 **logcat 崩溃 / ANR** 这类没有对应 watchdog 的 bug（崩溃由 LogcatMonitor 自动检测）。
- **`popups` 白名单**：仅列**已知良性**弹窗（用户协议、游戏内告警等预期噪音）及其消除动作（`click`/`key`/`gesture`）。卡住时自动扫除、**不记 finding**。没把握是不是 bug 的弹窗**别**列进来——让它卡成 timeout/watchdog 发现。

bug-skip 铁律：**只有"新记下一条 finding"才会跳转**；纯识别超时（卡顿、无 bug）绝不跳，那是 `on_timeout` 的活。具象例子见 README「上报后跳过（bug-skip）」节。

## 5. 分支与恢复

- **分支**：把所有可能的下一画面（正常 + 弹窗 + 异常）都列进 `next`，靠各自的 recognition 区分。
- **恢复**：给关键节点配 `on_timeout` 回退到一个能重新定位的稳定节点（如主界面），避免一步识别失败就整任务失败。
- 别把"检测到 bug 后改道"写成 `on_timeout`——那是 watchdog `skip_to` / `on_finding` 的职责；`on_timeout` 只管卡顿恢复。

## 6. 复用与封装

- **用例必须以同一个主场景（游戏主界面）为起点与终点**（套件连跑的前提）：起点节点 `用例开始` 要真门控主场景（`ocr` 主界面标志文字 + 底栏 roi，配 `on_timeout` 记 warning finding 再兜底），**不许写成 `{"type":"always"}` 闭眼开跑**；收尾也要确认回到主场景。满足这条，用例才能被 `task suite` 以 `start_after="主场景确认"` 跳过冷启动+登录连跑（每例实测省约 55s，boot 54.8s），见 `task/suite_runner.py`。
- **`includes`**：见下面 6.1 小节。
- **`custom`**：见第 3 节，把确定性多步逻辑沉淀成可复用 handler。

### 6.1 公共片段与 includes

**何时抽片段**：同一段节点链在 ≥2 个任务里逐字重复，且它本身不是被测对象——冷启动/登录/落地确认（`common/boot_to_home.json`）、GM 铺垫（`common/gm_boot.json`）、通用弹窗处理、回主界面。**别抽**：只有一个任务用的节点（多一跳查找成本）、用例正文（那是被测流程，藏进片段等于把测试意图藏起来）。

**怎么组织**：片段放 `task/task_definitions/` 下的子目录（现有约定 `common/`，按域再分如 `common/<模块>_*.json` 也行），任务里 `"includes": ["common/boot_to_home.json"]`。路径**相对 `task_definitions/`**，绝对路径和逃出该目录的 `..` 直接加载报错。

**片段文件形状**——只有节点，没有任务级字段：

```json
{
  "description": "冷启动 → 登录 → 落地主场景",
  "nodes": { "启动游戏": { ... }, "主场景确认": { ... } }
}
```

`entry` / `includes`（单层，片段不能再 include）/ `watchdogs` / `popups` / `on_finding` / `defaults` 等出现在片段里 = 加载报错：片段不是能跑的任务，断言和调参属于**引它的那个任务**。`_` 开头的键（`_comment`）可自由加注释。

**合并与冲突**：片段节点并进主任务节点表，`next`/`on_timeout` **可跨文件双向引用**（片段跳回正文入口、正文跳进片段都行），合并后整体校验——任一引用解析不通，整个加载失败，不产生半合并状态。同名节点默认 `strict`：一次列全部冲突节点名 + 双方来源文件，**不覆盖不静默**。确实要在主任务里特化某个共享节点时，显式写 `"on_conflict": "overwrite"`（后合并者胜，主任务最后合并；`common/gm_boot.json` 就是靠这个把「登录页点 GM 徽标」插进 boot 链）。

**保存与查看**：`save_task` 原样保留 `includes`，**不把片段内联进主文件**——改片段即刻对所有引用它的任务生效。`get_task` 返回合并后的视图并附 `_merge.include_map`（节点名 → 来源文件，`"<task>"` 表示主文件），要分清某个节点是哪来的看它。引了片段却一个节点都不可达 → lint **W006**。

## 7. 回放迭代工作流（写完不算完）

1. `get_task_schema` 对一遍字段格式。
2. `save_task`（CLI `task save`）——过加载校验（`entry`/`next`/`on_timeout`/`skip_to`/`on_finding`/custom 引用都得解析得通，原子校验）。
3. **处理 lint warnings**：`save_task` 成功后返回值带 `lint_warnings`（W001 缺 on_timeout / W002 异常分支缺 finding / W003 冷启动缺 popups / W004 有锚点却写死坐标 / W005 无任何 QA 断言 / W006 引了片段却无一节点可达）——**逐条处理或说明豁免理由**，别对着"合法但不健壮"的提示视而不见；单独查一遍用 CLI `task lint <name>`。这只是编写期提示，不阻断保存——除非 config `lint.strict: true`，此时有 warning 会直接拒绝保存（先改完/豁免再存）。
4. 回放验证：
   - 短任务：`run_task`（同步，跑完返回）。
   - 长任务 / 全流程冒烟：`start_task` 拿 `run_id` → `get_run_status(run_id)` 轮询进度（`status`/`current_node`/`elapsed_s`），别用 `run_task` 干等。
5. 看结果里的 **`findings`**——**即使任务 `completed` 也要呈现给用户**（这是 QA 工具的产出本体）。
6. 识别超时 / 走偏 → 调 `threshold`、`roi`、换更稳的锚点文本；`agent_required` → 按 `result.handoff` 完成该步后 `start_after=<节点>` 续跑。迭代到稳定通过。长流程 / 无人值守场景把这个"挂起—人工—续跑"循环交给 **`babysit-run` 技能**自动看护（它还会汇总各分段 findings 并提示哪些 agent 节点该固化）。

## 8. 性能调优（真机经验）

- 耗时大头是**截图**和 **ui_dump**，不是 OCR（截图已优化为 raw screencap / scrcpy 帧流）。
- 截图变快后，`post_delay_ms` / `poll_interval_ms` / 异常分支 `timeout_ms` 的占比才显现——这时调小它们才有效。
- 游戏内优先 `ocr`+`roi`，避开 `ui_text` dump。
- 别为 OCR 上 GPU（本就 <1s，收益微乎其微）。

## 9. 步号（可选，写完整理用）

任务是图不是列表，光从上往下读看不出执行顺序。`task renumber <name>` 按图重算并把 `step` 写回每个节点（主干 `1,2,3…`、兜底分支 `2.1`）；`task show <name>` 打印按步号排序的流程大纲；MCP `get_task` 额外返回 `_steps`/`_step_outline`。**引擎不读 `step`，纯导航/交接用**，编辑后重跑 renumber 刷新即可。

## 提交前自检清单

- [ ] 每个节点都有能确认"到了正确界面"的 `recognition`（没有滥用 `always`）？
- [ ] 点击都用 `target: "recognized"`，没写死坐标？
- [ ] 关键步骤有 `on_timeout` 恢复？分支都列进了 `next`？
- [ ] 加了 QA 断言：异常节点 `finding` + 任务级 `watchdogs` / `on_finding`？（QA 工具的硬要求）
- [ ] `popups` 只放了**确定良性**的弹窗？
- [ ] `save_task` 过校验 + `run_task` 回放通过，`findings` 已呈现给用户？
- [ ] `save_task` 返回的 `lint_warnings` 逐条处理或写清豁免理由？
- [ ] 用例起点 `用例开始` 是真门控（不是 `always`），收尾也回到主场景？（否则不能进套件连跑）
