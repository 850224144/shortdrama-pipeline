@echo off
chcp 65001
echo ========================================
echo  短剧工坊 Web服务
echo ========================================
echo.

cd /d %~dp0

set PYTHON_EXE=C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe

echo [1/3] 检查Python...
%PYTHON_EXE% --version
if errorlevel 1 (
    echo Python未找到！请先安装Python 3.11
    pause
    exit /b 1
)

echo [2/3] 安装依赖...
%PYTHON_EXE% -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo 依赖安装失败！
    pause
    exit /b 1
)

echo [3/3] 启动服务...
echo.
echo 仪表盘：http://localhost:8765
echo 演示：http://localhost:8765/api/demo
echo.
echo 按Ctrl+C停止服务
echo.

%PYTHON_EXE% app.py
pause