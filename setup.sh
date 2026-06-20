#!/usr/bin/env bash
set -e

echo ""
echo " ╔═══════════════════════════════════════════════════╗"
echo " ║   🤖  JOB AGENT — SETUP WIZARD                   ║"
echo " ║       AI-powered job hunting, on autopilot        ║"
echo " ╚═══════════════════════════════════════════════════╝"
echo ""

PROJECT="$(cd "$(dirname "$0")" && pwd)"

# ── Check Python ──────────────────────────────────────────────────────────────
echo " [1/4] Checking Python..."
if ! command -v python3 &>/dev/null; then
    echo ""
    echo " ERROR: Python 3 not found."
    echo " Install from: https://www.python.org/downloads/"
    echo " Or via Homebrew: brew install python"
    exit 1
fi
PYVER=$(python3 -c 'import sys; print(sys.version_info >= (3,10))')
if [ "$PYVER" != "True" ]; then
    echo " ERROR: Python 3.10+ required."
    exit 1
fi
echo " Found: $(python3 --version)"

# ── Check Chrome ──────────────────────────────────────────────────────────────
echo " [2/4] Checking for Google Chrome..."
CHROME_FOUND=0
if [ -f "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then CHROME_FOUND=1; fi
if command -v google-chrome &>/dev/null; then CHROME_FOUND=1; fi
if command -v chromium-browser &>/dev/null; then CHROME_FOUND=1; fi

if [ "$CHROME_FOUND" -eq 0 ]; then
    echo ""
    echo " WARNING: Google Chrome not found."
    echo " Download: https://www.google.com/chrome/"
    echo " You can continue and install Chrome separately."
    echo ""
    read -rp " Press Enter to continue..."
else
    echo " Chrome found."
fi

# ── Create virtual environment ────────────────────────────────────────────────
echo " [3/4] Setting up Python environment..."
if [ ! -d "$PROJECT/.venv" ]; then
    echo " Creating virtual environment..."
    python3 -m venv "$PROJECT/.venv"
fi
source "$PROJECT/.venv/bin/activate"
PY="$PROJECT/.venv/bin/python"

echo " Installing dependencies..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$PROJECT/requirements.txt" -r "$PROJECT/web/requirements.txt"

echo " Installing Playwright browser..."
"$PY" -m playwright install chromium

# ── Run wizard ────────────────────────────────────────────────────────────────
echo " [4/4] Running setup wizard..."
echo ""
cd "$PROJECT"
"$PY" setup_wizard.py

echo ""
echo " ╔═══════════════════════════════════════════════════╗"
echo " ║   SETUP COMPLETE                                  ║"
echo " ║   Run: ./start_job_agent.sh to launch.            ║"
echo " ╚═══════════════════════════════════════════════════╝"
echo ""
