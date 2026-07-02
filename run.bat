@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PORT=8085"
title SPARK 服务管理 (端口 %PORT%)

rem --- 定位 pythonw.exe（PATH 找不到时回退到已知安装路径）---
set "PYW=pythonw"
where pythonw >nul 2>&1
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
        set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
    ) else (
        echo.
        echo [错误] 找不到 pythonw.exe，Python 可能未安装或未加入 PATH。
        echo 请安装 Python 或检查 PATH 环境变量。
        echo.
        pause
        goto end
    )
)

rem --- 检测 8085 是否已在监听 ---
set "RUNNING="
for /f %%P in ('powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"') do set "RUNNING=%%P"

if defined RUNNING goto menu

:start
echo.
echo [SPARK] 正在以 %PORT% 端口后台启动主服务 (pythonw)...
if not exist "outputs" mkdir "outputs"
start "" "%PYW%" server.py

echo [SPARK] 正在以 8086 端口后台启动图像子服务 (pythonw)...
setlocal
set "PORT=8086"
start /d "image-service-station" "" "%PYW%" server.py
endlocal

rem --- 轮询确认主端口真正起来了，而不是盲等再关窗口（单次 powershell 内部自行重试，避免反复起进程）---
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 15;$i++){ if(Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue){$ok=$true;break}; Start-Sleep -Milliseconds 700 }; if(-not $ok){exit 1}"
if errorlevel 1 goto notup
goto up

:notup
echo.
echo [错误] 主服务在 10 秒内未能监听端口 %PORT%，启动失败。
echo 最近的 server.log 日志：
echo ------------------------------------------------
powershell -NoProfile -Command "if (Test-Path 'server.log') { Get-Content 'server.log' -Tail 20 } else { '(server.log 不存在)' }"
echo ------------------------------------------------
echo 请检查以上错误信息，或联系支持。
echo.
pause
goto end

:up
start "" "http://127.0.0.1:%PORT%/"
echo [SPARK] 服务已启动，已为您打开网页 http://127.0.0.1:%PORT%/
echo.
timeout /t 3 >nul
goto end

:menu
echo.
echo ============================================
echo   SPARK 已在运行 (PID !RUNNING!，端口 %PORT%)
echo ============================================
echo   服务已在后台运行中，正在为您打开浏览器网页...
start "" "http://127.0.0.1:%PORT%/"
echo.
echo   [1] 停止服务 (含图像子服务)
echo   [2] 重启服务 (含图像子服务)
echo   [3] 再次打开网页
echo   [4] 退出
echo.
set "choice="
set /p choice="请选择 [1-4] (直接回车默认退出): "
if "!choice!"=="1" goto stop
if "!choice!"=="2" goto restart
if "!choice!"=="3" ( start "" "http://127.0.0.1:%PORT%/" & goto end )
goto end

:stop
echo [SPARK] 正在停止主服务 (PID !RUNNING!)...
taskkill /PID !RUNNING! /F >nul 2>&1
echo [SPARK] 正在停止 8086 端口的图像子服务...
for /f %%P in ('powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort 8086 -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"') do taskkill /PID %%P /F >nul 2>&1
echo [SPARK] 已停止。
timeout /t 3 >nul
goto end

:restart
echo [SPARK] 正在重启服务...
taskkill /PID !RUNNING! /F >nul 2>&1
for /f %%P in ('powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort 8086 -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"') do taskkill /PID %%P /F >nul 2>&1
powershell -NoProfile -Command "Start-Sleep -Seconds 1"
set "RUNNING="
goto start

:end
endlocal
