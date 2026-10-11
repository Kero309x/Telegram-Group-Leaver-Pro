#!/usr/bin/env bash
# ==============================================================================
# Telegram Group & Channel Leaver Pro - Linux/macOS Launcher
# Engineered by @Kero309x (https://github.com/Kero309x)
# ==============================================================================

set -e

echo "==================================================="
echo "   Telegram Group & Channel Leaver Pro (v1.0)"
echo "   Engineered by @Kero309x"
echo "==================================================="
echo ""

# Check python
if ! command -v python3 &> /dev/null; then
    echo "[!] Python3 not found. Please install Python 3.8+."
    exit 1
fi

# Install requirements
echo "[*] Verifying requirements..."
python3 -m pip install -r requirements.txt --quiet

# Open browser if available
if which xdg-open > /dev/null; then
    (sleep 2 && xdg-open "http://localhost:5000") &
elif which open > /dev/null; then
    (sleep 2 && open "http://localhost:5000") &
fi

echo "[+] Starting Web Dashboard on http://localhost:5000"
echo "[!] Press Ctrl+C to terminate."
echo ""
python3 app.py
