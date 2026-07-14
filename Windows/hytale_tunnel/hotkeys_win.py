"""Windows global hotkeys + focus toggle for the overlay.

The Linux build uses Hyprland binds (SUPER+SHIFT+J toggles size via SIGUSR1, SUPER+
SHIFT+P toggles focus via focus-toggle.sh). Windows has neither, so we register true
system-global hotkeys with Win32 RegisterHotKey and catch the resulting WM_HOTKEY
messages through a Qt native event filter -- they fire even while HytaleClient is
focused, like the Hyprland binds.

Diagnostics: registration results and every hotkey hit are printed to stdout AND the
first hit of each key is reported to the overlay (notify), so you can confirm the keys
work without watching a terminal. Defaults: Win+Shift+J (size), Win+Shift+O (focus);
Win+Shift+P is reserved by Windows, so it's not the default. Configurable from app.py.
"""

import ctypes
import time
from ctypes import wintypes

from PyQt6 import QtCore

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
GW_OWNER = 4
SW_RESTORE = 9
SW_SHOW = 5
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x0002

_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN}

_SPECIAL_VK = {
    "\\": 0xDC,  # VK_OEM_5
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


def parse_hotkey(spec: str):
    """'win+shift+j' -> (modifiers|MOD_NOREPEAT, vk). Single letter/digit key only."""
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _SPECIAL_VK:
            vk = _SPECIAL_VK[part]
        elif len(part) == 1:
            vk = ord(part.upper())          # VK codes for 0-9 / A-Z are their ASCII values
        else:
            raise ValueError(f"unsupported key {part!r} in hotkey '{spec}'")
    if vk is None:
        raise ValueError(f"no key in hotkey '{spec}'")
    return mods | MOD_NOREPEAT, vk


# Handles are pointer-sized; declare argtypes/restype so ctypes doesn't default HWNDs
# to 32-bit c_int and truncate them on 64-bit Windows (same care as memio_win.py).
def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.RegisterHotKey.restype = wintypes.BOOL
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.UnregisterHotKey.restype = wintypes.BOOL
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.BringWindowToTop.restype = wintypes.BOOL
    u.SetFocus.argtypes = [wintypes.HWND]
    u.SetFocus.restype = wintypes.HWND
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindow.restype = wintypes.HWND
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                        ctypes.c_void_p, wintypes.UINT]
    u.SystemParametersInfoW.restype = wintypes.BOOL
    return u


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentThreadId.restype = wintypes.DWORD
    return k


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_game_hwnd(pid: int):
    """The game's main top-level window: visible, unowned, titled, owned by `pid`."""
    u = _user32()
    found = []

    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if (wpid.value == pid and u.IsWindowVisible(hwnd)
                and not u.GetWindow(hwnd, GW_OWNER)
                and u.GetWindowTextLengthW(hwnd) > 0):
            found.append(hwnd)
            return False                    # stop at the first match
        return True

    u.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def _force_foreground(hwnd) -> bool:
    """Make hwnd the foreground window past Windows' focus-steal defences. Returns
    whether hwnd actually ended up foreground.

    Three things block a background process from taking focus: the foreground *lock
    timeout*, thread-input isolation, and Z-order. We clear the lock timeout for the
    moment (then restore it), attach to the current foreground thread's input queue so
    Windows treats the change as user-initiated, and only then set foreground/focus.
    No synthetic keypresses, so the game never receives a stray ALT.

    Only un-minimize (SW_RESTORE) when the window is actually iconic -- calling
    SW_RESTORE unconditionally un-maximizes a fullscreen window (that was why returning
    focus to the game used to visibly shrink it out of fullscreen)."""
    u, k = _user32(), _kernel32()
    hwnd = wintypes.HWND(int(hwnd))
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_RESTORE)

    old = wintypes.DWORD(0)
    u.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old), 0)
    u.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0),
                            SPIF_SENDCHANGE)

    target = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    mine = k.GetCurrentThreadId()
    attached = bool(target and target != mine and u.AttachThreadInput(mine, target, True))
    try:
        u.BringWindowToTop(hwnd)
        u.ShowWindow(hwnd, SW_SHOW)
        set_ok = bool(u.SetForegroundWindow(hwnd))
        u.SetFocus(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(mine, target, False)
        u.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                ctypes.c_void_p(old.value), SPIF_SENDCHANGE)
    # GetForegroundWindow can read 0 for a beat mid-switch, so confirm with a few
    # short retries rather than trusting a single immediate read.
    target_hwnd = int(hwnd.value)
    now_fg = 0
    for _ in range(10):
        now_fg = int(u.GetForegroundWindow() or 0)
        if now_fg == target_hwnd:
            break
        time.sleep(0.01)
    success = now_fg == target_hwnd
    print(f"[hotkey] _force_foreground hwnd={target_hwnd} set_ok={set_ok} "
          f"attached={attached} now_fg={now_fg} success={success}", flush=True)
    return success


