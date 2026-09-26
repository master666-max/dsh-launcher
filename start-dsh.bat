@echo off
setlocal enabledelayedexpansion
chcp 936 >nul

rem ============================================================
rem  dsh 启动入口（双击即用）
rem  本文件与任何 AI agent / IDE 插件【完全无关】：
rem  不引用、不依赖它们的任何目录或解释器。
rem
rem  解释器优先级：环境变量 DSH_PYEXE > 系统 Python > py 启动器 > PATH
rem
rem  工具链目录（TOOLS）优先级（2026-09-26 起不再硬编码）：
rem    1) 环境变量 DSH_TOOLS 指定的目录
rem    2) 本 bat 所在目录（%~dp0）：仓库里那份直接双击，整目录搬走后 bat 跟着走
rem    3) %USERPROFILE%\dsh-launcher：README 的默认安装位，桌面副本走这条
rem  三处都找不到才报错，并把尝试过的位置全部列出来。
rem ============================================================

rem PATH 钉扎：System32 最前（防 whoami 被 Git 抢）+ 补 pnpm/node 落点
set "PATH=%SystemRoot%\system32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
set "PATH=%PATH%;%APPDATA%\npm;%LOCALAPPDATA%\pnpm;%ProgramFiles%\nodejs"

rem ---- 定位工具链目录 ----
rem [!] 每级都必须【if 与命令同行】：括号块内跨行 if 会让整个块解析中止，
rem     表现为双击秒退（2026-09-18 事故，自检第 6 项常驻守护）。
rem [!] 不要用子串取尾部反斜杠：变量为空时 cmd 的子串展开不生效，
rem     会把 "~-1" / "~0,-1" 原样留在 if 行里 -> rc=255 命令语法不正确
rem     （实测：空变量 + 同一行两处子串 = 必炸）。改用 for 取全路径名，
rem     自动去掉尾部反斜杠，三个来源由此得到同一种形态。
set "TOOLS="
if defined DSH_TOOLS if exist "%DSH_TOOLS%\dsh-launcher.py" for %%I in ("%DSH_TOOLS%.") do set "TOOLS=%%~fI"
if not defined TOOLS if exist "%~dp0dsh-launcher.py" for %%I in ("%~dp0.") do set "TOOLS=%%~fI"
if not defined TOOLS if exist "%USERPROFILE%\dsh-launcher\dsh-launcher.py" for %%I in ("%USERPROFILE%\dsh-launcher.") do set "TOOLS=%%~fI"

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
  echo [X] 找不到启动器 dsh-launcher.py
  echo.
  echo     已依次找过这三个位置:
  echo       1. DSH_TOOLS 环境变量 : %DSH_TOOLS%
  echo       2. 本 bat 所在目录    : %~dp0
  echo       3. 用户目录默认安装位 : %USERPROFILE%\dsh-launcher
  echo.
  echo     解决办法（任选其一）:
  echo       1. 把本 bat 放进工具链目录，与 dsh-launcher.py 同级
  echo       2. 设置环境变量 DSH_TOOLS 指向工具链目录
  echo       3. 把仓库 clone 到 %USERPROFILE%\dsh-launcher
  echo.
  pause
  exit /b 1
)

rem ---- 启动：1 秒内无输入自动启动 dsh；有按键进菜单 ----
"%PYEXE%" "%TOOLS%\dsh-launcher.py"
