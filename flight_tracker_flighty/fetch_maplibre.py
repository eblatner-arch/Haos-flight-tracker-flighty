"""Download the MapLibre GL build the web app uses (build time). usage: fetch_maplibre.py VERSION DIR"""
import os
import sys
import urllib.request

ver, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
for f in ("maplibre-gl.js", "maplibre-gl.css"):
    for base in ("https://cdn.jsdelivr.net/npm", "https://unpkg.com"):
        try:
            data = urllib.request.urlopen(f"{base}/maplibre-gl@{ver}/dist/{f}", timeout=60).read()
        except Exception as e:
            print(f"{base}: {f}: {e}")
            continue
        if len(data) > 1000:
            with open(os.path.join(out, f), "wb") as fh:
                fh.write(data)
            print(f"{f}: {len(data)} bytes from {base}")
            break
    else:
        sys.exit(f"couldn't download {f}")
