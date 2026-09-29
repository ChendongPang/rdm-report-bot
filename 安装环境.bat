@echo off
chcp 65001 >nul
title 一键安装 RDM 填报机器人

echo ============================================
echo   RDM 自动填报机器人 - 首次安装
echo ============================================
echo.

REM 检测 Python 是否安装
where python >nul 2>nul
if errorlevel 1 (
    echo [X] 系统中没找到 Python，请先到以下地址下载安装：
    echo     https://www.python.org/downloads/
    echo.
    echo 安装时请勾选 "Add Python to PATH"，然后重新运行本脚本。
    echo.
    pause
    exit /b 1
)

echo [√] Python 已安装：
python --version
echo.

REM 创建虚拟环境
if not exist ".venv\Scripts\python.exe" (
    echo [*] 正在创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [X] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo [√] 虚拟环境创建完成
) else (
    echo [√] 虚拟环境已存在，跳过创建
)
echo.

REM 安装依赖
echo [*] 正在安装依赖（pip install -r requirements.txt）...
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [X] 依赖安装失败
    pause
    exit /b 1
)
echo [√] 依赖安装完成
echo.

REM 安装 Chromium
echo [*] 正在下载 Chromium 浏览器（约 150MB，请勿关闭）...
.\.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
    echo [X] Chromium 安装失败
    pause
    exit /b 1
)
echo [√] Chromium 安装完成
echo.

echo ============================================
echo   安装完成！双击"运行填报机器人.bat"即可启动
echo ============================================
echo.
pause