@echo off
chcp 65001 >nul
title 打包 RDM 填报机器人

echo ============================================
echo   打包 RDM 自动填报机器人
echo   （用户拿到 dist\RDM填报机器人.exe 直接双击即可，无需装 Python）
echo ============================================
echo.

REM 1) 装 PyInstaller
if not exist ".venv\Scripts\python.exe" (
    echo [X] 未检测到 .venv，请先双击"安装环境.bat"
    pause
    exit /b 1
)
echo [*] 安装 PyInstaller ...
.\.venv\Scripts\python.exe -m pip install pyinstaller -q
if errorlevel 1 (
    echo [X] PyInstaller 安装失败
    pause
    exit /b 1
)
echo [√] PyInstaller 已就绪
echo.

REM 2) 清理旧产物
echo [*] 清理旧 build / dist 目录 ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

REM 3) 打包
echo [*] 正在打包（首次约 2-3 分钟，请勿关闭）...
.\.venv\Scripts\python.exe -m PyInstaller ^
    --onefile ^
    --name "RDM填报机器人" ^
    --collect-all playwright ^
    --collect-all chinese_calendar ^
    --hidden-import openpyxl ^
    --hidden-import requests ^
    --hidden-import llm_helper ^
    --hidden-import fill_progress ^
    webui.py

if errorlevel 1 (
    echo [X] 打包失败
    pause
    exit /b 1
)

REM 4) 拷贝示例文件到 dist（work_log.xlsx 是空模板，首次运行会用到）
if exist "work_log.xlsx" copy /y "work_log.xlsx" "dist\work_log.xlsx" >nul
if exist "README.md" copy /y "README.md" "dist\README.md" >nul

echo.
echo ============================================
echo   打包完成！
echo   生成文件：dist\RDM填报机器人.exe
echo ============================================
echo.
dir "dist\RDM填报机器人.exe" | findstr "RDM"
echo.
echo [提示] 把整个 dist 目录发给用户即可。
echo        用户双击 RDM填报机器人.exe，第一次会下载 Chromium（约 150MB），
echo        之后就能离线用了。
echo.
pause