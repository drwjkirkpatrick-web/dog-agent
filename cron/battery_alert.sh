#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Dog Agent — Battery Alert Cron Script
# ---------------------------------------------------------------------------
# Checks battery voltage from the system and sends a low-battery alert
# if voltage drops below threshold (3.4V for LiPo battery).
#
# Detection strategy:
#   1. /sys/class/power_supply/*/voltage_now (Linux — Pi, Jetson, etc.)
#   2. /usr/sbin/pmset -g batt (macOS)
#   3. Stub/fallback for development environments
#
# Designed to be called by cron every 60 minutes.
#
# Usage:
#   ./cron/battery_alert.sh
#   ./cron/battery_alert.sh --force-low   # Force low battery for testing
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ALERT_API="http://127.0.0.1:9118/alerts"
CONFIG_FILE="${PROJECT_DIR}/config.yaml"

# ─── Thresholds ────────────────────────────────────────────────────────────────
# LiPo 1S: nominal 3.7V, safe min ~3.4V, critical ~3.2V
VOLTAGE_LOW_THRESHOLD=3.4       # Volts — triggers "medium" alert
VOLTAGE_CRITICAL_THRESHOLD=3.2  # Volts — triggers "high" alert

# ─── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; }

# ─── Extract dog name ─────────────────────────────────────────────────────────
DOG_NAME="Fido"
if [ -f "$CONFIG_FILE" ]; then
    CFG_NAME=$(grep 'name:' "$CONFIG_FILE" | head -1 | awk '{print $2}' | tr -d '"')
    [ -n "$CFG_NAME" ] && DOG_NAME="$CFG_NAME"
fi

# ─── Force low-battery test mode ──────────────────────────────────────────────
FORCE_LOW=false
for arg in "$@"; do
    [ "$arg" = "--force-low" ] && FORCE_LOW=true
done

# ─── Read battery voltage ─────────────────────────────────────────────────────
VOLTAGE=""
SOURCE=""

detect_battery() {
    # Strategy 1: Linux power supply class
    if [ -z "$VOLTAGE" ]; then
        for supply in /sys/class/power_supply/*/voltage_now; do
            if [ -f "$supply" ]; then
                local uv
                uv=$(cat "$supply" 2>/dev/null || echo "")
                if [ -n "$uv" ] && [ "$uv" -gt 0 ] 2>/dev/null; then
                    # voltage_now is in microvolts
                    VOLTAGE=$(python3 -c "print($uv / 1_000_000.0)")
                    SOURCE="sysfs:$supply"
                    return
                fi
            fi
        done
    fi

    # Strategy 2: macOS pmset
    if [ -z "$VOLTAGE" ] && command -v pmset &>/dev/null; then
        local batt_info
        batt_info=$(pmset -g batt 2>/dev/null || true)
        if echo "$batt_info" | grep -q "InternalBattery"; then
            # Extract voltage if available (macOS may not expose voltage directly)
            # Fall back to percentage-based estimation
            local pct
            pct=$(echo "$batt_info" | grep -o '[0-9]\+%' | head -1 | tr -d '%')
            if [ -n "$pct" ]; then
                # Rough estimate: 100% = 4.2V, 0% = 3.0V (LiPo curve)
                VOLTAGE=$(python3 -c "print(round(3.0 + ($pct / 100.0) * 1.2, 3))")
                SOURCE="pmset (estimated from $pct%)"
                return
            fi
        fi
    fi

    # Strategy 3: Try reading from I2C battery gauge (common on Pi hats)
    # This is a stub — implement per your hardware
    # if command -v i2cget &>/dev/null; then
    #     ...
    # fi

    # Strategy 4: Fallback — no battery detected
    :
}

# ─── Main ──────────────────────────────────────────────────────────────────────
if [ "$FORCE_LOW" = true ]; then
    info "Force-low mode — simulating low battery (3.3V)"
    VOLTAGE="3.3"
    SOURCE="force-low test"
else
    detect_battery
fi

if [ -z "$VOLTAGE" ]; then
    info "No battery detected — skipping battery check"
    info "(This is normal on a desktop/server without a battery system)"
    exit 0
fi

info "Battery voltage: ${VOLTAGE}V (source: $SOURCE)"

# ─── Compare against thresholds ────────────────────────────────────────────────
ALERT_SEVERITY=""
ALERT_MESSAGE=""

COMPARE=$(python3 -c "
v = float('$VOLTAGE')
low = $VOLTAGE_LOW_THRESHOLD
crit = $VOLTAGE_CRITICAL_THRESHOLD
if v <= crit:
    print('critical')
elif v <= low:
    print('low')
else:
    print('ok')
")

case "$COMPARE" in
    critical)
        ALERT_SEVERITY="high"
        ALERT_MESSAGE="🔴 CRITICAL: $DOG_NAME's battery at ${VOLTAGE}V — below ${VOLTAGE_CRITICAL_THRESHOLD}V threshold! Needs charging immediately!"
        info "CRITICAL low battery: ${VOLTAGE}V"
        ;;
    low)
        ALERT_SEVERITY="medium"
        ALERT_MESSAGE="🟡 Low battery: $DOG_NAME's battery at ${VOLTAGE}V — below ${VOLTAGE_LOW_THRESHOLD}V threshold. Please recharge soon."
        info "Low battery: ${VOLTAGE}V"
        ;;
    ok)
        info "Battery OK (${VOLTAGE}V above ${VOLTAGE_LOW_THRESHOLD}V threshold)"
        exit 0
        ;;
esac

# ─── Send alert via alert_manager API ─────────────────────────────────────────
if [ -n "$ALERT_SEVERITY" ] && [ -n "$ALERT_MESSAGE" ]; then
    info "Sending battery alert to alert_manager..."

    RESULT=$(curl -s -X POST "$ALERT_API" \
        -H "Content-Type: application/json" \
        -d "{
            \"alert_type\": \"battery\",
            \"severity\": \"$ALERT_SEVERITY\",
            \"message\": \"$ALERT_MESSAGE\",
            \"data\": {
                \"voltage\": $VOLTAGE,
                \"threshold_low\": $VOLTAGE_LOW_THRESHOLD,
                \"threshold_critical\": $VOLTAGE_CRITICAL_THRESHOLD,
                \"source\": \"$SOURCE\"
            }
        }" \
        --max-time 10 2>/dev/null || true)

    if echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('alert_type') else 1)" 2>/dev/null; then
        info "Battery alert sent successfully"
    else
        error "Failed to send battery alert: $RESULT"
        # Fallback: print to stdout so cron can mail it
        echo ""
        echo "=== BATTERY ALERT ==="
        echo "$ALERT_MESSAGE"
        echo "====================="
    fi
fi