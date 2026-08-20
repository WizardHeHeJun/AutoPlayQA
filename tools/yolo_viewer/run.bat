@echo off
REM YOLO 实时识别验证窗口启动器 — 免记路径，参数原样透传给 yolo_viewer.py。
REM 用法: run.bat [--device XXX] [--model path] [--conf 0.20] [--hide 类名...]
REM 解释器: 默认用 PATH 里的 python；项目 conda 环境不在 PATH 时，先设环境变量，例如
REM   set PYTHON=<conda-envs>\game_automation\python.exe
if "%PYTHON%"=="" set PYTHON=python
"%PYTHON%" "%~dp0yolo_viewer.py" %*
