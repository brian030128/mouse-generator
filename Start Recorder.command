#!/bin/bash
# Double-click in Finder to open the recorder; it keeps running after this window closes.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3.10 or newer from python.org (includes Tk)."
    read -r -p "Press Return to close."
    exit 1
fi
nohup python3 recorder.py --start >/dev/null 2>&1 &
disown
osascript -e 'tell application "Terminal" to close (every window whose name contains "Start Recorder")' >/dev/null 2>&1 &
exit 0
