#!/bin/bash

# SPARK 服务管理脚本 (macOS / Linux)
# 功能与 run.bat 对齐：启动 / 停止 / 重启 / 打开网页

PORT=8085
ALT_PORT=8086

# 切到脚本所在目录
cd "$(dirname "$0")"

# ─── 定位 Python ───────────────────────────────────────────
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo ""
    echo "[错误] 找不到 python / python3，Python 可能未安装或未加入 PATH。"
    echo "请安装 Python 或检查 PATH 环境变量。"
    echo ""
    read -p "按回车键退出..." _
    exit 1
fi

# ─── 虚拟环境 (解决 macOS Homebrew PEP 668 限制) ───────────
# Google FX 运行时与依赖已经内置/统一声明，新的 venv 不再借用系统 site-packages。
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "[SPARK] 首次运行，正在创建虚拟环境 ($VENV_DIR) ..."
    $PYTHON_CMD -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
PYTHON_CMD="python"

# ─── 工具函数 ─────────────────────────────────────────────
get_pid_on_ports() {
    lsof -t -i:$PORT -i:$ALT_PORT 2>/dev/null
}

open_url() {
    URL="http://127.0.0.1:$PORT/"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        open "$URL"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v xdg-open &>/dev/null; then
            xdg-open "$URL"
        else
            echo "请在浏览器中打开 $URL"
        fi
    else
        echo "请在浏览器中打开 $URL"
    fi
}

# ─── 启动服务 ─────────────────────────────────────────────
start_service() {
    echo ""
    echo "[SPARK] 正在以 $PORT 端口后台启动主服务..."
    mkdir -p outputs

    # 检查并安装依赖
    $PYTHON_CMD -c "import PIL, requests, numpy, playwright, pydantic, dotenv" &>/dev/null
    if [ $? -ne 0 ]; then
        echo "[SPARK] 缺少依赖，正在安装 (pip install -r requirements.txt)..."
        $PYTHON_CMD -m pip install -r requirements.txt
        if [ $? -ne 0 ]; then
            echo ""
            echo "[错误] 依赖安装失败，请检查网络连接或手动执行:"
            echo "  source .venv/bin/activate && pip install -r requirements.txt"
            echo ""
            read -p "按回车键退出..." _
            return 1
        fi
    fi

    # 后台启动服务。
    # fd1/fd2 直接追加进 server.log（不是另开一个 server_nohup.log）：日志只留
    # 一个落点。Python 侧的 _console_stream 探到这不是终端就不再往 fd1 重复写
    # 一份，所以两路合起来正好是一份完整、不重样的日志。
    # 必须用 >> 而不是 >：> 会在每次启动时把历史日志截断清零。
    # 这一路兜的是 server_common 装好日志流之前的输出——依赖缺失、语法错误、
    # 端口占用这类连日志系统都没来得及初始化就死掉的情况。
    nohup $PYTHON_CMD server.py >> server.log 2>&1 &

    # 等待端口就绪（最多 ~28 秒，启动期可能有 manifest 迁移）
    echo "[SPARK] 等待服务绑定端口 $PORT ..."
    SUCCESS=0
    for i in $(seq 1 40); do
        if lsof -i:$PORT &>/dev/null; then
            SUCCESS=1
            break
        fi
        sleep 0.7
    done

    if [ $SUCCESS -eq 1 ]; then
        open_url
        echo "[SPARK] 服务已启动，已为您打开网页 http://127.0.0.1:$PORT/"
        echo ""
        sleep 3
    else
        echo ""
        echo "[错误] 主服务在 28 秒内未能监听端口 $PORT，启动失败。"
        echo "最近的 server.log 日志："
        echo "------------------------------------------------"
        if [ -f server.log ]; then
            tail -n 20 server.log
        else
            echo "(server.log 不存在)"
        fi
        echo "------------------------------------------------"
        echo "请检查以上错误信息，或联系支持。"
        echo ""
        read -p "按回车键退出..." _
    fi
}

# ─── 停止服务 ─────────────────────────────────────────────
stop_service() {
    echo "[SPARK] 正在停止 $PORT 主服务及所有残留的 $ALT_PORT 旧实例..."
    PIDS=$(get_pid_on_ports)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs kill -9 2>/dev/null
    fi
    rm -f server.pid 2>/dev/null
    echo "[SPARK] 已停止。"
    sleep 3
}

# ─── 主流程 ───────────────────────────────────────────────
RUNNING_PIDS=$(get_pid_on_ports)

if [ -n "$RUNNING_PIDS" ]; then
    # 服务已在运行 → 显示菜单
    echo ""
    echo "============================================"
    echo "  SPARK 已在运行 (PID $RUNNING_PIDS，端口 $PORT)"
    echo "============================================"
    echo "  服务已在后台运行中，正在为您打开浏览器网页..."
    open_url
    echo ""
    echo "  [1] 停止服务"
    echo "  [2] 重启服务"
    echo "  [3] 再次打开网页"
    echo "  [4] 退出"
    echo ""
    read -p "请选择 [1-4] (直接回车默认退出): " choice

    case $choice in
        1)
            stop_service
            ;;
        2)
            stop_service
            sleep 1
            start_service
            ;;
        3)
            open_url
            ;;
        *)
            exit 0
            ;;
    esac
else
    # 服务未运行 → 直接启动
    start_service
fi
