@echo off
chcp 65001 >nul
title RDM 自动填报机器人

REM 没装就先提示安装
if not exist ".venv\Scripts\python.exe" (
    echo 未检测到虚拟环境 .venv，请先双击"安装环境.bat"完成首次安装。
    echo.
    pause
    exit /b 1
)

echo 正在启动 RDM 自动填报机器人 ...
echo 浏览器会自动打开 http://127.0.0.1:8765
echo 关闭本窗口或 Ctrl+C 可停止机器人
echo ============================================
echo.

.\.venv\Scripts\python.exe webui.py

if errorlevel 1 (
    echo.
    echo [X] 启动失败，按任意键关闭 ...
    pause
)