"""Linux/Wayland send backend: focus the client (hyprctl) and type via wtype."""

import json
import os
import shutil
import subprocess
import time

from . import crypto

# ydotool keycodes (Linux input-event-codes): LEFTCTRL=29 V=47 LEFTSHIFT=42 INSERT=110
# ENTER=28 T=20 SLASH=53
_YD_KEYS = {
    "ctrl-v": ["29:1", "47:1", "47:0", "29:0"],
    "shift-insert": ["42:1", "110:1", "110:0", "42:0"],
}
_YD_ENTER = ["28:1", "28:0"]
_YD_OPEN = {"return": _YD_ENTER, "enter": _YD_ENTER,
            "t": ["20:1", "20:0"], "slash": ["53:1", "53:0"]}

# ============================================================================
# SEND TIMING — tweak these if paste/typing lands wrong (seconds; ms where noted)
# ============================================================================
T_SETTLE = 0.30        # after focusing the game, before any input
T_OPEN_WAIT = 0.05     # after opening chat, before pasting/typing. RAISE if the paste
                       #   lands in gameplay (chat not finished opening); LOWER for speed
T_AFTER_INPUT = 0.08   # after paste/type, before pressing Enter to send
T_CHUNK_GAP = 0.30     # pause between multi-part (chunked) messages
YD_COMBO_DELAY_MS = 50  # ms between ydotool key events for the Ctrl+V combo
# ============================================================================


def _ydotool_socket() -> str | None:
    s = os.environ.get("YDOTOOL_SOCKET")
    if s and os.path.exists(s):
        return s
    cand = f"/run/user/{os.getuid()}/.ydotool_socket"
    return cand if os.path.exists(cand) else None


def _yd(sock: str, keys: list, delay: int = 25) -> bool:
    try:
        subprocess.run(["ydotool", "key", "-d", str(delay), *keys],
                       env=dict(os.environ, YDOTOOL_SOCKET=sock),
                       capture_output=True, timeout=3)
        return True
    except Exception:
        return False


def _hyprctl_json(*args):
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


GAME_CLASS = "HytaleClient"     # exact class; do NOT substring-match "hytale"
GAME_TITLE = "Hytale"           # matching the overlay (hytale-tunnel) or a terminal


def find_game_window() -> dict | None:
    clients = _hyprctl_json("clients") or []
    # Exact class first so we never grab the overlay window or a terminal that
    # merely has "hytale" in its title.
    for c in clients:
        if c.get("class") == GAME_CLASS or c.get("initialClass") == GAME_CLASS:
            return c
    for c in clients:
        if c.get("title") == GAME_TITLE:
            return c
    return None


def focus_game() -> bool:
    w = find_game_window()
    if not w:
        return False
    subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{w['address']}"],
                   capture_output=True)
    return True


def _wtype(*args):
    subprocess.run(["wtype", *args], capture_output=True)


def _type_line(line: str, delay_ms: int):
    # Feed text via stdin ("wtype -") instead of as an argument: handles long base64
    # and special chars cleanly, and is reliable + fast. (This is the method that works.)
    subprocess.run(["wtype", "-d", str(delay_ms), "-"], input=line.encode("utf-8"),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wl_copy(data):
    # wl-copy forks a daemon to serve the clipboard; capturing its output makes us
    # wait forever on a pipe the daemon keeps open. Redirect to DEVNULL + timeout.
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        subprocess.run(["wl-copy"], input=data, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        pass


def _prep_paste(paste_method: str):
    """Resolve whether clipboard paste is usable, and save the clipboard if so.

    Paste is delivered via ydotool (kernel /dev/uinput = a REAL input device): apps
    reject Ctrl+V from wtype's virtual keyboard, but accept ydotool's. Needs ydotoold.
    Returns (yd_sock, use_paste, saved_clipboard).
    """
    yd_sock = _ydotool_socket()
    use_paste = (paste_method in ("ctrl-v", "shift-insert")
                 and bool(shutil.which("wl-copy")) and bool(shutil.which("ydotool"))
                 and yd_sock is not None)
    saved = None
    if use_paste:
        try:
            saved = subprocess.run(["wl-paste", "-n"], capture_output=True,
                                   timeout=2).stdout
        except Exception:
            saved = None
    return yd_sock, use_paste, saved


def _send_line(line: str, open_key: str, type_delay_ms: int, paste_method: str,
               yd_sock: str | None, use_paste: bool) -> None:
    yd_open = _YD_OPEN.get(open_key.lower(), _YD_ENTER)
    if use_paste:
        # The game's chat opens only from REAL keyboard input, so open + paste + send
        # all via ydotool (uinput). wtype's virtual keyboard never opens the chat.
        _wl_copy(line)                                # clipboard ready before paste
        _yd(yd_sock, yd_open)                         # open chat (real Enter)
        time.sleep(T_OPEN_WAIT)                       # let the chat finish opening
        _yd(yd_sock, _YD_KEYS.get(paste_method, _YD_KEYS["ctrl-v"]),
            delay=YD_COMBO_DELAY_MS)                  # paste
        time.sleep(T_AFTER_INPUT)
        _yd(yd_sock, _YD_ENTER)                       # send (real Enter)
    else:
        _wtype("-k", open_key)
        time.sleep(T_OPEN_WAIT)
        _type_line(line, type_delay_ms)
        time.sleep(T_AFTER_INPUT)
        _wtype("-k", "Return")


def send_message(friend: str, message: str, open_key: str = "Return",
                 settle: float = 0.3, type_delay_ms: int = 12, pre_send=None,
                 paste_method: str = "type") -> list[str]:
    """Encrypt `message` for `friend` and send it in-game as one or more /msg lines.

    Long messages are split into encrypted chunks (the receiver reassembles them).
    `paste_method`: 'type' (per-key, reliable, default), or 'ctrl-v' / 'shift-insert'
    (clipboard paste -- instant, but only works if the game's chat box accepts it).
    `pre_send(token)` is called for each token before it is sent so the UI can
    ledger it (avoids the echo showing as a reply).
    """
    tokens = crypto.encrypt_messages(friend, message)
    yd_sock, use_paste, saved = _prep_paste(paste_method)
    focus_game()
    time.sleep(T_SETTLE)
    for idx, tok in enumerate(tokens):
        if pre_send:
            pre_send(tok)
        _send_line(f"/msg {friend} {tok}", open_key, type_delay_ms, paste_method,
                  yd_sock, use_paste)
        if idx < len(tokens) - 1:
            time.sleep(T_CHUNK_GAP)

    if use_paste and saved:                       # restore the user's clipboard
        _wl_copy(saved)
    return tokens


def send_public(message: str, open_key: str = "Return", type_delay_ms: int = 12,
                paste_method: str = "type") -> str:
    """Type `message` into the in-game chat as a normal (unencrypted) public line --
    no /msg prefix, no crypto. Used for plain compose-box input (anything that
    isn't a /msg <name> or /r whisper)."""
    yd_sock, use_paste, saved = _prep_paste(paste_method)
    focus_game()
    time.sleep(T_SETTLE)
    _send_line(message, open_key, type_delay_ms, paste_method, yd_sock, use_paste)
    if use_paste and saved:
        _wl_copy(saved)
    return message
