@echo off
chcp 936 >nul
title 结束 DeepSeek Harness
echo ============================================================
echo    结束 dsh 实例，释放 3080 端口
echo ============================================================
echo.

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3080" ^| findstr "LISTENING"') do (
  echo 正在结束占用 3080 的进程  PID=%%p
  taskkill /PID %%p /F >nul 2>&1
)

echo.
echo 完成。现在可以重新运行 start-dsh.bat
echo.
pause
