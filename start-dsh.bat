@echo off
setlocal enabledelayedexpansion
chcp 936 >nul

rem ============================================================
rem  dsh 启动入口（双击即用）
rem  本文件与任何 AI agent / IDE 插件【完全无关】：
rem  不引用、不依赖它们的任何目录或解释器。
rem
rem  解释器优先级：环境变量 DSH_PYEXE > 系统 Python > py 启动器 > PATH
rem ============================================================

rem PATH 钉扎：System32 最前（防 whoami 被 Git 抢）+ 补 pnpm/node 落点
set "PATH=%SystemRoot%\system32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
set "PATH=%PATH%;%APPDATA%\npm;%LOCALAPPDATA%\pnpm;%ProgramFiles%\nodejs"

set "TOOLS=%USERPROFILE%\dsh-launcher"

rem ---- 0) 允许用环境变量指定解释器 ----
set "PYEXE="
if defined DSH_PYEXE if exist "%DSH_PYEXE%" set "PYEXE=%DSH_PYEXE%"

rem ---- 1) 用户级安装的 Python ----
for %%V in (314 313 312 311 310) do (
  if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)

rem ---- 2) 机器级安装的 Python ----
for %%V in (314 313 312 311 310) do (
  if not defined PYEXE if exist "%ProgramFiles%\Python%%V\python.exe" set "PYEXE=%ProgramFiles%\Python%%V\python.exe"
)

rem ---- 3) Windows 的 py 启动器 ----
if not defined PYEXE (
  for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
)

rem ---- 4) PATH 里的 python（排除 WindowsApps 商店桩）----
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
    if not defined PYEXE set "PYEXE=%%P"
  )
)

if not defined PYEXE (
  echo.
  echo [X] 找不到 Python 解释器（本工具需要 Python 3.8 以上）
  echo.
  echo     解决办法（任选其一）:
  echo       1. 安装 Python: https://www.python.org/downloads/
  echo       2. 设置环境变量 DSH_PYEXE 指向你的 python.exe
  echo.
  pause
  exit /b 1
)

if not exist "%TOOLS%\dsh-launcher.py" (
  echo.
  echo [X] 找不到启动器: %TOOLS%\dsh-launcher.py
  echo     本工具应位于 %USERPROFILE%\dsh-launcher\
  echo.
  pause
  exit /b 1
)

rem ---- 启动：1 秒内无输入自动启动 dsh；有按键进菜单 ----
"%PYEXE%" "%TOOLS%\dsh-launcher.py"
