"""Settings in layers, and the API keys kept encrypted.

Lowest to highest:  DEFAULTS  <  config.yaml  <  the admin page (database)  <  environment

The admin page writes the third layer; CLI and web both read all four (load_config).
The settings that protect the server itself (LOCKED) can't be changed from the page.

API keys (SECRETS) set on the page are stored AES-256-GCM encrypted, each bound to its
setting name. The encryption key never goes in the database: it comes from the
FLICKR_ORGANIZER_SECRET_KEY environment variable, or a key file (default
~/.config/flickr-organizer/secret.key, created on first use, readable only by you;
FLICKR_ORGANIZER_KEY_FILE moves it; on a server it's secret.key on the data disk). A copy of the database alone doesn't reveal them.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets as pysecrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import CHOICES, COLORS, DEFAULTS, ENV_OVERRIDES, FREE_MAPS, ITEM_KEYS, OPTIONAL, RENAMED, _merge, validate

SECRETS = ("flickr.api_key", "flickr.api_secret", "vision.api_key", "weatherflow.token")
LOCKED = {"database": "where the database is", "web.host": "what address the server listens on",
          "web.port": "what port the server listens on", "web.allowed_hosts": "which host names are accepted",
          "web.trusted_proxies": "whose X-Forwarded-For headers are believed",
          "web.secure_cookies": "whether the login cookie needs HTTPS"}
KEY_ENV, KEY_FILE_ENV = "FLICKR_ORGANIZER_SECRET_KEY", "FLICKR_ORGANIZER_KEY_FILE"
DEFAULT_KEY_FILE = "~/.config/flickr-organizer/secret.key"


class SettingsError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---- paths ---------------------------------------------------------------------------
def leaves(node: dict = DEFAULTS, prefix: tuple = ()) -> list[str]:
    """Every editable setting as a dotted path. Maps whose keys are yours (categories,
    aliases ...) are one setting each."""
    out = []
    for k, v in node.items():
        here = prefix + (k,)
        if isinstance(v, dict) and here not in FREE_MAPS:
            out += leaves(v, here)
        else:
            out.append(".".join(here))
    return out


def get_path(raw: dict, path: str, default=None):
    node = raw
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def set_path(raw: dict, path: str, value) -> None:
    keys = path.split(".")
    node = raw
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


# ---- encryption ------------------------------------------------------------------------
def _key_from_text(text: str) -> bytes:
    """A 32-byte key, as url-safe or standard base64 (`settings new-key`, a key file, or Render's
    generateValue). Anything else (at least 32 characters) is turned into one with SHA-256."""
    text = text.strip()
    for decode in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decode(text + "=" * (-len(text) % 4))
        except (ValueError, TypeError):
            continue
        if len(key) == 32:
            return key
    if len(text) < 32:
        raise SystemExit(f"{KEY_ENV} is too short: use `flickr-organizer settings new-key` (or 32+ random characters)")
    return hashlib.sha256(text.encode()).digest()


def key_file() -> Path:
    """FLICKR_ORGANIZER_KEY_FILE, else secret.key in the data directory (on a server, the
    disk, so it survives redeploys), else ~/.config/flickr-organizer/secret.key."""
    from .config import data_dir

    if os.environ.get(KEY_FILE_ENV):
        return Path(os.path.expanduser(os.environ[KEY_FILE_ENV]))
    return data_dir() / "secret.key" if data_dir() else Path(os.path.expanduser(DEFAULT_KEY_FILE))


def _key(create: bool) -> bytes | None:
    if os.environ.get(KEY_ENV):
        return _key_from_text(os.environ[KEY_ENV])
    path = key_file()
    if path.exists():
        return _key_from_text(path.read_text())
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # readable only by you
    with os.fdopen(fd, "w") as f:
        f.write(base64.urlsafe_b64encode(AESGCM.generate_key(256)).decode())
    return _key_from_text(path.read_text())


def new_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(256)).decode()


def encrypt(name: str, value: str) -> tuple[bytes, bytes]:
    nonce = pysecrets.token_bytes(12)
    return nonce, AESGCM(_key(create=True)).encrypt(nonce, value.encode(), name.encode())


def decrypt(name: str, nonce: bytes, ciphertext: bytes, key: bytes | None = None) -> str | None:
    """None if there's no key, or it's the wrong one (e.g. a database moved without its key)."""
    key = key or _key(create=False)
    if key is None:
        return None
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, name.encode()).decode()
    except Exception:
        return None


def hint(value: str) -> str:
    return value[-4:] if len(value) >= 12 else ""


# ---- layering ------------------------------------------------------------------------
def env_paths(environ=os.environ) -> dict[str, str]:
    """setting path -> the environment variable setting it."""
    from .config import platform_paths

    return {**{".".join(path): var for var, path in ENV_OVERRIDES.items() if environ.get(var)},
            **platform_paths(environ)}


def stored_settings(store) -> dict:
    """The admin page's settings, with any saved under an old (RENAMED) path moved to the new one."""
    stored = store.admin_settings()
    for old, new in RENAMED.items():
        if old in stored:
            value = stored.pop(old)
            stored.setdefault(new, value)
    return stored


