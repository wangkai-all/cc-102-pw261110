#!/bin/sh
# Helper for local dev/testing: start or stop the server via a pidfile.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PIDFILE="$DIR/.server.pid"
DB="${TOPICFEED_DB:-$DIR/data/topicfeed.db}"
PORT="${TOPICFEED_PORT:-8765}"

stop() {
  if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    kill "$PID" 2>/dev/null || true
    sleep 0.4
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
}

case "${1:-start}" in
  stop)
    stop
    echo "stopped"
    ;;
  start|restart)
    stop
    cd "$DIR"
    TOPICFEED_PORT="$PORT" TOPICFEED_DB="$DB" \
      setsid python3 -m app.main >"$DIR/.server.log" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if curl -sf -m 1 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
        echo "started on port $PORT (pid $(cat "$PIDFILE"))"
        exit 0
      fi
      sleep 0.3
    done
    echo "failed to start; log:" >&2
    cat "$DIR/.server.log" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 {start|stop|restart}" >&2
    exit 2
    ;;
esac
