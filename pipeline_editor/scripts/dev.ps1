# PipelineEditor 开发环境：并行拉起后端(FastAPI:8930) + 前端(Vite:5173)
#
# 用法: powershell -File pipeline_editor\scripts\dev.ps1 [-Python <python.exe 路径>]
#       仓库根的 editor.ps1 是它的薄包装，参数原样转发，等价。
#
# -Python 传 AutoPlayQA 所用环境的解释器（框架 README 的「环境」一节写了它在哪）。
# 不传就用 PATH 上的 python —— 只有在那个 python 正好是框架环境时才对。
param(
    [string]$Python = "python"
)

# <repo>/pipeline_editor/scripts/dev.ps1 → $root = <repo>/pipeline_editor
$root = Split-Path -Parent $PSScriptRoot

# 后端默认按自身位置上推一级定位仓库根；只有把编辑器挪出仓库时才需要显式指定。
if ($env:AUTOPLAYQA_ROOT) {
    Write-Host "AUTOPLAYQA_ROOT (来自环境变量): $env:AUTOPLAYQA_ROOT"
} else {
    Write-Host "AutoPlayQA 根目录: $(Split-Path -Parent $root)（自动定位）"
}

Write-Host "启动后端 http://127.0.0.1:8930 ..."
$backend = Start-Process -FilePath $Python `
    -ArgumentList "$root\backend\main.py" `
    -WorkingDirectory $root -PassThru -WindowStyle Minimized

Write-Host "启动前端 http://localhost:5173 ..."
try {
    Set-Location "$root\frontend"
    npm run dev
} finally {
    if ($backend -and -not $backend.HasExited) {
        Write-Host "停止后端 (pid $($backend.Id))"
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
}