def layer(file_raw: dict, store, environ=os.environ) -> tuple[dict, dict[str, str], list[str]]:
    """(raw config with all four layers, source per setting path, problems reading secrets)."""
    from .config import apply_env

    raw = copy.deepcopy(file_raw)
    sources = {p: ("file" if get_path(file_raw, p) is not None else "default") for p in leaves()}
    problems = []
    if store is not None:
        for path, value in stored_settings(store).items():
            if path in sources and path not in LOCKED and path not in SECRETS:
                set_path(raw, path, value)
                sources[path] = "admin"
        for name, row in store.secret_rows().items():
            value = decrypt(name, row["nonce"], row["ciphertext"])
            if value is None:
                problems.append(f"{name}: stored encrypted, but the key to read it isn't here "
                                f"({KEY_ENV} or {DEFAULT_KEY_FILE}); set it again on the admin page")
                continue
            set_path(raw, name, value)
            sources[name] = "admin"
    raw = apply_env(raw, environ)
    for path in env_paths(environ):
        sources[path] = "env"
    return raw, sources, problems


def _store_for(cfg_path: str | os.PathLike, file_raw: dict):
    from .store import Store

    from .config import database_path

    db = database_path(cfg_path, file_raw)
    return Store(db) if os.path.exists(db) else None


# ---- saving -------------------------------------------------------------------------------
def save(cfg_path, file_raw: dict, store, changes: dict[str, object], removed: list[str], username: str) -> dict:
    """Apply admin-page changes after validating the result. Raises SettingsError."""
    known, errors = set(leaves()), []
    for path in [*changes, *removed]:
        if path not in known:
            errors.append(f"{path}: not a setting")
        elif path in LOCKED:
            errors.append(f"{path}: can only be changed in config.yaml or the environment ({LOCKED[path]})")
        elif path in SECRETS:
            errors.append(f"{path}: API keys are set separately")
    if errors:
        raise SettingsError(errors)
    stored = store.admin_settings()
    current = {p: v for p, v in stored_settings(store).items() if p in known}   # retired: dropped below
    proposed = {**current, **changes}
    for path in removed:
        proposed.pop(path, None)
    underlying = _merge(DEFAULTS, file_raw)
    # Same as the built-in default: drop the override. One that matches config.yaml stays:
    # the database carries every setting, so a server (no config.yaml) restored from a
    # backup runs the same as here.
    for path in list(proposed):
        if proposed[path] == get_path(DEFAULTS, path):
            proposed.pop(path)
    trial = copy.deepcopy(file_raw)
    for path, value in proposed.items():
        set_path(trial, path, value)
    errors, _ = validate(trial, _merge(DEFAULTS, trial))
    if errors:
        raise SettingsError(errors)
    effective = lambda overrides, p: overrides.get(p, get_path(underlying, p))
    log = [(p, json.dumps(effective(current, p))[:500], json.dumps(effective(proposed, p))[:500])
           for p in sorted(set(current) | set(proposed)) if effective(current, p) != effective(proposed, p)]
    # Against what's stored, not `current`: a setting under an old path is written anew here.
    store.save_admin_settings({p: v for p, v in proposed.items() if stored.get(p) != v},
                              [p for p in stored if p not in proposed], username, log)
    return {"changed": [p for p, _, _ in log]}


def set_secret(store, name: str, value: str | None, username: str) -> None:
    if name not in SECRETS:
        raise SettingsError([f"{name}: not an API key setting"])
    if value is None or not value.strip():
        store.save_secret(name, None, None, "", username)
        return
    value = value.strip()
    nonce, ciphertext = encrypt(name, value)
    store.save_secret(name, nonce, ciphertext, hint(value), username)


# ---- describing (for the admin page) ---------------------------------------------------------
def _kind(path: str, default) -> dict:
    tpath = tuple(path.split("."))
    if path in SECRETS:
        return {"kind": "secret"}
    if tpath in CHOICES:
        from .settings_meta import META

        labels = META.get(path, {}).get("options", {})
        return {"kind": "choice", "options": list(CHOICES[tpath]),
                "option_labels": {o: labels.get(o, o) for o in CHOICES[tpath]}}
    if tpath in COLORS:
        return {"kind": "color"}
    if path == "vision.categories":
        return {"kind": "categories"}
    if tpath in FREE_MAPS:
        return {"kind": "map"}
    if tpath in ITEM_KEYS:
        required, optional = ITEM_KEYS[tpath]
        return {"kind": "items", "fields": sorted(required) + sorted(optional), "required": sorted(required)}
    if isinstance(default, bool):
        return {"kind": "bool"}
    if isinstance(default, int):
        return {"kind": "int"}
    if isinstance(default, float):
        return {"kind": "float"}
    if isinstance(default, list) or OPTIONAL.get(tpath) is list:
        return {"kind": "list"}
    if path.endswith("instructions"):
        return {"kind": "text"}
    return {"kind": "string"}


def describe(cfg: dict, sources: dict[str, str], store) -> list[dict]:
    from .settings_meta import META

    secret_rows = store.secret_rows() if store is not None else {}
    env = env_paths()
    out = []
    order = {p: i for i, p in enumerate(META)}
    for path in sorted(leaves(), key=lambda p: order.get(p, len(order))):   # the page's order
        default = get_path(DEFAULTS, path)
        meta = META.get(path, {})
        item = {"path": path, "section": meta.get("section") or path.split(".")[0], "label": meta.get("label", path),
                "help": meta.get("help", ""), "group": meta.get("group", ""), **_kind(path, default),
                "source": sources.get(path, "default"),
                "locked": LOCKED.get(path) or (f"set by the environment ({env[path]})" if path in env else None)}
        if path in SECRETS:
            value = get_path(cfg, path)
            row = secret_rows.get(path)
            item["value"] = None
            item["set"] = bool(value)
            item["hint"] = row["hint"] if row and item["source"] == "admin" else (hint(value) if value else "")
        else:
            item["value"] = get_path(cfg, path)
            item["default"] = default
        out.append(item)
    return out
