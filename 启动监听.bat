@echo off
chcp 65001 >nul
title 接单监听
cd /d "%~dp0"

REM 依次尝试：官方 py 启动器 → PATH 里的 python → 常见安装位置

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 clip_watch.py
  goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
  python clip_watch.py
  goto :end
)

if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" clip_watch.py
  goto :end
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" clip_watch.py
  goto :end
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" clip_watch.py
  goto :end
)

if exist "%USERPROFILE%\anaconda3\python.exe" (
  "%USERPROFILE%\anaconda3\python.exe" clip_watch.py
  goto :end
)

echo.
echo   没找到 Python。
echo   请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
echo   安装时记得勾选 "Add python.exe to PATH"。
echo.
pause

:end
