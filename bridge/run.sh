#!/bin/bash
# Hermes bridge launcher (Mac). Idempotent: kills an old instance, starts new.
# Logs: /tmp/jack-agent-bridge.log
cd "$(dirname "$0")/.."
[ -f /tmp/jack-agent-bridge.pid ] && kill "$(cat /tmp/jack-agent-bridge.pid)" 2>/dev/null
sleep 0.5
# macOS has no setsid; nohup + & detaches from this shell. PATH exported so
# the bridge's `hermes` subprocess (Hermes-specific; drop if your agent doesn't need PATH) resolves even when launched headless.
export PATH="$HOME/.hermes/hermes-agent/.hermes/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
nohup .venv/bin/python -m bridge.agent_bridge </dev/null >>/tmp/jack-agent-bridge.log 2>&1 &
echo $! > /tmp/jack-agent-bridge.pid
echo "bridge pid $(cat /tmp/jack-agent-bridge.pid)"
