"""Detect the local player's in-game name (so our own messages render as 'you').

The Hytale client logs it on every launch:
    ...|HytaleClient.Application.Program|Auth config: mode=Authenticated, uuid=..., name=Shaeldor

We read the newest client log and take the last such line. Cross-platform via a
list of candidate log directories; callers may override with an explicit name.
"""

import glob
import os
import re
import sys
from pathlib import Path

_AUTH_RE = re.compile(r"Auth config:.*\bname=([A-Za-z0-9_]{2,20})")


def _log_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform.startswith("linux"):
        return [
            home / ".var/app/com.hypixel.HytaleLauncher/data/Hytale/UserData/Logs",
            home / ".local/share/Hytale/UserData/Logs",
        ]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        cands = []
        for base in (appdata, local):
            if base:
                cands.append(Path(base) / "Hytale" / "UserData" / "Logs")
                cands.append(Path(base) / "HytaleLauncher" / "Hytale" / "UserData" / "Logs")
        return cands
    return [home / "Hytale/UserData/Logs"]


def detect(explicit: str | None = None) -> str | None:
    """Return the local player's name, or None if it can't be found.

    `explicit` (e.g. a --me flag) always wins.
    """
    if explicit:
        return explicit
    logs: list[str] = []
    for d in _log_dirs():
        if d.is_dir():
            logs += glob.glob(str(d / "*client.log"))
    if not logs:
        return None
    logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in logs[:5]:                       # newest few, in case the latest is mid-write
        found = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _AUTH_RE.search(line)
                    if m:
                        found = m.group(1)       # keep last match in this file
        except OSError:
            continue
        if found:
            return found
    return None
