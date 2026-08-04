#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_PORT="${BOSUN_BACKEND_PORT:-8770}"
FRONTEND_PORT="${BOSUN_FRONTEND_PORT:-5199}"
RETRY_DELAY="${BOSUN_RETRY_DELAY:-1}"
BACKEND_PID=""
FRONTEND_PID=""
LAUNCHED_PID=""
SHUTTING_DOWN=0
MODE="dev"

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

usage() {
  printf '用法：%s [--prod]\n' "$(basename "${BASH_SOURCE[0]}")"
  printf '  （默认）开发模式：启动后端 + Vite dev server（前端热更新）。\n'
  printf '  --prod   生产模式：先 npm run build 生成 frontend/dist，再仅启动后端（后端托管前端产物）。\n'
  printf '           手机/PWA 访问后端端口即可，不再走 Vite dev，避免总是热重载。\n'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prod) MODE="prod" ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; fail "未知参数：$1" ;;
    esac
    shift
  done
}

require_dependencies() {
  [[ -x "$BACKEND_DIR/.venv/bin/python" ]] || fail "缺少 backend/.venv/bin/python，请先安装后端依赖。"
  [[ -d "$FRONTEND_DIR/node_modules" ]] || fail "缺少 frontend/node_modules，请先运行 npm install。"
  command -v npm >/dev/null 2>&1 || fail "找不到 npm，请先安装 Node.js/npm。"
  command -v lsof >/dev/null 2>&1 || fail "找不到 lsof，无法检查端口占用。"
}

build_frontend() {
  printf '正在构建前端产物（npm run build）...\n'
  ( cd "$FRONTEND_DIR" && npm run build ) || fail "前端构建失败，已中止。"
  [[ -d "$FRONTEND_DIR/dist" ]] || fail "构建结束但未找到 frontend/dist。"
}

listener_pids() {
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

clear_port() {
  local port="$1"
  local pids
  pids="$(listener_pids "$port")"
  [[ -z "$pids" ]] && return 0

  printf '端口 %s 已占用，正在停止进程：%s\n' "$port" "$(printf '%s' "$pids" | tr '\n' ' ')"
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true

  local attempt
  for attempt in 1 2 3 4 5; do
    [[ -z "$(listener_pids "$port")" ]] && return 0
    sleep "$RETRY_DELAY"
  done

  pids="$(listener_pids "$port")"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
  fi

  for attempt in 1 2 3 4 5; do
    [[ -z "$(listener_pids "$port")" ]] && return 0
    sleep "$RETRY_DELAY"
  done

  printf '错误：强制停止后端口 %s 仍被占用。\n' "$port" >&2
  return 1
}

child_group_alive() {
  local pid="$1"
  kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null
}

signal_child_group() {
  local signal_name="$1"
  local pid="$2"
  kill "-$signal_name" -- "-$pid" 2>/dev/null || kill "-$signal_name" "$pid" 2>/dev/null || true
}

stop_child() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  signal_child_group TERM "$pid"

  local attempt
  for attempt in 1 2 3 4 5; do
    if ! child_group_alive "$pid"; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep "$RETRY_DELAY"
  done

  signal_child_group KILL "$pid"
  for attempt in 1 2 3 4 5; do
    if ! child_group_alive "$pid"; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep "$RETRY_DELAY"
  done

  printf '错误：无法停止服务进程组 %s。\n' "$pid" >&2
  return 1
}

launch_child_group() {
  local directory="$1"
  shift
  (
    cd "$directory" || exit 1
    exec "$BACKEND_DIR/.venv/bin/python" -c \
      'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
  ) &
  LAUNCHED_PID=$!

  local attempt
  for attempt in 1 2 3 4 5; do
    kill -0 -- "-$LAUNCHED_PID" 2>/dev/null && return 0
    kill -0 "$LAUNCHED_PID" 2>/dev/null || return 0
    sleep 0.01
  done
}

cleanup() {
  [[ "$SHUTTING_DOWN" -eq 1 ]] && return 0
  SHUTTING_DOWN=1
  local failed=0
  if [[ -n "$BACKEND_PID" ]]; then
    stop_child "$BACKEND_PID" || failed=1
    clear_port "$BACKEND_PORT" || failed=1
  fi
  if [[ -n "$FRONTEND_PID" ]]; then
    stop_child "$FRONTEND_PID" || failed=1
    clear_port "$FRONTEND_PORT" || failed=1
  fi
  if [[ "$failed" -ne 0 ]]; then
    printf '错误：部分服务或端口未能完全清理。\n' >&2
    return 1
  fi
}

on_signal() {
  cleanup
  exit 130
}

lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [[ -z "$ip" ]] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  printf '%s' "$ip"
}

parse_args "$@"

trap cleanup EXIT
trap on_signal INT TERM
require_dependencies
clear_port "$BACKEND_PORT" || fail "无法清理后端端口 $BACKEND_PORT。"
if [[ "$MODE" == "dev" ]]; then
  clear_port "$FRONTEND_PORT" || fail "无法清理前端端口 $FRONTEND_PORT。"
fi

# 生产模式：先构建前端产物，backend 启动时会自动挂载 frontend/dist。
if [[ "$MODE" == "prod" ]]; then
  build_frontend
fi

launch_child_group "$BACKEND_DIR" env BOSUN_HOST=0.0.0.0 "BOSUN_PORT=$BACKEND_PORT" .venv/bin/python run.py
BACKEND_PID="$LAUNCHED_PID"
if [[ "$MODE" == "dev" ]]; then
  launch_child_group "$FRONTEND_DIR" npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT"
  FRONTEND_PID="$LAUNCHED_PID"
fi

IP="$(lan_ip)"
if [[ "$MODE" == "prod" ]]; then
  printf '\n生产模式：后端已托管前端产物（frontend/dist）。\n'
  printf '本机：http://127.0.0.1:%s\n' "$BACKEND_PORT"
  if [[ -n "$IP" ]]; then
    printf '局域网/PWA：http://%s:%s\n' "$IP" "$BACKEND_PORT"
  else
    printf '局域网/PWA：请使用本机局域网 IP 替换 <本机IP>：http://<本机IP>:%s\n' "$BACKEND_PORT"
  fi
  printf '按 Ctrl+C 停止后端服务。\n\n'
else
  printf '\n后端：http://127.0.0.1:%s\n' "$BACKEND_PORT"
  printf '前端：http://localhost:%s\n' "$FRONTEND_PORT"
  if [[ -n "$IP" ]]; then
    printf '局域网：http://%s:%s\n' "$IP" "$FRONTEND_PORT"
  else
    printf '局域网：请使用本机局域网 IP 替换 <本机IP>：http://<本机IP>:%s\n' "$FRONTEND_PORT"
  fi
  printf '按 Ctrl+C 停止全部服务。\n\n'
fi

if [[ "$MODE" == "prod" ]]; then
  while kill -0 "$BACKEND_PID" 2>/dev/null; do
    sleep 1
  done
  printf '后端服务已退出。\n' >&2
else
  while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
    sleep 1
  done
  printf '有服务意外退出，正在停止另一个服务。\n' >&2
fi
exit 1
