# outputs/ — 运行产物目录

本目录由程序运行时生成,**整个目录在 `.gitignore` 中(不进版本控制)**。可以随时清空里面的运行产物,程序会按需重建。

## 目录约定

| 子目录 | 内容 | 谁写入 | 清理策略 |
|--------|------|--------|----------|
| `findings/` | QA 测试发现证据:`<日期>/<设备>/<run_id>/` 下含 report.json + **report.html**(双击浏览器直接看,零外部依赖) + 截图 + logcat + timeline + video/ + pcap/(opt-in,需 root+tcpdump) | FindingsRecorder | **启动时自动清理**超过 `findings.retention_days`(默认 14 天)的日期目录 |
| `screenshots/` | MCP/CLI 按需截图(`mcp_*`/`marked_*`) | ScreenshotCapturer | 调试产物,可随时手动删,目录会自动重建 |
| `recordings/` | 手势录制会话:`<时间戳>/` 下含 gestures.json + 每个手势的 before/after/anchor PNG | GestureRecordingRegistry | 手动 |
| `agent_sessions/` | 智能体自录动作日志:`<时间戳>_<label>/` 下含 session.json + 每步动作前截图 `s001_before.png`(kind=explore 自主探索 / handoff 节点交接归档) | ActionLogRegistry(MCP `record_actions_start/stop`) | 手动(路径可用 config `recording.agent_sessions_dir` 改) |
| `cache/` | 回放锚点缓存 `replay_cache.json` | ReplayCache | `task cache clear` |
| `touch_calibration/` | 触控标定数据 | 标定脚本 | 手动 |

## findings 导出(交付物)

- 配置 `findings.export_dir` 后,**每次**产生 finding 的运行会自动把整个 run 打成**单个 zip**导出:
  `<时间戳>_<任务>_<设备>_<状态>.zip`(report.json 与 report.html 在 zip 根,自包含相对路径;解压后直接打开 report.html 即可看图看录屏)。
- MCP `run_task(export_to=<目录>)` 可按次指定导出目录,产出同样的 zip;结果的 `report["export_path"]` 是该 zip 路径。
- 本地 `findings/` 下仍保留可浏览的目录结构(供直接查看 + 保留天数滚动清理),**只有交付物是 zip**。

## 相关配置(config.yaml `findings:` 段,缺省走默认)

```yaml
findings:
  enabled: true
  retention_days: 14        # 启动时删除超过 N 天的 findings 日期目录;<=0 关闭自动清理
  output_dir: outputs/findings
  export_dir: null          # 设置后每次有 finding 的运行自动导出 zip 到此目录
  video: true               # 设备端滚动录屏作为证据
  video_segment_s: 60
  history: true             # 飞行记录仪:问题前 ~60s 上下文
  history_window_s: 60
  log_tail_lines: 300
  pcap:                     # bug 时刻协议快照(opt-in,需 root + 设备端 tcpdump)
    enabled: false          # 默认关;开启后有 finding 时 pull 抓包到 <run_id>/pcap/
    bpf_filter: "not port 5555 and not port 5037"  # 排除 adb 无线/adb server 流量
    snaplen: 262144         # 抓全包(256KiB 哨兵值);改 96 只抓包头
    segment_s: 60
    keep_segments: 2
    tcpdump_path: tcpdump   # 设备上 tcpdump 二进制路径(可 push 预置到 /data/local/tmp)
    su_mode: auto           # auto|su|direct:是否用 su 提权跑 tcpdump
```

## 备注

- 本目录的内容全部是**运行期产物**,不是仓库资产:换台机器 / 换个被测游戏重跑一遍就会重新长出来。
- 冒烟缺陷报告(`bug_reports/`)由 `.claude/skills/smoke-report/` 的脚本生成,同样落在这里,可整目录打包外发。
