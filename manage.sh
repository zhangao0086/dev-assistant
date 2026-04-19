#!/bin/bash
# Dev Assistant service management script

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Load PORT/DATA_DIR from config.json if not already set in env
_config_json="$HOME/.dev-assistant/config.json"
if [ -f "$_config_json" ]; then
    _cfg_port=$(python3 -c "import json,sys; d=json.load(open('$_config_json')); print(d.get('PORT',''))" 2>/dev/null)
    _cfg_datadir=$(python3 -c "import json,sys; d=json.load(open('$_config_json')); print(d.get('DATA_DIR',''))" 2>/dev/null)
    [ -n "$_cfg_port" ] && PORT="${PORT:-$_cfg_port}"
    [ -n "$_cfg_datadir" ] && DATA_DIR="${DATA_DIR:-$_cfg_datadir}"
fi

DATA_DIR="${DATA_DIR:-$HOME/.dev-assistant}"
PORT="${PORT:-8089}"
RUN_DIR="$PROJECT_DIR/run"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$LOG_DIR/server.log"

mkdir -p "$RUN_DIR" "$LOG_DIR"

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        else
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1
}

start_server() {
    if is_running; then
        print_warning "Server is already running (PID: $(cat $PID_FILE))"
        return 0
    fi

    if ! command -v uvicorn > /dev/null 2>&1; then
        print_error "uvicorn not found — run: pip install -r requirements.txt"
        return 1
    fi

    print_info "Starting server (port: $PORT)..."
    nohup uvicorn server:app --host 0.0.0.0 --port "$PORT" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo $pid > "$PID_FILE"

    sleep 1
    if is_running; then
        print_success "Server started (PID: $pid, port: $PORT)"
        print_info "Visit: http://localhost:$PORT"
    else
        print_error "Server failed to start — check logs: $LOG_FILE"
        return 1
    fi
}

stop_server() {
    if ! is_running; then
        print_warning "Server is not running"
        return 0
    fi

    local pid=$(cat "$PID_FILE")
    print_info "Stopping server (PID: $pid)..."

    # Call shutdown endpoint first so the server can clean up claude/happy processes
    if curl -sf -X POST "http://localhost:${PORT}/admin/shutdown" > /dev/null 2>&1; then
        print_info "Shutdown signal sent to server"
        sleep 1
    else
        print_warning "Could not reach shutdown endpoint — killing process directly"
    fi

    kill $pid 2>/dev/null || true

    local count=0
    while ps -p $pid > /dev/null 2>&1; do
        sleep 1
        count=$((count + 1))
        if [ $count -ge 10 ]; then
            print_warning "Process not responding — force killing..."
            kill -9 $pid 2>/dev/null || true
            break
        fi
    done

    rm -f "$PID_FILE"
    print_success "Server stopped"
}

show_status() {
    echo ""
    echo "======================================"
    echo "  Dev Assistant Status"
    echo "======================================"
    echo ""
    if is_running; then
        print_success "Server: running (PID: $(cat $PID_FILE), port: $PORT)"
    else
        print_error "Server: not running"
    fi
    echo ""
    echo "Log file: $LOG_FILE"
    echo ""
}

case "$1" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        sleep 1
        start_server
        ;;
    status)
        show_status
        ;;
    logs)
        lines=${2:-50}
        if [ -f "$LOG_FILE" ]; then
            print_info "Last $lines lines of log:"
            tail -n "$lines" "$LOG_FILE"
        else
            print_warning "Log file does not exist"
        fi
        ;;
    follow)
        if [ -f "$LOG_FILE" ]; then
            print_info "Tailing log (Ctrl+C to stop):"
            tail -f "$LOG_FILE"
        else
            print_warning "Log file does not exist"
        fi
        ;;
    *)
        echo "Dev Assistant service manager"
        echo ""
        echo "Usage: $0 {start|stop|restart|status|logs|follow}"
        echo ""
        echo "Commands:"
        echo "  start           Start the server"
        echo "  stop            Stop the server"
        echo "  restart         Restart the server (recommended)"
        echo "  status          Show PID and port"
        echo "  logs [lines]    Print last N lines of log (default: 50)"
        echo "  follow          Tail the log in real time"
        echo ""
        exit 1
        ;;
esac

exit 0
