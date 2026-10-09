#!/bin/bash
# 停止 proxy.py 及其子进程 litellm。
DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_PORT=4000
LITELLM_PORT=4001

CONFIG_PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
if [[ -n "$CONFIG_PY" && -f "$DIR/providers_config.json" ]]; then
    PORTS="$($CONFIG_PY - "$DIR/providers_config.json" 2>/dev/null <<'PY' || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    server = (json.load(f).get("server") or {})
print(server.get("proxy_port", 4000), server.get("litellm_port", 4001))
PY
)"
    if [[ "$PORTS" =~ ^[0-9]+\ [0-9]+$ ]]; then
        PROXY_PORT="${PORTS%% *}"
        LITELLM_PORT="${PORTS##* }"
    fi
fi

lsof -ti "tcp:${PROXY_PORT}" | xargs kill -9 2>/dev/null || true
lsof -ti "tcp:${LITELLM_PORT}" | xargs kill -9 2>/dev/null || true
echo "已停止 (:${PROXY_PORT} / :${LITELLM_PORT})"
