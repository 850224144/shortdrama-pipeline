@echo off
chcp 65001 >nul
echo === 短剧工坊一键启动 ===

echo [1/3] 启动 Ollama...
start /b "" "C:\Users\86159\AppData\Local\Programs\Ollama\ollama.exe" serve
timeout /t 2 /nobreak >nul

echo [2/3] 启动 webapp...
cd /d D:\短剧工坊\webapp
start "短剧工坊" "C:\Users\86159\AppData\Local\Programs\Python\Python311\python.exe" app.py
timeout /t 3 /nobreak >nul

echo [3/3] 打开浏览器...
start http://127.0.0.1:8765

echo === 完成 ===
