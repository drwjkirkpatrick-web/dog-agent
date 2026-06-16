#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Dog Agent — Daily Report Cron Script
# ---------------------------------------------------------------------------
# Queries the behavior API at localhost:9115/behavior/summary and formats
# the response into a Telegram-friendly daily report. If Telegram is
# configured in config.yaml, sends via Telegram Bot API; otherwise prints
# to stdout.
#
# Designed to be called by cron (e.g., daily at 21:00).
#
# Usage:
#   ./cron/daily_report.sh                        # Auto-detect config path
#   ./cron/daily_report.sh /path/to/config.yaml   # Explicit config path
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE="${1:-$PROJECT_DIR/config.yaml}"
BEHAVIOR_API="http://127.0.0.1:9115/behavior/summary"
TELEGRAM_API="https://api.telegram.org"

# ─── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; }

# ─── Validate config ──────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    error "Config file not found: $CONFIG_FILE"
    error "Usage: $0 [path/to/config.yaml]"
    exit 1
fi

# ─── Extract Telegram config from YAML (basic grep/awk parser) ────────────────
# This avoids requiring a YAML parser in the cron script.
TELEGRAM_ENABLED=$(grep -A3 'telegram:' "$CONFIG_FILE" | grep 'enabled' | head -1 | awk '{print $2}')
BOT_TOKEN=$(grep -A3 'telegram:' "$CONFIG_FILE" | grep 'bot_token' | head -1 | awk '{print $2}' | tr -d '"')
CHAT_ID=$(grep -A3 'telegram:' "$CONFIG_FILE" | grep 'chat_id' | head -1 | awk '{print $2}' | tr -d '"')
DOG_NAME=$(grep 'name:' "$CONFIG_FILE" | head -1 | awk '{print $2}' | tr -d '"')

# Fallback dog name
if [ -z "$DOG_NAME" ]; then
    DOG_NAME="Fido"
fi

# ─── Fetch behavior summary ───────────────────────────────────────────────────
info "Fetching behavior summary from $BEHAVIOR_API"

RESPONSE=$(curl -s -f --max-time 10 "$BEHAVIOR_API" 2>/dev/null || true)

if [ -z "$RESPONSE" ]; then
    error "Failed to fetch behavior summary — is the behavior service running?"
    echo "⚠️ Could not generate daily report for $DOG_NAME — behavior service unreachable."
    exit 1
fi

# ─── Format the report ─────────────────────────────────────────────────────────
# Parse JSON using basic tools (jq not assumed available on Pi)
# We extract key fields with Python for reliability
REPORT=$(python3 -c "
import json, sys

try:
    data = json.loads('''$RESPONSE''')
except json.JSONDecodeError:
    print('⚠️ Could not parse behavior response.')
    sys.exit(1)

lines = []
lines.append(f'🐾 Daily Report for {data.get(\"summary\", {}).get(\"dog_name\", \"$DOG_NAME\")}')
lines.append(f'📅 {data.get(\"summary\", {}).get(\"date\", \"today\")}')
lines.append('')

summary = data.get('summary', {})
if summary:
    # Activity summary
    total_activity = summary.get('total_activity_minutes', 0)
    rest_minutes = summary.get('rest_minutes', 0)
    lines.append(f'🚶 Activity: {total_activity} min active, {rest_minutes} min resting')

    # Walk info
    walks = summary.get('walks_today', 0)
    last_walk = summary.get('last_walk_time', 'N/A')
    lines.append(f'🦮 Walks today: {walks}  (last: {last_walk})')

    # Meals
    meals = summary.get('meals_today', 0)
    lines.append(f'🍽️ Meals: {meals}')

    # Health context
    if 'avg_heart_rate' in summary:
        lines.append(f'❤️ Avg heart rate: {summary[\"avg_heart_rate\"]} bpm')
    if 'avg_temperature' in summary:
        lines.append(f'🌡️ Avg temperature: {summary[\"avg_temperature\"]} °C')
else:
    lines.append('⏳ Still learning routine (need more data)')

lines.append('')

# Deviations / anomalies
deviations = data.get('summary', {}).get('deviations', [])
if deviations:
    lines.append(f'⚠️ *{len(deviations)} deviation(s) detected:*')
    for d in deviations:
        lines.append(f'  • {d.get(\"description\", d.get(\"type\", \"unknown\"))}')
    lines.append('')
else:
    lines.append('✅ No anomalies detected — all normal!')

# Routine status
routine = data.get('routine', {})
if routine.get('ready', False):
    next_walk = routine.get('next_expected_walk', 'N/A')
    lines.append(f'⏰ Next expected walk: {next_walk}')
    lines.append(f'📊 Routine confidence: {routine.get(\"confidence\", \"N/A\")}')

print('\n'.join(lines))
" 2>&1) || REPORT="⚠️ Could not generate report for $DOG_NAME."

# ─── Send via Telegram or print ──────────────────────────────────────────────
if [ "${TELEGRAM_ENABLED:-}" = "true" ] && [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
    info "Sending report via Telegram..."

    SEND_RESULT=$(curl -s -X POST "$TELEGRAM_API/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" \
        -d "text=$REPORT" \
        -d "parse_mode=Markdown" \
        --max-time 15 2>/dev/null || true)

    if echo "$SEND_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
        info "Daily report sent to Telegram"
    else
        error "Telegram send failed: $SEND_RESULT"
        echo "Report content (not sent):"
        echo "$REPORT"
    fi
else
    info "Telegram not configured — printing report to stdout"
    echo ""
    echo "$REPORT"
fi