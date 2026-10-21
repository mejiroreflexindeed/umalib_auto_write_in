@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto no_python
"%PY%" -B "%~dp0tools\launch_operator.py" --check
"%PY%" -B "%~dp0tools\launch_operator.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" goto failed
exit /b 0

:no_python
echo [错误] 未找到内置 Python 解释器。
echo 预期路径: "%PY%"
echo 请完整解压源码包后重试，或用本机 Python 运行: python tools\launch_operator.py
echo.
pause
exit /b 2

:failed
echo.
echo [错误] 助手异常退出，退出码 %CODE%。请查看上方错误信息。
pause >nul
exit /b %CODE%
