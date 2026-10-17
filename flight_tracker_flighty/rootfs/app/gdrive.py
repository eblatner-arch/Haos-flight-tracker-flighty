"""Google Drive backup: your own Google OAuth client (TVs and Limited Input devices type), signed in
with Google's device flow, scope drive.file (it only ever sees the files it creates).

Same setup as the OOOI Flight Logger add-on; the same client ID and secret work for both.
State (refresh token, folder and file IDs) lives in <data>/google.json.
"""

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("flight_tracker_flighty")

SCOPE = "https://www.googleapis.com/auth/drive.file openid email"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UP = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"

LOCK = threading.RLock()
_PATH = None
_CFG = {}
DEVICE = {"pending": None}


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


def init(data_dir, cfg):
    global _PATH, _CFG
    _PATH, _CFG = os.path.join(data_dir, "google.json"), cfg


def configured():
    return bool((_CFG.get("google_client_id") or "").strip() and (_CFG.get("google_client_secret") or "").strip())


def state():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(g):
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(g, f)
    os.replace(tmp, _PATH)


def _update(**kw):
    with LOCK:
        g = state()
        g.update(kw)
        _save(g)
        return g


def _http(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace"))


def _form(url, fields):
    _, body = _http("POST", url, urllib.parse.urlencode(fields).encode(),
                    {"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(body)


# ---------------------------------------------------------------- sign-in

def start_device_flow(on_connected=None):
    if not configured():
        raise ValueError("Add google_client_id and google_client_secret in the add-on Configuration tab, then restart it.")
    res = _form(DEVICE_URL, {"client_id": _CFG["google_client_id"].strip(), "scope": SCOPE})
    p = {"user_code": res["user_code"], "verification_url": res.get("verification_url") or res.get("verification_uri"),
         "expires_at": time.time() + int(res.get("expires_in", 1800)), "interval": int(res.get("interval", 5)),
         "device_code": res["device_code"], "error": None}
    DEVICE["pending"] = p
    threading.Thread(target=_poll, args=(p, on_connected), daemon=True).start()
    return {k: p[k] for k in ("user_code", "verification_url", "expires_at")}


def _poll(p, on_connected):
    interval = p["interval"]
    while time.time() < p["expires_at"] and DEVICE["pending"] is p:
        time.sleep(interval)
        try:
            tok = _form(TOKEN_URL, {"client_id": _CFG["google_client_id"].strip(),
                                    "client_secret": _CFG["google_client_secret"].strip(),
                                    "device_code": p["device_code"],
                                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        except HttpError as e:
            try:
                err = json.loads(e.body).get("error", "")
            except Exception:
                err = ""
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            p["error"] = {"access_denied": "You declined access on the Google page.",
                          "expired_token": "The code expired. Start again."}.get(err, f"Google sign-in failed ({err or e.status}).")
            return
        except Exception as e:
            log.warning("Google device poll: %s", e)
            continue
        email = ""
        if tok.get("id_token"):
            try:
                part = tok["id_token"].split(".")[1]
                email = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
            except Exception:
                pass
        g = state()
        _update(refresh_token=tok.get("refresh_token") or g.get("refresh_token"), access_token=tok.get("access_token"),
                access_exp=time.time() + int(tok.get("expires_in", 3600)) - 60, email=email or g.get("email", ""),
                connected_at=time.time(), last_error=None)
        DEVICE["pending"] = None
        log.info("Google Drive connected%s", f" as {email}" if email else "")
        if on_connected:
            on_connected()
        return
    if DEVICE["pending"] is p and not p["error"]:
        p["error"] = "The code expired. Start again."


def disconnect():
    _update(refresh_token=None, access_token=None, folder_id=None, files={}, email="")


def access_token():
    g = state()
    if not g.get("refresh_token"):
        raise PermissionError("not connected")
    if g.get("access_token") and g.get("access_exp", 0) > time.time():
        return g["access_token"]
    try:
        tok = _form(TOKEN_URL, {"client_id": _CFG["google_client_id"].strip(), "client_secret": _CFG["google_client_secret"].strip(),
                                "refresh_token": g["refresh_token"], "grant_type": "refresh_token"})
    except HttpError as e:
        if e.status in (400, 401):
            _update(refresh_token=None, access_token=None,
                    last_error="Google sign-in expired or was revoked. Connect Google again in Settings.")
            raise PermissionError("revoked")
        raise
    _update(access_token=tok["access_token"], access_exp=time.time() + int(tok.get("expires_in", 3600)) - 60)
    return tok["access_token"]


# ---------------------------------------------------------------- files

def _gd(method, url, token, data=None, ctype=None):
    h = {"Authorization": "Bearer " + token}
    if ctype:
        h["Content-Type"] = ctype
    return _http(method, url, data, h)


def _find(token, name, parent=None, mime=None):
    q = "name = '%s' and trashed = false" % name.replace("\\", "\\\\").replace("'", "\\'")
    if parent:
        q += " and '%s' in parents" % parent
    if mime:
        q += " and mimeType = '%s'" % mime
    _, body = _gd("GET", f"{DRIVE_API}?q={urllib.parse.quote(q)}&fields=files(id,modifiedTime,size)&spaces=drive&pageSize=1", token)
    files = json.loads(body).get("files") or []
    return files[0] if files else None


def _folder(token, name):
    g = state()
    fid = g.get("folder_id")
    if fid:
        try:
            _, body = _gd("GET", f"{DRIVE_API}/{fid}?fields=id,trashed", token)
            if not json.loads(body).get("trashed"):
                return fid
            log.info("Drive: the backup folder is in the trash; making a new one")
        except HttpError as e:
            if e.status != 404:
                raise
    found = _find(token, name, None, "application/vnd.google-apps.folder")
    if found:
        fid = found["id"]
    else:
        _, body = _gd("POST", f"{DRIVE_API}?fields=id", token,
                      json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder"}).encode(), "application/json")
        fid = json.loads(body)["id"]
    _update(folder_id=fid, files={})
    return fid


def _live(token, fid, folder):
    """True only when the file exists, isn't in the trash, and is still in our folder."""
    try:
        _, body = _gd("GET", f"{DRIVE_API}/{fid}?fields=id,trashed,parents", token)
    except HttpError as e:
        if e.status == 404:
            return False
        raise
    m = json.loads(body or b"{}")
    return not m.get("trashed") and folder in (m.get("parents") or [])


def _put(token, folder, name, mime, content):
    """Create or replace one file in the folder (content: bytes)."""
    g = state()
    files = g.get("files") or {}
    fid = files.get(name)
    if fid and not _live(token, fid, folder):
        fid = None  # trashed, deleted, or moved out of the folder: never write into the trash
    if fid:
        _gd("PATCH", f"{DRIVE_UP}/{fid}?uploadType=media&fields=id", token, content, mime)
        return fid
    found = _find(token, name, folder)
    if found:
        _gd("PATCH", f"{DRIVE_UP}/{found['id']}?uploadType=media&fields=id", token, content, mime)
        fid = found["id"]
    else:
        bd = "ft" + secrets.token_hex(8)
        meta = json.dumps({"name": name, "parents": [folder], "mimeType": mime}).encode()
        body = (f"--{bd}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta +
                f"\r\n--{bd}\r\nContent-Type: {mime}\r\n\r\n".encode() + content + f"\r\n--{bd}--".encode())
        _, res = _gd("POST", f"{DRIVE_UP}?uploadType=multipart&fields=id", token, body, f"multipart/related; boundary={bd}")
        fid = json.loads(res)["id"]
    files[name] = fid
    _update(files=files)
    return fid


def upload(files):
    """files: [(name, mime, bytes)]. Returns True when done, False when not connected."""
    try:
        token = access_token()
        folder = _folder(token, (_CFG.get("drive_folder") or "Flight Tracker Flighty (Home Assistant)").strip())
        for name, mime, content in files:
            _put(token, folder, name, mime, content)
        _update(last_backup=time.time(), last_error=None)
        return True
    except PermissionError:
        return False
    except HttpError as e:
        try:
            msg = json.loads(e.body)["error"]["message"]
        except Exception:
            msg = e.body[:200]
        _update(last_error=f"Drive: {msg}")
        raise
    except Exception as e:
        _update(last_error=f"Drive: {str(e)[:200]}")
        raise


def copy(name, new_name):
    """Keep a copy of a file already in the folder under another name (before it gets overwritten)."""
    token = access_token()
    folder = _folder(token, (_CFG.get("drive_folder") or "Flight Tracker Flighty (Home Assistant)").strip())
    found = _find(token, name, folder)
    if not found:
        return False
    _gd("POST", f"{DRIVE_API}/{found['id']}/copy?fields=id", token,
        json.dumps({"name": new_name, "parents": [folder]}).encode(), "application/json")
    return True


def note(**kw):
    _update(**kw)


def download(name):
    """The bytes of one backup file in the folder, or None."""
    token = access_token()
    folder = _folder(token, (_CFG.get("drive_folder") or "Flight Tracker Flighty (Home Assistant)").strip())
    found = _find(token, name, folder)
    if not found:
        return None
    _, body = _gd("GET", f"{DRIVE_API}/{found['id']}?alt=media", token)
    return body


def status():
    g = state()
    p = DEVICE.get("pending")
    return {"configured": configured(), "connected": bool(g.get("refresh_token")), "email": g.get("email") or "",
            "last_backup": g.get("last_backup"), "last_error": g.get("last_error"),
            "folder": (_CFG.get("drive_folder") or "Flight Tracker Flighty (Home Assistant)").strip(),
            "folder_url": f"https://drive.google.com/drive/folders/{g['folder_id']}" if g.get("folder_id") else None,
            "pending": ({k: p.get(k) for k in ("user_code", "verification_url", "expires_at", "error")} if p else None)}
