@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PORT=8085"
title SPARK 服务管理 (端口 %PORT%)

rem ============================================================
rem  首次运行引导：定位 Python -> 建虚拟环境 -> 装依赖 -> 备好配置
rem  与 run.sh (macOS/Linux) 对齐。此前这里只找 pythonw 就直接跑
rem  server.py，新机器上克隆完第一次双击必然是 ModuleNotFoundError，
rem  而 pythonw 没有控制台、错误连 server.log 都进不去，界面上只会
rem  显示"服务在 28 秒内未能监听端口"，看不出真正原因。
rem ============================================================

rem --- 定位 Python 解释器（py 启动器 -> PATH 上的 python -> 已知安装路径）。
rem     解释器与参数分开存：路径要带引号（用户名含空格时 C:\Users\Big Boss\...
rem     不加引号会被拆成两段），而 py 启动器的 -3 又不能一起塞进引号里。---
set "PY_EXE="
set "PY_ARGS="
where py >nul 2>&1 && (set "PY_EXE=py" & set "PY_ARGS=-3")
if not defined PY_EXE (
    where python >nul 2>&1 && set "PY_EXE=python"
)
if not defined PY_EXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY_EXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
)
if not defined PY_EXE (
    echo.
    echo [错误] 找不到 Python。请先安装 Python 3.11 或更高版本
    echo        安装时务必勾选 "Add Python to PATH"：https://www.python.org/downloads/
    echo.
    pause
    goto end
)

rem --- 虚拟环境：所有依赖装在项目内的 .venv，不污染系统 Python ---
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%VENV_PY%" (
    echo.
    echo [SPARK] 首次运行，正在创建虚拟环境 .venv ...
    "%PY_EXE%" %PY_ARGS% -m venv ".venv"
)
if not exist "%VENV_PY%" (
    echo.
    echo [错误] 创建虚拟环境失败，.venv\Scripts\python.exe 不存在。
    echo        请确认 Python 安装完整（含 venv 模块），或手动执行：
    echo        python -m venv .venv
    echo.
    pause
    goto end
)

rem --- 依赖：缺什么装什么。用 python.exe 而不是 pythonw.exe 探测，
rem     这样报错看得见（pythonw 无控制台，错误会静默消失）---
"%VENV_PY%" -c "import PIL, requests, numpy, playwright, pydantic, dotenv" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [SPARK] 缺少依赖，正在安装 ^(pip install -r requirements.txt^)，首次约需 1-3 分钟...
    "%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络连接，或手动执行：
        echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
        echo.
        pause
        goto end
    )
)

rem --- 配置文件：server_config.json 含密钥，不在仓库里（.gitignore）。
rem     新机器上从模板生成一份，让服务能起来。不能直接 copy 模板——
rem     模板里 accessCode/apiKey 填的是中文说明文字，原样拷过去等于开着
rem     一个谁也不知道口令的门禁，见 tools/bootstrap_config.py。---
if not exist "server_config.json" (
    echo.
    "%VENV_PY%" tools\bootstrap_config.py
    echo.
    timeout /t 5 >nul
)

rem --- 检测 8085 是否已在监听 ---
set "RUNNING="
for /f %%P in ('powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"') do set "RUNNING=%%P"

if defined RUNNING goto menu

:start
echo.
echo [SPARK] 正在以 %PORT% 端口后台启动主服务...
if not exist "outputs" mkdir "outputs"

rem --- 启动前先做一次可见的导入自检：语法错误、依赖损坏、配置文件写坏
rem     这类"连日志系统都没来得及初始化就死掉"的情况，在这里就能报出人话，
rem     而不是让用户对着"未能监听端口"猜。---
"%VENV_PY%" -c "import server_common" 2>server_boot_error.txt
if errorlevel 1 (
    echo.
    echo [错误] 服务启动自检失败，错误信息：
    echo ------------------------------------------------
    type server_boot_error.txt
    echo ------------------------------------------------
    echo.
    pause
    goto end
)
del server_boot_error.txt >nul 2>&1

start "" "%VENV_PYW%" server.py

rem --- 服务入口永久固定为 %PORT% 一个端口。图像服务站已并入主页面的
rem     「🎨 图像工坊」标签页，不再单独拉起 8086，否则重启会不断堆积
rem     重复实例（2026-07-04 实测同时挂着 2 个 8086 进程）。

rem --- 轮询确认主端口真正起来了，而不是盲等再关窗口（单次 powershell 内部自行重试，避免反复起进程）---
rem     等待窗口 ~28 秒：启动期要先跑 outputs/ 的 manifest 同步迁移（PNG→WebP），
rem     素材多时 10 秒不够，会误报“启动失败”
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 40;$i++){ if(Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue){$ok=$true;break}; Start-Sleep -Milliseconds 700 }; if(-not $ok){exit 1}"
if errorlevel 1 goto notup
goto up

:notup
echo.
echo [错误] 主服务在 28 秒内未能监听端口 %PORT%，启动失败。
echo 最近的 server.log 日志：
echo ------------------------------------------------
powershell -NoProfile -Command "if (Test-Path 'server.log') { Get-Content 'server.log' -Tail 20 } else { '(server.log 不存在)' }"
echo ------------------------------------------------
echo 若日志里看不出原因，在本目录打开命令行执行下面这句，错误会直接打印出来：
echo     .venv\Scripts\python.exe server.py
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
echo   [1] 停止服务
echo   [2] 重启服务
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
echo [SPARK] 正在停止 8085 主服务及所有残留的 8086 旧实例...
rem 必须清干净 8085/8086 上的全部监听进程（旧逻辑只杀第一个 8086，导致重复实例越积越多）
powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort 8085,8086 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
if exist server.pid del server.pid >nul 2>&1
echo [SPARK] 已停止。
timeout /t 3 >nul
goto end

:restart
echo [SPARK] 正在重启服务（先清干净 8085/8086 全部残留实例）...
powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort 8085,8086 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Start-Sleep -Seconds 1"
set "RUNNING="
goto start

:end
endlocal
