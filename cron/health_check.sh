#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Dog Agent — Health Check Cron Script
# ---------------------------------------------------------------------------
# Queries the health endpoint at localhost:9113/health and checks for any
# active alerts. If alerts exist, forwards them to the alert_manager API
# (localhost:9118/alerts) so they get routed to configured destinations
# (Telegram, local log, etc.).
#
# Designed to be called by cron every 30 minutes.
#
# Usage:
#   ./cron/health_check.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

HEALTH_API="http://127.0.0.1:9113/health"
ALERT_API="http://127.0.0.1:9118/alerts"
CONFIG_FILE="${PROJECT_DIR}/config.yaml"

# ─── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; }

# ─── Extract dog name ─────────────────────────────────────────────────────────
DOG_NAME="Fido"
if [ -f "$CONFIG_FILE" ]; then
    CFG_NAME=$(grep 'name:' "$CONFIG_FILE" | head -1 | awk '{print $2}' | tr -d '"')
    [ -n "$CFG_NAME" ] && DOG_NAME="$CFG_NAME"
fi

# ─── Fetch health status ──────────────────────────────────────────────────────
info "Checking health at $HEALTH_API"

HEALTH_RESPONSE=$(curl -s -f --max-time 10 "$HEALTH_API" 2>/dev/null || true)

if [ -z "$HEALTH_RESPONSE" ]; then
    error "Health endpoint not responding — is health_monitor running on port 9113?"

    # Send an alert about the health service itself being down
    curl -s -X POST "$ALERT_API" \
        -H "Content-Type: application/json" \
        -d "{
            \"alert_type\": \"service_down\",
            \"severity\": \"high\",
            \"message\": \"Health monitor service unreachable — $DOG_NAME's vitals not being checked\",
            \"data\": {\"service\": \"health_monitor\", \"port\": 9113}
        }" \
        --max-time 10 2>/dev/null || true

    exit 1
fi

# ─── Parse health response and check for alerts ───────────────────────────────
info "Analyzing health data..."

# Use Python to parse the JSON and extract alerts
python3 -c "
import json
import urllib.request
import urllib.error

health = json.loads('''$HEALTH_RESPONSE''')

status = health.get('status', 'unknown')
active_alerts = health.get('active_alerts', [])
alert_count = health.get('active_alerts_count', 0)

if status == 'ok' and alert_count == 0:
    print('Health check passed — no active alerts')
    exit(0)

print(f'Found {alert_count} active alert(s) — forwarding to alert manager')
print(json.dumps(active_alerts, indent=2))
exit(1 if alert_count > 0 else 0)
" 2>&1 || HAS_ALERTS=$?

if [ "${HAS_ALERTS:-0}" -eq 1 ]; then
    info "Active health alerts detected — forwarding to alert_manager..."

    # Extract each alert and POST to alert_manager
    python3 -c "
import json, sys, urllib.request, urllib.error

health = json.loads('''$HEALTH_RESPONSE''')
alerts = health.get('active_alerts', [])

if not alerts:
    sys.exit(0)

for alert in alerts:
    payload = {
        'alert_type': alert.get('type', 'health_alert'),
        'severity': alert.get('severity', 'medium'),
        'message': alert.get('message', 'Health alert'),
        'data': {k: v for k, v in alert.items() if k not in ('type', 'severity', 'message')},
    }

    try:
        req = urllib.request.Request(
            '$ALERT_API',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            print(f'  ✅ Alert forwarded: {payload[\"alert_type\"]} ({payload[\"severity\"]})')
    except Exception as e:
        print(f'  ❌ Failed to forward alert: {e}', file=sys.stderr)
" 2>&1 || true

    info "Health alerts processed"
else
    info "No active health alerts — all clear"
fi