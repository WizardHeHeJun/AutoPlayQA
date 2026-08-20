---
layout: home

hero:
  name: PipelineEditor
  text: AutoPlayQA 可视化任务编排器
  tagline: 画布拖拽编排任务状态机，真值校验 + lint，真机运行实时高亮
  actions:
    - theme: brand
      text: 快速上手
      link: /guide/quick-start
    - theme: alt
      text: 了解架构
      link: /reference/architecture

features:
  - title: 画布编排
    details: React Flow 画布上拖拽连线即编排状态机——实线是 next、橙虚线是 on_timeout，连线/删边都是对 next 数组的增量操作，永不从图反推 JSON。
  - title: 真值校验
    details: 停手 0.8 秒自动把当前 doc 发给后端干跑 task_loader.resolve_task，编辑器不复刻任何校验规则；再叠加后端 lint 与前端本地提示，三类合并进问题面板。
  - title: 真机运行高亮
    details: 后台线程跑引擎，on_step 回调经 WebSocket 推送（事件带 seq，断线快照补齐），画布高亮当前节点 + visited 轨迹，停止走协作式干净收尾。
  - title: AI / MCP 协同
    details: 后端在 /mcp 内嵌只做编辑面的 MCP server，与编辑器同进程；AI 一落盘，文件监视器就把 task_changed 推给前端，自动重载或弹冲突横幅。
---
