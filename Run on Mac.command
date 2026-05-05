#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    osascript -e 'display dialog "Python 3.10 or newer is required.\n\nInstall it from python.org/downloads — or, if you have Homebrew: brew install python" buttons {"OK"} default button 1 with icon caution'
    exit 1
fi
python3 zdweb.py
