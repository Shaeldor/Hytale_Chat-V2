"""Windows send backend: focus the client window and type via SendInput.

Uses KEYEVENTF_UNICODE to type arbitrary characters (base64 '+','/','=' included)
regardless of keyboard layout, and virtual-key codes for Enter / the chat-open key.

windll is touched only inside functions so this imports on non-Windows too; memio's
sibling dispatcher (send.py) only selects it on win32.
"""

import ctypes
import subprocess
import time
from ctypes import wintypes

from . import crypto

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_INSERT = 0x2D
VK_V = 0x56
SW_RESTORE = 9

# Send timing (seconds) — tweak if paste/typing lands wrong. SendInput is real input
# on Windows, so it both opens the chat and pastes (unlike Wayland's virtual keyboard).
T_SETTLE = 0.30        # after focusing the game, before any input
T_OPEN_WAIT = 0.15     # after opening chat, before paste/type (raise if it lands wrong)
T_AFTER_INPUT = 0.08   # after paste/type, before pressing Enter to send
T_CHUNK_GAP = 0.30     # pause between multi-part (chunked) messages

# Fixed-width aliases so struct layout matches Win32 on all platforms (see memio_win).
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _WORD),
        ("wScan", _WORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUTUNION)]


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendInput.restype = wintypes.UINT
    u.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    # HWND is pointer-sized; without argtypes ctypes truncates it to 32 bits on
    # 64-bit Windows, so focus/show act on a garbage window. Declare them all.
    u.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    return u


# WINFUNCTYPE is Windows-only; fall back so this module still imports off-Windows
# (it is only ever *called* on win32, via the send.py dispatcher).
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDENUMPROC = _FUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_game_window():
    """Return the HWND of the Hytale game window, else None.

    Matches the exact title "Hytale" (the game) and explicitly avoids the overlay
    window (titled "hytale-tunnel") or any "...tunnel" terminal.
    """
    u = _user32()
    exact, fuzzy = [], []

    def cb(hwnd, _lparam):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value.lower()
            if t == "hytale":
                exact.append(hwnd)
                return False
            if "hytale" in t and "tunnel" not in t:
                fuzzy.append(hwnd)
        return True

    u.EnumWindows(_WNDENUMPROC(cb), 0)
    return exact[0] if exact else (fuzzy[0] if fuzzy else None)


def focus_game() -> bool:
    u = _user32()
    hwnd = find_game_window()
    if not hwnd:
        return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentThreadId.restype = wintypes.DWORD
    # AttachThreadInput to the current foreground thread so SetForegroundWindow is
    # allowed (Windows blocks foreground stealing otherwise).
    fg = u.GetForegroundWindow()
    fg_thread = u.GetWindowThreadProcessId(fg, None) if fg else 0
    our_thread = k.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != our_thread
                    and u.AttachThreadInput(our_thread, fg_thread, True))
    try:
        # Only un-minimize; do NOT SW_RESTORE a maximized/borderless window (that
        # would shrink it -- the "minimizes my game" symptom).
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(our_thread, fg_thread, False)
    return True


def _ki(vk=0, scan=0, flags=0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def _send(inputs):
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    _user32().SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(INPUT))


def _type_text(text: str, delay_ms: int = 8):
    # Type with a per-character delay; sending the whole line at once can outrun the
    # game and drop characters (a dropped char breaks the /msg command or the blob).
    for ch in text:
        code = ord(ch)
        _send([_ki(scan=code, flags=KEYEVENTF_UNICODE),
               _ki(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        if delay_ms:
            time.sleep(delay_ms / 1000)


def _press_vk(vk: int):
    _send([_ki(vk=vk), _ki(vk=vk, flags=KEYEVENTF_KEYUP)])


def _open_chat(open_key: str):
    key = open_key.lower()
    if key in ("return", "enter"):
        _press_vk(VK_RETURN)
    elif key == "slash":
        _type_text("/")
    elif len(open_key) == 1:
        _type_text(open_key)
    else:
        _press_vk(VK_RETURN)


def _set_clipboard(text: str) -> bool:
    try:
        subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
        return True
    except Exception:
        return False


def _paste(method: str):
    mod, key = (VK_SHIFT, VK_INSERT) if method == "shift-insert" else (VK_CONTROL, VK_V)
    _send([_ki(vk=mod), _ki(vk=key),
           _ki(vk=key, flags=KEYEVENTF_KEYUP), _ki(vk=mod, flags=KEYEVENTF_KEYUP)])


def _send_line(line: str, open_key: str, type_delay_ms: int, paste_method: str) -> None:
    use_paste = paste_method in ("ctrl-v", "shift-insert")
    if use_paste and _set_clipboard(line):
        _open_chat(open_key)
        time.sleep(T_OPEN_WAIT)
        _paste(paste_method)
    else:
        _open_chat(open_key)
        time.sleep(T_OPEN_WAIT)
        _type_text(line, type_delay_ms)
    time.sleep(T_AFTER_INPUT)
    _press_vk(VK_RETURN)


def send_message(friend: str, message: str, open_key: str = "Return",
                 settle: float = 0.3, type_delay_ms: int = 12, pre_send=None,
                 paste_method: str = "type") -> list[str]:
    """Encrypt `message` for `friend` and send it in-game as one or more /msg lines.

    Long messages are split into encrypted chunks (the receiver reassembles them).
    `paste_method`: 'type' (default, reliable), or 'ctrl-v' / 'shift-insert' (paste).
    `pre_send(token)` is called for each token before it is sent.
    """
    tokens = crypto.encrypt_messages(friend, message)
    focus_game()
    time.sleep(T_SETTLE)
    for idx, tok in enumerate(tokens):
        if pre_send:
            pre_send(tok)
        _send_line(f"/msg {friend} {tok}", open_key, type_delay_ms, paste_method)
        if idx < len(tokens) - 1:
            time.sleep(T_CHUNK_GAP)
    return tokens


def send_public(message: str, open_key: str = "Return", type_delay_ms: int = 12,
                paste_method: str = "type") -> str:
    """Type `message` into the in-game chat as a normal (unencrypted) public line --
    no /msg prefix, no crypto. Used for plain compose-box input (anything that
    isn't a /msg <name> or /r whisper)."""
    focus_game()
    time.sleep(T_SETTLE)
    _send_line(message, open_key, type_delay_ms, paste_method)
    return message
