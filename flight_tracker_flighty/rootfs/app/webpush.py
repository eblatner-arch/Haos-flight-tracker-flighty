"""Web Push (the standard behind notifications for Home Screen web apps on iOS 16.4+, Android and
desktop browsers): encrypt a message for one subscription (RFC 8291, aes128gcm) and sign the request
with this add-on's VAPID key (RFC 8292). Apple, Google or Mozilla deliver it to the device.

The key pair is made once and kept in /data, so phones that subscribed keep working across updates.
"""
import base64
import json
import os
import struct
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_pub(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


class Vapid:
    def __init__(self, path):
        self.path = path
        self.key = None
        try:
            with open(path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), password=None)
        except (OSError, ValueError):
            self.key = ec.generate_private_key(ec.SECP256R1())
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
            os.replace(path + ".tmp", path)

    @property
    def public(self):
        """The application server key the page subscribes with (base64url, uncompressed point)."""
        return b64u(_raw_pub(self.key))

    def header(self, endpoint, sub="https://github.com/eblatner-arch/haos-flight-tracker-flighty"):
        u = urllib.parse.urlsplit(endpoint)
        aud = f"{u.scheme}://{u.netloc}"
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": sub}, separators=(",", ":")).encode())
        msg = f"{head}.{body}".encode()
        r, s = decode_dss_signature(self.key.sign(msg, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"vapid t={head}.{body}.{b64u(sig)}, k={self.public}"


def _hkdf(salt, ikm, info, n):
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def encrypt(sub_keys, data):
    """RFC 8291 aes128gcm: the body to POST to the subscription's endpoint."""
    ua_pub = unb64u(sub_keys["p256dh"])
    auth = unb64u(sub_keys["auth"])
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw_pub(eph)
    shared = eph.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ct = AESGCM(cek).encrypt(nonce, data + b"\x02", None)   # one record, last-record padding delimiter
    rs = 4096
    return salt + struct.pack("!IB", rs, len(as_pub)) + as_pub + ct


def send(vapid, sub, payload, ttl=3600, urgency="high", topic=None, timeout=15):
    """POST one encrypted message. -> HTTP status (201/200 delivered; 404/410 the phone unsubscribed)."""
    body = encrypt(sub["keys"], json.dumps(payload, separators=(",", ":")).encode())
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
               "Urgency": urgency, "Authorization": vapid.header(sub["endpoint"])}
    if topic:   # a newer message with the same topic replaces one not yet delivered
        headers["Topic"] = "".join(c for c in topic if c.isalnum() or c in "-_")[:32]
    req = urllib.request.Request(sub["endpoint"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
