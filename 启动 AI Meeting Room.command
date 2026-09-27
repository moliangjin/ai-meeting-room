#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
PRODUCT_DATA_DIR="${AI_MEETING_ROOM_DATA_DIR:-${HOME}/.ai-meeting-room}"
LOG_DIR="$PRODUCT_DATA_DIR/logs"
LOG_FILE="$LOG_DIR/desktop-launch.log"
LOCK_DIR="${TMPDIR:-/tmp}/ai-meeting-room-launch.lock"

show_error() {
  local message="$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display dialog \"$message\" buttons {\"确定\"} with title \"AI Meeting Room\"" >/dev/null 2>&1 || true
  else
    print -u2 "$message"
  fi
}

if [[ ! -d "$PROJECT_DIR/desktop" || ! -f "$PROJECT_DIR/desktop/package.json" ]]; then
  show_error "AI Meeting Room 启动失败：找不到当前项目目录。"
  exit 1
fi

mkdir -p "$LOG_DIR"
if [[ -f "$LOG_FILE" ]] && (( $(stat -f%z "$LOG_FILE" 2>/dev/null || print 0) > 2097152 )); then
  tail -c 1048576 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

if [[ -d "$LOCK_DIR" ]]; then
  if [[ -f "$LOCK_DIR/pid" ]] && kill -0 "$(<"$LOCK_DIR/pid")" 2>/dev/null; then
    show_error "AI Meeting Room 已经在运行，未启动重复实例。"
    exit 0
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || true
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:8765 -sTCP:LISTEN -t >/dev/null 2>&1; then
  show_error "AI Meeting Room 已经在运行，未启动重复实例。"
  exit 0
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  show_error "AI Meeting Room 已经在启动，未启动重复实例。"
  exit 0
fi
print -r -- "$$" > "$LOCK_DIR/pid"
trap 'rm -f "$LOCK_DIR/pid"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$PROJECT_DIR"
unset AIMR_DESKTOP_BRAIN_POC_ON_START
{
  print -r -- "[$(date '+%Y-%m-%dT%H:%M:%S%z')] launch project=$PROJECT_DIR"
  /usr/bin/env npm --prefix desktop start
} >> "$LOG_FILE" 2>&1
exit_code=$?

if (( exit_code != 0 )); then
  show_error "AI Meeting Room 启动失败，请查看 ~/.ai-meeting-room/logs/desktop-launch.log。"
fi
exit "$exit_code"
