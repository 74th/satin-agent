#!/bin/sh
echo "satin startup: mode=$SATIN_MODE first_run=$SATIN_FIRST_RUN"

# (B) Claude Code: register the Echo MCP (in-cluster, direct) in the host config dir that every
# session reads. The key is passed as an env var from a Secret and expanded by Claude Code at
# connect time; it is never written to disk.
if [ "$SATIN_MODE" = "cc" ] && [ -n "${ECHO_MCP_API_KEY:-}" ]; then
  dir="${SATIN_HOST_CONFIG_DIR:-/home/satin/.claude-host}"
  mkdir -p "$dir"
  cat > "$dir/.claude.json" <<'EOF'
{"mcpServers": {"echo": {"type": "http", "url": "http://echo-mcp.satin-mcp.svc.cluster.local:8080/mcp", "headers": {"X_API_KEY": "${ECHO_MCP_API_KEY}"}}}}
EOF
fi
