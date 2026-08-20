# PipelineEditor（可视化任务编排器）启动入口：并行拉起后端 :8930 + 前端 :5173。
# 只是转发到 pipeline_editor\scripts\dev.ps1，参数原样传（如 -Python <python.exe 路径>）。
#
# 用法: powershell -File editor.ps1 [-Python <python.exe 路径>]

& (Join-Path $PSScriptRoot 'pipeline_editor\scripts\dev.ps1') @args
