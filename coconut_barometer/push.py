"""Web Push: notifications to the browsers and phones turned on in Settings → Alerts (alerts.py decides what).

The VAPID key pair is made on first use; its private half is kept in the meta table encrypted, like the API
keys (settings.encrypt), so a database copy alone can't send as this server. Each device's subscription
(its push service's endpoint and the keys to encrypt for it) is kept beside it, with who turned it on. A
push service that says a subscription is gone (404 / 410) has it removed: that device was unsubscribed or
the browser's data cleared.

On iPhone and iPad, Web Push only reaches the app added to the Home Screen (iOS 16.4+), not Safari."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time

from cryptography.hazmat.primitives import serialization

from . import local

logger = logging.getLogger(__name__)
KEY_META, SUBS_META = "push_vapid", "push_subscriptions"
TTL = 4 * 3600          # a phone that's off for longer than this doesn't get a stale alert when it's back


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _vapid(store):
    """This server's VAPID key (py_vapid), made and saved the first time."""
    from py_vapid import Vapid02

    from .settings import decrypt, encrypt

    saved = json.loads(store.get_meta(KEY_META) or "null")
    if saved:
        pem = decrypt(KEY_META, base64.b64decode(saved["nonce"]), base64.b64decode(saved["key"]))
        if pem is None:
            raise RuntimeError("the push key can't be decrypted (a database moved without its secret.key?): "
                               "turn alerts off and on again on each device")
        return Vapid02.from_pem(pem.encode())
    v = Vapid02()
    v.generate_keys()
    nonce, key = encrypt(KEY_META, v.private_pem().decode())
    store.set_meta(KEY_META, json.dumps({"nonce": base64.b64encode(nonce).decode(), "key": base64.b64encode(key).decode()}))
    return v


def public_key(store) -> str:
    """The applicationServerKey a browser subscribes with (the raw P-256 point, base64url)."""
    return _b64(_vapid(store).public_key.public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint))


def _contact(origin: str | None) -> str:
    """VAPID's `sub`: the site, if it's https (Apple refuses anything else that isn't a mailto:)."""
    return origin if origin and origin.startswith("https://") else "mailto:alerts@example.com"


def device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def devices(store) -> list[dict]:
    return json.loads(store.get_meta(SUBS_META) or "[]")


def _save(store, subs: list[dict]) -> None:
    store.set_meta(SUBS_META, json.dumps(subs))


def subscribe(store, subscription: dict, username: str, label: str, origin: str) -> dict:
    """Add (or refresh) a device. `subscription` is the browser's PushSubscription.toJSON()."""
    endpoint, keys = subscription.get("endpoint") or "", subscription.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("not a push subscription")
    sub = {"id": device_id(endpoint), "endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
           "username": username, "label": label[:80], "origin": origin, "added": int(time.time())}
    _save(store, [s for s in devices(store) if s["id"] != sub["id"]] + [sub])
    return sub


def unsubscribe(store, id_: str) -> bool:
    subs = devices(store)
    kept = [s for s in subs if s["id"] != id_]
    _save(store, kept)
    return len(kept) < len(subs)


def send(store, title: str, body: str, url: str = "/", tag: str = "", only: str | None = None) -> dict:
    """To every device (or just `only`, an id). Returns {"sent": n, "failed": [errors], "removed": n}; in local
    mode (local.py) nothing is sent, and it says so ("local": True)."""
    if local.on():
        local.held(f"sending a push notification: {title}")
        return {"sent": 0, "failed": [], "removed": 0, "local": True}
    from pywebpush import WebPushException, webpush

    subs = [s for s in devices(store) if only is None or s["id"] == only]
    if not subs:
        return {"sent": 0, "failed": [], "removed": 0}
    vapid, payload = _vapid(store), json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent, failed, gone = 0, [], set()
    for s in subs:
        try:
            webpush({"endpoint": s["endpoint"], "keys": s["keys"]}, payload, vapid_private_key=vapid,
                    vapid_claims={"sub": _contact(s.get("origin"))}, ttl=TTL, timeout=15)
            sent += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                gone.add(s["id"])
            else:
                failed.append(f"{s['label']}: {code or ''} {e.message if hasattr(e, 'message') else e}".strip())
        except Exception as e:   # noqa: BLE001 - one device's push service being down doesn't stop the others
            failed.append(f"{s['label']}: {type(e).__name__}: {e}")
    if gone:
        _save(store, [s for s in devices(store) if s["id"] not in gone])
    for f in failed:
        logger.warning("push not delivered: %s", f)
    logger.info("pushed %r to %d device(s)%s", title, sent, f", {len(gone)} gone" if gone else "")
    return {"sent": sent, "failed": failed, "removed": len(gone)}
