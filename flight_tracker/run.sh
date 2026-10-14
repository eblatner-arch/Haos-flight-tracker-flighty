#!/bin/bash
OPTS=/data/options.json

# Virtual display for the headed browser (sites block headless Chrome more often).
export DISPLAY=:99
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
sleep 2

if [ "$(jq -r '.enable_viewer' $OPTS)" = "true" ]; then
  PW=$(jq -r '.viewer_password // empty' $OPTS)
  if [ -z "$PW" ]; then
    echo "Viewer enabled but no viewer_password set; skipping the viewer."
  else
    x11vnc -display :99 -forever -shared -quiet -passwd "$PW" -rfbport 5900 &
    websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    echo "Browser viewer: http://<your-ha-ip>:6080/vnc.html"
  fi
fi

cd /app
exec python3 -u /app/server.py
