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
from ctypes import wintypes

from PyQt6 import QtCore

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
GW_OWNER = 4
SW_RESTORE = 9

_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN}


def parse_hotkey(spec: str):
    """'win+shift+j' -> (modifiers|MOD_NOREPEAT, vk). Single letter/digit key only."""
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
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
    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindow.restype = wintypes.HWND
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
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


def _force_foreground(hwnd) -> None:
    """Bring hwnd to the foreground past the focus-steal lock via AttachThreadInput
    (no synthetic keypresses, so the game never receives a stray ALT)."""
    u, k = _user32(), _kernel32()
    u.ShowWindow(hwnd, SW_RESTORE)
    target = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    mine = k.GetCurrentThreadId()
    attached = bool(target and target != mine and u.AttachThreadInput(mine, target, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetFocus(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(mine, target, False)


class _Filter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, handlers: dict, notify=None):
        super().__init__()
        self._handlers = handlers          # {id: (callback, spec)}
        self._notify = notify
        self._announced = set()

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
                        if self._notify and hid not in self._announced:
                            self._announced.add(hid)
                            self._notify(f"hotkey {spec} works ✓")
                        cb()
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


def _focus_toggle(ui, find_pid) -> None:
    u = _user32()
    overlay_hwnd = int(ui.winId())
    if int(u.GetForegroundWindow() or 0) == overlay_hwnd:
        pid = find_pid()                       # overlay is up front -> go back to game
        gh = find_game_hwnd(pid) if pid else None
        if gh:
            _force_foreground(gh)
    else:
        if getattr(ui, "_collapsed", False):
            ui.set_collapsed(False)            # expand so there's a box to type into
        _force_foreground(overlay_hwnd)
        ui.raise_()
        ui.activateWindow()
        ui.focus_input()


def setup(app, ui, find_pid, size_spec="win+shift+j",
          focus_spec="win+shift+o", notify=None):
    """Register both global hotkeys and wire them to the overlay. Returns a WinHotkeys
    (call .unregister_all() on shutdown). Reports results to stdout and via notify()."""
    hk = WinHotkeys(app, ui.winId(), notify=notify)
    ok_size = hk.register(size_spec, ui.toggle_collapsed)
    ok_focus = hk.register(focus_spec, lambda: _focus_toggle(ui, find_pid))
    if notify:
        good = [s for s, ok in ((size_spec, ok_size), (focus_spec, ok_focus)) if ok]
        bad = [s for s, ok in ((size_spec, ok_size), (focus_spec, ok_focus)) if not ok]
        if good:
            notify("hotkeys ready: " + ", ".join(good))
        if bad:
            notify("hotkey(s) already in use: " + ", ".join(bad)
                   + " — set others with --hotkey-size / --hotkey-focus")
    return hk
