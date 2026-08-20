# 套件与报告

套件把多个任务串成一次连跑，报告页回看历史 run 的 findings 与证据。

## 套件

顶栏「套件」页：左列表（显示每个套件的用例数）、中表单、右运行面板（与任务运行面板同一套）。

表单字段：`cases`（用例顺序执行，从任务列表下拉添加，行内可上下移/删除）、
`resume_after`（跳过开机链的续跑节点，必填）、`case_entry`（用例正文入口节点，必填）、
`on_case_failure`（restart_retry / restart_continue / abort）、`max_retries`、
`full_boot_cases`（必须强制冷启动的用例，从 cases 里多选）、
`landing`（用例间落地画面识别，用的是同一套识别表单；关掉开关 = 写显式 `null` 禁用）。

新建套件是**本地草稿**，点保存才落盘；改名保存 = 存成另一个文件。

## 报告

顶栏「报告」页扫描历史 run 的 findings 目录：左侧表格按时间列出任务、状态和
按 severity 着色的 findings 计数；选中一行右侧内嵌该 run 的 `report.html`
（证据截图/录屏等相对引用直接可用），没有 HTML 时退回展示原始 JSON；
最右是 findings 侧栏。
