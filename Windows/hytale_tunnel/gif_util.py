"""Local GIF library + cache for the tunnel overlay.

No third-party search service: GIFs are added by pasting a DIRECT gif URL and
saved to a local favorites list (the "library"). Displaying a GIF downloads its
bytes -- the only outbound network call in the project -- into a local cache;
QMovie (in overlay.py) does the actual animation. Pure I/O, no Qt import, so it
can be unit-tested on its own.

Storage (alongside the existing ~/.hytalecrypt files):
    gifs.json        {"favorites": [url, ...], "recents": [url, ...]}
    gifs/<hash>.gif  downloaded cache
"""
import hashlib
import json
import urllib.request
from pathlib import Path

from . import crypto

GIFS_JSON = crypto.CONFIG_DIR / "gifs.json"
CACHE_DIR = crypto.CONFIG_DIR / "gifs"

_MAX_BYTES = 15 * 1024 * 1024        # hard cap on a downloaded GIF
_TIMEOUT = 15                        # seconds for the fetch
_MAX_RECENTS = 24


def available() -> bool:
    """GIFs need no API key/library, so the feature is always available."""
    return True


def valid_url(url: str) -> bool:
    """A quick shape check for the compose box / picker (not a fetch)."""
    url = (url or "").strip()
    return url.lower().startswith(("http://", "https://")) and 8 < len(url) <= 2000


# --------------------------------------------------------------------------- store

def _load() -> dict:
    try:
        d = json.loads(GIFS_JSON.read_text())
        if isinstance(d, dict):
            d.setdefault("favorites", [])
            d.setdefault("recents", [])
            return d
    except (OSError, ValueError):
        pass
    return {"favorites": [], "recents": []}


def _save(d: dict) -> None:
    try:
        crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GIFS_JSON.write_text(json.dumps(d))
    except OSError:
        pass


def favorites() -> list:
    return list(_load()["favorites"])


def recents() -> list:
    return list(_load()["recents"])


def is_favorite(url: str) -> bool:
    return url in _load()["favorites"]


def add_favorite(url: str) -> None:
    if not valid_url(url):
        return
    d = _load()
    if url not in d["favorites"]:
        d["favorites"].insert(0, url)          # newest first
        _save(d)


def remove_favorite(url: str) -> None:
    d = _load()
    if url in d["favorites"]:
        d["favorites"].remove(url)
        _save(d)


def forget(url: str) -> None:
    """Purge a URL from BOTH favorites and recents (so a bad/unwanted GIF can actually be
    deleted from the picker -- removing only the favorite would leave it in recents)."""
    d = _load()
    changed = False
    for lst in ("favorites", "recents"):
        if url in d[lst]:
            d[lst].remove(url)
            changed = True
    if changed:
        _save(d)


def push_recent(url: str) -> None:
    if not valid_url(url):
        return
    d = _load()
    r = d["recents"]
    if url in r:
        r.remove(url)
    r.insert(0, url)
    del r[_MAX_RECENTS:]
    _save(d)


# --------------------------------------------------------------------------- cache

def cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{h}.gif"


def is_cached(url: str) -> bool:
    p = cache_path(url)
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def fetch(url: str) -> Path | None:
    """Download the GIF at `url` into the cache and return its path (or None on any
    failure). Defensive because this is the only network call in the project and the
    URL comes from a friend: http/https only (incl. after redirects), size-capped,
    and the body must be an image (rejects HTML 'share' pages). The bytes are only
    ever handed to QMovie, never executed. Safe to call off the Qt thread."""
    if not valid_url(url):
        return None
    p = cache_path(url)
    if is_cached(url):
        return p
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hytale-tunnel"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            if resp.geturl().split(":", 1)[0].lower() not in ("http", "https"):
                return None                    # redirected off http(s)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/"):
                return None                    # e.g. text/html share page, not a direct GIF
            data = resp.read(_MAX_BYTES + 1)
            if not data or len(data) > _MAX_BYTES:
                return None
    except Exception:                          # noqa: BLE001 - any network/parse error
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(p)                         # atomic
    except OSError:
        return None
    return p
