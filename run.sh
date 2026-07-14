#!/bin/bash

# Target port
PORT=8085
ALT_PORT=8086

# Change directory to the script's directory
cd "$(dirname "$0")"

# Find python command
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "Error: Python is not installed or not in PATH."
    exit 1
fi

# Detect if the port is running
get_pid_on_ports() {
    lsof -t -i:$PORT -i:$ALT_PORT 2>/dev/null
}

RUNNING_PIDS=$(get_pid_on_ports)

open_url() {
    URL="http://127.0.0.1:$PORT/"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        open "$URL"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v xdg-open &>/dev/null; then
            xdg-open "$URL"
        else
            echo "Please open $URL in your browser."
        fi
    else
        echo "Please open $URL in your browser."
    fi
}

start_service() {
    echo "Starting SPARK service on port $PORT..."
    mkdir -p outputs
    
    # Check dependencies first
    $PYTHON_CMD -c "import PIL, requests" &>/dev/null
    if [ $? -ne 0 ]; then
        echo "Warning: Pillow or requests library not found. Installing dependencies..."
        $PYTHON_CMD -m pip install -r requirements.txt
    fi

    # Start the server in the background
    nohup $PYTHON_CMD server.py > server_nohup.log 2>&1 &
    
    # Wait for the service to start
    echo "Waiting for the service to bind to port $PORT..."
    SUCCESS=0
    for i in {1..20}; do
        if lsof -i:$PORT &>/dev/null; then
            SUCCESS=1
            break
        fi
        sleep 0.5
    done

    if [ $SUCCESS -eq 1 ]; then
        echo "SPARK service started successfully."
        open_url
    else
        echo "Error: Service failed to start in 10 seconds. Check server.log and server_nohup.log."
        tail -n 20 server.log 2>/dev/null
    fi
}

stop_service() {
    echo "Stopping SPARK service on ports $PORT and $ALT_PORT..."
    PIDS=$(get_pid_on_ports)
    if [ -n "$PIDS" ]; then
        echo "Killing processes: $PIDS"
        echo "$PIDS" | xargs kill -9 2>/dev/null
    fi
    rm -f server.pid 2>/dev/null
    echo "SPARK service stopped."
}

if [ -n "$RUNNING_PIDS" ]; then
    echo "============================================"
    echo "  SPARK is already running on port $PORT"
    echo "============================================"
    open_url
    
    echo "Choose an action:"
    echo "  [1] Stop service"
    echo "  [2] Restart service"
    echo "  [3] Open webpage again"
    echo "  [4] Exit"
    echo ""
    read -p "Enter choice [1-4] (default: Exit): " choice
    
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
    start_service
fi