class _Filter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, handlers: dict, notify=None):
        super().__init__()
        self._handlers = handlers          # {id: (callback, spec)}
        self._notify = notify

    def nativeEventFilter(self, eventType, message):
        try:
            et = eventType if isinstance(eventType, bytes) else bytes(eventType)
            if b"MSG" in et:               # windows_generic_MSG / windows_dispatcher_MSG
                msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                if msg.message == WM_HOTKEY:
                    hid = int(msg.wParam)
                    entry = self._handlers.get(hid)
                    print(f"[hotkey] WM_HOTKEY id={hid} -> "
                          f"{'handled' if entry else 'no handler'}", flush=True)
                    if entry:
                        cb, spec = entry
                        QtCore.QTimer.singleShot(0, cb)
        except Exception as e:             # never let the filter die silently
            print(f"[hotkey] filter error: {e!r}", flush=True)
        return False, 0


class WinHotkeys:
    def __init__(self, app, hwnd, notify=None):
        self._u = _user32()
        self._hwnd = wintypes.HWND(int(hwnd))
        self._handlers: dict = {}
        self._ids: list = []
        self._next = 1
        self._filter = _Filter(self._handlers, notify)
        app.installNativeEventFilter(self._filter)
        print(f"[hotkey] native event filter installed on hwnd={int(hwnd)}", flush=True)

    def register(self, spec: str, callback) -> bool:
        mods, vk = parse_hotkey(spec)
        hid = self._next
        ok = bool(self._u.RegisterHotKey(self._hwnd, hid, mods, vk))
        if ok:
            self._handlers[hid] = (callback, spec)
            self._ids.append(hid)
            self._next += 1
            print(f"[hotkey] registered {spec!r} as id={hid}", flush=True)
        else:
            print(f"[hotkey] FAILED to register {spec!r} "
                  f"(GetLastError={ctypes.get_last_error()}; 1409 = already in use by "
                  f"another app)", flush=True)
        return ok

    def unregister_all(self) -> None:
        for hid in self._ids:
            self._u.UnregisterHotKey(self._hwnd, hid)
        self._ids.clear()
        self._handlers.clear()


def _focus_game(find_pid) -> bool:
    """Hand foreground focus back to the game window. Returns False if not found.

    This direction holds reliably -- the game is happy to be foreground. The reverse
    (forcing the overlay foreground over a running game) does NOT hold: the game grabs
    foreground straight back within a frame, so we never try it. To type, click the
    chat -- a real click gives lasting focus where a programmatic grab can't."""
    pid = find_pid()
    gh = find_game_hwnd(pid) if pid else None
    if gh:
        _force_foreground(gh)
        return True
    print("[hotkey] focus game: HytaleClient window not found", flush=True)
    return False


def _open_chat(ui) -> None:
    """Expand the overlay to normal size -- the same path that restores it from the
    pill. Stays on-top; click it to type."""
    if getattr(ui, "_collapsed", False):
        ui.set_collapsed(False)
        
    if hasattr(ui, "set_opened"):
        ui.set_opened(True)
    
    # We must explicitly force the OS to give us foreground, as Windows prevents
    # background windows from stealing focus from the active game.
    _force_foreground(int(ui.winId()))
    ui.raise_()
    ui.activateWindow()
    ui.input.setFocus()


def _close_chat(ui, find_pid) -> None:
    """Collapse to the pill and hand focus back to the game -- exactly what ESC does."""
    _focus_game(find_pid)
    if hasattr(ui, "set_opened"):
        ui.set_opened(False)
    if not getattr(ui, "_collapsed", False):
        ui.set_collapsed(True)


def _unfocus_chat(ui, find_pid) -> None:
    """Hand focus back to the game without collapsing the overlay."""
    print("[hotkey] -> unfocus chat (focus game)", flush=True)
    _focus_game(find_pid)


def setup(app, ui, find_pid, open_spec="shift+up", close_spec="shift+down", unfocus_spec="shift+left", notify=None):
    """Register both global hotkeys and wire them to the overlay. Returns a WinHotkeys
    (call .unregister_all() on shutdown). Reports results to stdout and via notify()."""
    hk = WinHotkeys(app, ui.winId(), notify=notify)
    ok_open = hk.register(open_spec, lambda: _open_chat(ui))
    ok_close = hk.register(close_spec, lambda: _close_chat(ui, find_pid))
    ok_unfocus = hk.register(unfocus_spec, lambda: _unfocus_chat(ui, find_pid))
    # ESC collapses the overlay to a pill; without this it keeps OS focus and the game
    # stays unresponsive until you click it. Hand focus straight back to the game.
    if hasattr(ui, "escape_to_game"):
        ui.escape_to_game.connect(lambda: _focus_game(find_pid))
    if notify:
        good = [s for s, ok in ((open_spec, ok_open), (close_spec, ok_close), (unfocus_spec, ok_unfocus)) if ok]
        bad = [s for s, ok in ((open_spec, ok_open), (close_spec, ok_close), (unfocus_spec, ok_unfocus)) if not ok]
        if bad:
            notify("hotkey(s) already in use: " + ", ".join(bad)
                   + " — set others with --hotkey-* args")
    return hk
