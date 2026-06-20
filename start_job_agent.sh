#!/usr/bin/env bash
set -e

PROJECT="$(cd "$(dirname "$0")" && pwd)"
PY="$PROJECT/.venv/bin/python"

if [ ! -f "$PY" ]; then
    echo ""
    echo " Virtual environment not found. Run setup first:"
    echo "   chmod +x setup.sh && ./setup.sh"
    exit 1
fi

# Load API key from environment if not already set
if [ -z "$ANTHROPIC_API_KEY" ]; then
    if [ -f "$PROJECT/.env" ]; then
        export $(grep -v '^#' "$PROJECT/.env" | xargs)
    fi
fi

echo ""
echo " ╔════════════════════════════════════════════╗"
echo " ║   JOB AGENT — Starting...                 ║"
echo " ║   Open http://localhost:8000 in Chrome     ║"
echo " ║   Press Ctrl+C to stop.                   ║"
echo " ╚════════════════════════════════════════════╝"
echo ""

cd "$PROJECT"
"$PY" -m uvicorn web.backend.main:app --host 0.0.0.0 --port 8000
