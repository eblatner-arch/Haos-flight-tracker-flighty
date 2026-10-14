#!/bin/bash
export DISPLAY=:99
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
sleep 2

VIEWER=$(jq -r '.enable_viewer' /data/options.json)
PW=$(jq -r '.viewer_password // empty' /data/options.json)

if [ "$VIEWER" = "true" ]; then
  if [ -z "$PW" ]; then
    echo "Viewer enabled but no viewer_password set; skipping viewer."
  else
    x11vnc -display :99 -forever -shared -quiet -passwd "$PW" -rfbport 5900 &
    websockify --web /usr/share/novnc 6080 localhost:5900 &
    echo "Viewer: http://<your-ha-ip>:6080/vnc.html"
  fi
fi

exec python3 -u /run.py
