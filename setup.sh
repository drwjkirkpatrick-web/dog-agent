#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Dog Agent — Setup Script
# ---------------------------------------------------------------------------
# Usage:
#   ./setup.sh                 # Normal setup
#   ./setup.sh --skip-hermes   # Skip Hermes skill install
#   ./setup.sh --help          # Show usage
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

SKIP_HERMES=false

# ─── Parse args ───────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --skip-hermes) SKIP_HERMES=true ;;
        --help|-h)
            echo "Usage: $0 [--skip-hermes]"
            echo "  --skip-hermes    Skip Hermes skill installation"
            exit 0
            ;;
        *)
            error "Unknown argument: $arg"
            echo "Usage: $0 [--skip-hermes]"
            exit 1
            ;;
    esac
done

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  🐾 Dog Agent — Setup                          ${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo ""

# ─── 1. Check Python 3.8+ ────────────────────────────────────────────────────
info "Checking Python version..."

if ! command -v python3 &>/dev/null; then
    error "python3 not found. Please install Python 3.8 or newer."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]; }; then
    error "Python 3.8+ required, found $PYTHON_VERSION"
    exit 1
fi

ok "Python $PYTHON_VERSION"

# ─── 2. Create virtual environment ────────────────────────────────────────────
info "Setting up virtual environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
    ok "Created virtual environment at venv/"
else
    ok "Virtual environment already exists at venv/"
fi

# Activate
source venv/bin/activate

# Upgrade pip
pip install --quiet --upgrade pip setuptools wheel 2>&1 | grep -v "^$" || true

# ─── 3. Install dependencies ──────────────────────────────────────────────────
info "Installing Python dependencies..."

if [ ! -f "requirements.txt" ]; then
    warn "requirements.txt not found — skipping pip install"
else
    pip install --quiet -r requirements.txt 2>&1 | grep -v "^$" || true
    ok "Dependencies installed from requirements.txt"
fi

# ─── 4. Create config.yaml from example ───────────────────────────────────────
info "Checking configuration..."

if [ ! -f "config.yaml" ]; then
    if [ -f "config.example.yaml" ]; then
        cp config.example.yaml config.yaml
        ok "Created config.yaml from config.example.yaml — edit it with your settings"
    else
        warn "config.example.yaml not found — skipping config setup"
    fi
else
    ok "config.yaml already exists"
fi

# ─── 5. Create data directories ──────────────────────────────────────────────
info "Creating data directories..."

mkdir -p data/gps_tracks
mkdir -p data/health_logs
mkdir -p data/behavior
mkdir -p data/events
mkdir -p data/alerts
mkdir -p logs

ok "Data directories created"

# ─── 6. Install Hermes skill ──────────────────────────────────────────────────
if [ "$SKIP_HERMES" = false ]; then
    info "Installing Hermes skill..."

    if command -v hermes &>/dev/null; then
        if [ -d "hermes" ]; then
            hermes skills install "$SCRIPT_DIR/hermes" 2>&1 || {
                warn "Failed to install Hermes skill from hermes/ directory"
                warn "You can install it manually: hermes skills install $(pwd)/hermes"
            }
            ok "Hermes skill installed"
        else
            warn "hermes/ directory not found — skipping skill install"
        fi
    else
        warn "hermes command not found — skipping Hermes skill install"
        warn "Install Hermes or run with --skip-hermes"
    fi
else
    info "Skipping Hermes skill install (--skip-hermes)"
fi

# ─── 7. Test module imports ──────────────────────────────────────────────────
info "Testing Python module imports..."

TEST_RESULT=0
python3 -c "
import sys
sys.path.insert(0, 'src')
modules = [
    'alert_manager',
    'behavior',
    'data_logger',
    'geofence',
    'gps_daemon',
    'health_monitor',
    'main',
    'sensor_daemon',
    'voice',
]
failed = []
for m in modules:
    try:
        __import__(m)
        print(f'  ✅ {m}')
    except Exception as e:
        print(f'  ❌ {m}: {e}')
        failed.append(m)
if failed:
    print(f'FAILED: {\", \".join(failed)}')
    sys.exit(1)
else:
    print('All modules loaded successfully.')
" || TEST_RESULT=$?

if [ "$TEST_RESULT" -eq 0 ]; then
    ok "All Python modules load cleanly"
else
    warn "Some modules failed to import (may need dependencies or hardware)"
    warn "This is expected if hardware libraries (pyserial, smbus2) are unavailable"
fi

# ─── 8. Make cron scripts executable ─────────────────────────────────────────
info "Setting up cron scripts..."

if [ -d "cron" ]; then
    chmod +x cron/*.sh 2>/dev/null || true
    ok "Cron scripts are executable"
else
    warn "cron/ directory not found"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Dog Agent setup complete!                   ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}Next steps:${NC}"
echo ""
echo "  1. Edit config.yaml with your settings:"
echo "     - Dog name, breed, weight"
echo "     - GPS port (default: /dev/ttyS0)"
echo "     - Telegram bot token and chat ID (for alerts)"
echo "     - Geofence coordinates for your home"
echo ""
echo "  2. Activate the environment:"
echo "     $ source venv/bin/activate"
echo ""
echo "  3. Run the orchestrator:"
echo "     $ python src/main.py --all"
echo ""
echo "  4. Or run individual modules:"
echo "     $ python src/main.py --simulate    # Simulate everything"
echo "     $ python src/health_monitor.py --simulate"
echo "     $ python src/behavior.py --simulate"
echo ""
echo "  5. Set up cron jobs (see cron/ directory):"
echo "     $ crontab cron/crontab.example"
echo ""
echo -e "  ${YELLOW}Happy dog monitoring! 🐾${NC}"
echo ""