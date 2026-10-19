"""United 737 side views in the current (2019 "blue evolution") livery, by Kyle's AI Works
(kyles-ai-works.com, free AI-traffic repaints for flight simulators, shown with his preview pictures).

The pictures aren't shipped with the add-on: on first start the add-on downloads them from his site into
/data/livery, cuts them out (transparent background, trimmed), and keeps only the ones in the current
livery (blue engines, no gold stripe), so an older-livery picture is never shown. The app then uses the
picture of the exact tail when there is one, else one of the same type.
"""
import io
import json
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("livery")

SITE = "https://kyles-ai-works.com/wp-content/uploads/"
# type key -> picture names on his site (UAL-<name>.png); the name starts with the registration
SETS = {
    "b737": ["N24706", "N16709", "N13720"],
    "b738": ["N87527", "N78540", "N33203", "N37267", "N76522", "N14219", "N78285", "N77536", "N76269", "N33294"],
    "b739": ["N38454", "N68805", "N38424", "N73445", "N75435", "N35407", "N38403"],
    "b38m": ["N27252-2", "N27255-1", "N77259-1", "N27261-1"],
    "b39m": ["N37523-1", "N37506-1"],
}
WIDTH = 900

_lock = threading.Lock()
_state = {"tails": {}, "types": {}, "skipped": {}, "ready": False, "at": 0}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (flight-tracker add-on)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cut_out(im):
    """RGBA, background made transparent (if it isn't already), trimmed, WIDTH wide."""
    from PIL import ImageDraw
    im = im.convert("RGBA")
    w, h = im.size
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    if any(im.getpixel(c)[3] > 200 for c in corners):   # a solid background: flood it away from the corners
        for c in corners:
            p = im.getpixel(c)
            if p[3] > 200 and min(p[:3]) > 225:
                ImageDraw.floodfill(im, c, (0, 0, 0, 0), thresh=18)
    box = im.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
    if box:
        im = im.crop(box)
    if im.width != WIDTH:
        from PIL import Image
        im = im.resize((WIDTH, max(1, round(im.height * WIDTH / im.width))), Image.LANCZOS)
    return im


def classify(im):
    """'new' (blue engines, no gold stripe), 'old' (2010 livery: gold stripe, grey engines) or '?'."""
    im = im.convert("RGBA")
    small = im.resize((300, max(1, round(im.height * 300 / im.width))))
    w, h = small.size
    gold = blue_low = solid = 0
    px = small.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 200:
                continue
            solid += 1
            if r > 120 and r - b > 40 and r >= g > b:   # the old livery's gold (stripe, tail globe)
                gold += 1
            if y > h * 0.55 and b > 120 and b - r > 70 and b - g > 40:
                blue_low += 1
    if not solid:
        return "?"
    if gold / solid > 0.003:
        return "old"
    if blue_low / solid > 0.008:
        return "new"
    return "?"


def path(d, tail):
    return os.path.join(d, f"{tail}.png")


def _save_index(d):
    with open(os.path.join(d, "index.json.tmp"), "w") as f:
        json.dump({k: _state[k] for k in ("tails", "types", "skipped", "at")}, f)
    os.replace(os.path.join(d, "index.json.tmp"), os.path.join(d, "index.json"))


def load(d):
    try:
        with open(os.path.join(d, "index.json")) as f:
            j = json.load(f)
        with _lock:
            _state.update(j)
            _state["ready"] = True
    except (OSError, ValueError):
        pass


def fetch_all(d, force=False):
    """Download, cut out and sort the pictures (once; again only for ones still missing)."""
    from PIL import Image
    os.makedirs(d, exist_ok=True)
    load(d)
    tails, types, skipped = dict(_state["tails"]), {}, dict(_state["skipped"])
    for key, names in SETS.items():
        for name in names:
            tail = name.split("-")[0].upper()
            if not force and (tail in skipped or (tail in tails and os.path.exists(path(d, tail)))):
                if tail in tails:
                    types.setdefault(key, []).append(tail)
                continue
            raw = None
            for url in (f"{SITE}UAL-{name}.png", f"https://i0.wp.com/kyles-ai-works.com/wp-content/uploads/UAL-{name}.png?w=1400"):
                try:
                    raw = _get(url)
                    break
                except Exception as e:  # noqa: BLE001
                    log.info("livery %s: %s (%s)", name, e, url)
            if not raw:
                continue
            try:
                im = _cut_out(Image.open(io.BytesIO(raw)))
            except Exception as e:  # noqa: BLE001
                log.warning("livery %s: can't read the picture (%s)", name, e)
                continue
            kind = classify(im)
            if kind != "new":
                skipped[tail] = kind
                log.info("livery %s: not the current livery (%s), not used", tail, kind)
                continue
            im.save(path(d, tail) + ".tmp", "PNG", optimize=True)
            os.replace(path(d, tail) + ".tmp", path(d, tail))
            tails[tail] = key
            types.setdefault(key, []).append(tail)
            time.sleep(0.5)
    with _lock:
        _state.update({"tails": tails, "types": types, "skipped": skipped, "ready": True, "at": time.time()})
        _save_index(d)
    log.info("liveries: %d pictures (%s), %d skipped", len(tails),
             ", ".join(f"{k} {len(v)}" for k, v in types.items()), len(skipped))


def index():
    with _lock:
        return {"tails": dict(_state["tails"]), "types": {k: list(v) for k, v in _state["types"].items()},
                "skipped": dict(_state["skipped"]), "ready": _state["ready"]}


def start(d):
    def run():
        for attempt in range(6):   # no internet yet at boot: try again later
            try:
                fetch_all(d)
                if _state["tails"]:
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("liveries: %s", e)
            time.sleep(600 * (attempt + 1))
    load(d)
    threading.Thread(target=run, daemon=True, name="livery").start()
