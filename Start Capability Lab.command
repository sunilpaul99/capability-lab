#!/bin/sh
# Double-click in Finder to start Capability Lab and open it in your browser.
cd "$(dirname "$0")"
PORT=${PORT:-8765}
while lsof -i :$PORT >/dev/null 2>&1; do PORT=$((PORT+1)); done
( sleep 1; open "http://127.0.0.1:$PORT" ) &
PORT=$PORT exec /usr/bin/python3 server.py
