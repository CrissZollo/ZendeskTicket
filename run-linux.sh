#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10 or newer is required."
    echo "Install via your package manager, e.g.:"
    echo "  sudo apt install python3        # Debian/Ubuntu"
    echo "  sudo dnf install python3        # Fedora/RHEL"
    read -p "Press Enter to exit..."
    exit 1
fi
python3 zdweb.py
