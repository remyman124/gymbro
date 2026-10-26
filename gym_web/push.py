"""v3.3.44 — Web Push helpers (VAPID keys + subscription store).

Importing this module must never raise, never touch the network and never
read the VAPID private key at import time — the keys are loaded lazily at
call time from a file OUTSIDE the repo.

The VAPID private key lives at ``/home/work/.hermes/gymbro_vapid.json``
(mode 0600, outside git). It must never be written into the repo, into a
log line, or into a commit.

Store format (``GYMBRO_PUSH_STORE``, default
``/home/work/.hermes/gymbro_push_subs.json``)::

    {"subs": [{"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."},
               "created": "<iso>", "last_ok": <ts>, "last_error": ""}]}
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from gym_web.core import safe_write_json

VAPID_PATH = "/home/work/.hermes/gymbro_vapid.json"
PUSH_STORE_ENV = "GYMBRO_PUSH_STORE"
DEFAULT_PUSH_STORE = "/home/work/.hermes/gymbro_push_subs.json"

# VAPID "sub" claim — the push service uses it as the operator contact.
# py_vapid rejects anything that is not a mailto: address with a dotted
# domain, so this has to look like a real (placeholder) address.
VAPID_SUB_ENV = "GYMBRO_VAPID_SUB"
DEFAULT_VAPID_SUB = "mailto:jim@example.com"

MAX_ENDPOINT_LEN = 2048
MAX_PAYLOAD_BYTES = 4096

HKT = timezone(timedelta(hours=8))

_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    """Print a warning the first time only (avoids log spam per request)."""
    with _LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    print(f"[push] {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(HKT).isoformat()


# ---------- Paths ----------


def push_store_path() -> Path:
    """Resolve the subscription store path (env-overridable for tests)."""
    return Path(os.environ.get(PUSH_STORE_ENV) or DEFAULT_PUSH_STORE)


# ---------- VAPID ----------


def load_vapid() -> dict | None:
    """Load the VAPID key material. Any failure → None (never raises)."""
    try:
        raw = Path(VAPID_PATH).read_text()
        data = json.loads(raw)
    except Exception as e:
        print(f"[push.load_vapid] cannot read VAPID keys from {VAPID_PATH}: "
              f"{type(e).__name__}: {e}", flush=True)
        return None
    if not isinstance(data, dict):
        print(f"[push.load_vapid] {VAPID_PATH} is not a JSON object", flush=True)
        return None
    if not data.get("private_key"):
        print(f"[push.load_vapid] {VAPID_PATH} has no private_key", flush=True)
        return None
    return data


def _derive_public_key(private_key: str) -> str:
    """Derive the base64url P-256 public key that matches ``private_key``.

    The VAPID public key is not an independent secret — it IS the public
    half of the private key. The browser must subscribe with this exact
    value, otherwise the push service rejects the signed request.
    """
    if not private_key:
        return ""
    try:
        from py_vapid import Vapid, b64urlencode
        from cryptography.hazmat.primitives import serialization

        vap = Vapid.from_string(private_key=private_key)
        raw = vap.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return b64urlencode(raw)
    except Exception as e:
        print(f"[push._derive_public_key] cannot derive public key: "
              f"{type(e).__name__}: {e}", flush=True)
        return ""


def vapid_public_key() -> str:
    """base64url VAPID public key for the frontend. "" when unavailable.

    The value derived from the private key always wins — a stored
    ``public_key`` that disagrees with the private key is broken and would
    make every push fail with 401/403.
    """
    vap = load_vapid()
    if not vap:
        return ""
    derived = _derive_public_key(str(vap.get("private_key") or ""))
    stored = str(vap.get("public_key") or "")
    if derived and stored and derived != stored:
        _warn_once(
            "vapid-mismatch",
            f"VAPID public_key in {VAPID_PATH} does NOT match its private_key "
            f"(stored is {len(stored)} chars, derived is {len(derived)} chars). "
            f"Using the DERIVED key so pushes can actually be authorised. "
            f"Please regenerate {VAPID_PATH}.",
        )
    return derived or stored


# ---------- Subscription store ----------


def _read_store() -> dict:
    path = push_store_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"subs": []}
    except Exception as e:
        print(f"[push._read_store] cannot read {path}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {"subs": []}
    if not isinstance(data, dict):
        return {"subs": []}
    if not isinstance(data.get("subs"), list):
        data["subs"] = []
    return data


def _write_store(store: dict) -> bool:
    path = push_store_path()
    ok = safe_write_json(path, store)
    if not ok:
        print(f"[push._write_store] atomic write failed for {path}", flush=True)
    return ok


def list_subscriptions() -> list[dict]:
    """All stored subscriptions (may be empty)."""
    with _LOCK:
        subs = _read_store().get("subs", [])
        return [s for s in subs if isinstance(s, dict)]


def save_subscription(sub: dict) -> dict:
    """Store/replace a subscription keyed by endpoint. Returns the record.

    Raises ValueError on a malformed subscription — callers validate first.
    """
    sub = sub or {}
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "")
    auth = str(keys.get("auth") or "")
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LEN:
        raise ValueError("bad endpoint")
    if not p256dh or not auth:
        raise ValueError("bad keys")

    record = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
        "created": _now_iso(),
        "last_ok": 0,
        "last_error": "",
    }
    with _LOCK:
        store = _read_store()
        subs = store.get("subs", [])
        for i, existing in enumerate(subs):
            if isinstance(existing, dict) and existing.get("endpoint") == endpoint:
                record["created"] = existing.get("created") or record["created"]
                subs[i] = record
                break
        else:
            subs.append(record)
        store["subs"] = subs
        _write_store(store)
    return record


def remove_subscription(endpoint: str) -> bool:
    """Drop one subscription. True when something was actually removed."""
    endpoint = str(endpoint or "")
    if not endpoint:
        return False
    with _LOCK:
        store = _read_store()
        subs = store.get("subs", [])
        keep = [s for s in subs
                if not (isinstance(s, dict) and s.get("endpoint") == endpoint)]
        removed = len(keep) != len(subs)
        if removed:
            store["subs"] = keep
            _write_store(store)
        return removed


def mark_result(endpoint: str, ok: bool, error: str = "") -> None:
    """Record the last delivery outcome on a subscription."""
    with _LOCK:
        store = _read_store()
        changed = False
        for sub in store.get("subs", []):
            if isinstance(sub, dict) and sub.get("endpoint") == endpoint:
                if ok:
                    sub["last_ok"] = int(time.time())
                    sub["last_error"] = ""
                else:
                    sub["last_error"] = str(error)[:500]
                changed = True
                break
        if changed:
            _write_store(store)


# ---------- Delivery ----------


def _http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction from a pywebpush failure."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    if match:
        return int(match.group(1))
    return None


def send_push_detailed(sub: dict, payload: str, ttl: int = 86400,
                       urgency: str = "normal") -> tuple[bool, str, int | None]:
    """Send one push. Returns (ok, error_message, http_status_or_None).

    Never raises — a broken subscription must not take the caller down.
    """
    vap = load_vapid()
    if not vap:
        return (False, "vapid keys unavailable", None)

    try:
        from pywebpush import webpush
    except Exception as e:
        print(f"[push.send_push] pywebpush is not importable: "
              f"{type(e).__name__}: {e}", flush=True)
        return (False, f"pywebpush unavailable: {e}", None)

    sub = sub or {}
    keys = sub.get("keys") or {}
    info = {
        "endpoint": str(sub.get("endpoint") or ""),
        "keys": {
            "p256dh": str(keys.get("p256dh") or ""),
            "auth": str(keys.get("auth") or ""),
        },
    }
    if not info["endpoint"] or not info["keys"]["p256dh"] or not info["keys"]["auth"]:
        return (False, "incomplete subscription", None)

    try:
        webpush(
            subscription_info=info,
            data=payload,
            vapid_private_key=vapid["private_key"],
            vapid_claims={"sub": os.environ.get(VAPID_SUB_ENV) or DEFAULT_VAPID_SUB},
            headers={"Urgency": str(urgency)},
            ttl=int(ttl),
        )
        return (True, "", 200)
    except Exception as e:
        status = _http_status(e)
        msg = str(e)
        print(f"[push.send_push] delivery failed (status={status}): {msg}",
              flush=True)
        return (False, msg, status)


def send_push(sub: dict, payload: str, ttl: int = 86400,
              urgency: str = "normal") -> tuple[bool, str]:
    """Send one push. Returns (ok, error_message). Never raises."""
    ok, msg, _status = send_push_detailed(sub, payload, ttl=ttl, urgency=urgency)
    return (ok, msg)


def build_payload(title: str, body: str, url: str = "/",
                  tag: str = "gymbro") -> str:
    """JSON payload for the Service Worker. Raises ValueError if too big."""
    payload = json.dumps(
        {
            "title": str(title or ""),
            "body": str(body or ""),
            "url": str(url or "/"),
            "tag": str(tag or "gymbro"),
        },
        ensure_ascii=False,
    )
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too big")
    return payload
