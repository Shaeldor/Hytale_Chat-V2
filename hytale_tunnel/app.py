"""Glue: memory scanner thread -> overlay transcript; compose box -> encrypt+send.

Run via the `hytale-tunnel` launcher (which puts ~/.local/lib on PYTHONPATH).
"""

import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

from PyQt6 import QtCore, QtWidgets

from . import chatframe, crypto, gif_util, inject_client, memscan, playername, send
from .chatframe import Msg
from .overlay import Overlay

LINUX = sys.platform.startswith("linux")

# Optional geometry trace: set HYTALE_GEO_DEBUG=/path to log every open/close/persist/
# collapse decision + the geometry it read/applied, for diagnosing the "window drifts /
# gets short after moving or pilling" bug. No-op unless the env var is set.
_GEO_DBG = None
if os.environ.get("HYTALE_GEO_DEBUG"):
    try:
        _GEO_DBG = open(os.environ["HYTALE_GEO_DEBUG"], "a", buffering=1)
    except OSError:
        _GEO_DBG = None


def _glog(tag: str, **kw) -> None:
    if _GEO_DBG:
        try:
            _GEO_DBG.write(tag + " " + " ".join(f"{k}={v}" for k, v in kw.items()) + "\n")
        except OSError:
            pass

# Persisted overlay layout (position, size, font, recipient) so the overlay comes back
# exactly where you left it. Written on change (polled) and on exit; loaded at startup.
STATE_PATH = crypto.CONFIG_DIR / "overlay.json"


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _query_geometry_linux():
    """Our overlay's (x, y, w, h) relative to its monitor, via hyprctl, or None.
    (Wayland doesn't tell Qt its own frame position, so we ask the compositor.)"""
    try:
        clients = json.loads(subprocess.run(["hyprctl", "-j", "clients"],
                                            capture_output=True, text=True).stdout)
        mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"],
                                         capture_output=True, text=True).stdout)
    except Exception:                             # noqa: BLE001
        return None
    w = next((c for c in clients if c.get("class") == "hytale-tunnel"
              or c.get("initialClass") == "hytale-tunnel"), None)
    if not w or not w.get("at") or not w.get("size"):
        return None
    ax, ay = w["at"]
    ww, hh = w["size"]
    mon = next((m for m in mons if m.get("id") == w.get("monitor")), None) \
        or (mons[0] if mons else None)
    ox, oy = (mon["x"], mon["y"]) if mon else (0, 0)
    return (ax - ox, ay - oy, ww, hh)


def _parse_command(text: str, last_contact: dict):
    """Classify compose-box input.

    '/msg <name> <message>' or '/r <message>' (reply to whoever we last privately
    exchanged messages with) -> encrypted private send to a specific friend.
    '/p <message>' -> encrypted party send (to the shared party group key).
    Anything else -> plain public chat, typed in-game exactly as-is, unencrypted.
    Returns (mode, target, body) with mode in {'private', 'party', 'public', 'error'}
    (for 'party' target is None -> resolved by the caller; 'error': body is a
    user-facing message, target is None).
    """
    if text.startswith("/msg "):
        parts = text[len("/msg "):].split(None, 1)
        if len(parts) < 2:
            return "error", None, "usage: /msg <name> <message>"
        name, msg = parts[0], parts[1]
        if name in crypto.list_psk_friends():
            return "private", name, msg                # have a key -> encrypted
        return "public", None, f"/msg {name} {msg}"    # no key -> plain /msg, as-is
    if text == "/p" or text.startswith("/p "):
        body = text[len("/p"):].strip()
        if not body:
            return "error", None, "usage: /p <message>"
        if not crypto.list_groups():
            return "error", None, ("no party key set up. Everyone in the party runs: "
                                   "hytalecrypt gengroupkey  →  setgroupkey party <key>")
        return "party", None, body
    if text == "/r" or text.startswith("/r "):
        body = text[len("/r"):].strip()
        if not body:
            return "error", None, "usage: /r <message>"
        name = last_contact["name"]
        if not name:
            return "error", None, "no one to reply to yet"
        if name in crypto.list_psk_friends():
            return "private", name, body               # have a key -> encrypted
        return "public", None, f"/msg {name} {body}"   # no key -> plain /msg, as-is
    return "public", None, text


def _pick_party_group(groups: list, explicit: str | None) -> str | None:
    """Which party group '/p' sends to: an explicit --party name, else the group named
    'party' if present, else the sole/first group ('' -> None when none exist)."""
    if explicit:
        return explicit
    if not groups:
        return None
    return "party" if "party" in groups else groups[0]


def _position_top_right(ui, app, pos=None) -> None:
    """Place the overlay at `pos` (x, y) relative to the focused monitor, or a default
    spot. Wayland ignores client-set positions, so on Linux we ask Hyprland to move it;
    on Windows Qt's move() works natively."""
    pos_x, pos_y = pos if pos else (60, 700)
    if sys.platform.startswith("linux"):
        import json
        import subprocess
        try:
            mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"],
                                             capture_output=True, text=True).stdout)
            mon = next((m for m in mons if m.get("focused")), mons[0])
            x = int(mon["x"]) + pos_x
            y = int(mon["y"]) + pos_y
            subprocess.run(["hyprctl", "dispatch", "movewindowpixel",
                            f"exact {x} {y},class:^(hytale-tunnel)$"], capture_output=True)
        except Exception:
            pass
    else:
        scr = app.primaryScreen().availableGeometry()
        ui.move(scr.left() + pos_x, scr.top() + pos_y)


def main() -> int:
    ap = argparse.ArgumentParser(prog="hytale-tunnel",
                                 description="Encrypted in-game chat tunnel overlay.")
    ap.add_argument("-r", "--recipient", help="default friend to send to")
    ap.add_argument("--party", help="which party group key '/p' sends to (default: the "
                    "group named 'party', or your only group). Set up with "
                    "'hytalecrypt gengroupkey' + 'setgroupkey <name> <key>'")
    ap.add_argument("--me", help="your in-game name (auto-detected from the client "
                    "log if omitted); used to render your own messages as 'you'")
    ap.add_argument("--font", help="chat font family (any installed font, e.g. 'Fira Code'); "
                    "default: your saved choice or monospace. Change it live with '/font <name>' "
                    "in the compose box; list installed fonts with '/font list'.")
    ap.add_argument("--show-system", action="store_true",
                    help="also show server/console lines ([!], [Duel], joins, …); by "
                         "default only messages sent by players are shown")
    ap.add_argument("--tunnel-only", action="store_true",
                    help="show ONLY encrypted tunnel messages (classic private-channel "
                         "mode) instead of mirroring all player chat")
    ap.add_argument("--open-key", default="Return",
                    help="key that opens the in-game chat input (default: Return)")
    ap.add_argument("--paste-method", choices=["type", "ctrl-v", "shift-insert"],
                    default="ctrl-v",
                    help="how to put text in chat: 'ctrl-v' (default) pastes via ydotool "
                         "(needs ydotoold; tune T_PASTE_SETTLE in send_linux.py), or 'type' "
                         "key-by-key as a fallback")
    ap.add_argument("--type-delay", type=int, default=8,
                    help="ms between keystrokes when typing (lower = faster; raise if "
                         "characters get dropped). default 8")
    ap.add_argument("--send-method", choices=["type", "inject"], default="inject",
                    help="how to send: 'inject' (default) pushes the message straight onto the "
                         "game's QUIC stream via ptrace injection -- instant, no chatbox "
                         "(Linux, needs root/pkexec; the injector launches on your first send); "
                         "'type' types into the in-game chatbox instead")
    ap.add_argument("--interval", type=float, default=0.2, help="fast poll seconds")
    ap.add_argument("--sweep", type=float, default=3.0,
                    help="seconds between full discovery sweeps (lower = catch messages "
                         "in new regions faster, but more CPU)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel reader threads per sweep")
    ap.add_argument("--max-region", type=int, default=128 * 1024 * 1024,
                    help="skip memory regions larger than N bytes during sweeps "
                         "(default 128MB; chat lives in small regions, so this skips the "
                         "huge GPU/heap arenas and makes sweeps faster). 0 = no limit.")
    ap.add_argument("--mark-seen", action="store_true",
                    help="record all messages currently in memory as seen, then exit "
                         "(open the in-game chat first to bake in the backlog)")
    args = ap.parse_args()

    if args.mark_seen:
        n = memscan.mark_all_seen()
        print(f"Marked {n} message(s) as seen; they won't appear in the overlay.")
        return 0

    friends = crypto.list_psk_friends()
    if not friends:
        print("No shared keys yet. Set one up:\n"
              "  hytalecrypt genkey           # generate, share with your friend\n"
              "  hytalecrypt setkey <name> <key>", file=sys.stderr)
        return 1
    # Restore saved layout (position/size/font/recipient). Recipient precedence:
    # explicit --recipient, else the saved one, else "Revenir" if we hold a key for it,
    # else the first friend.
    state = _load_state()
    recipient = (args.recipient or state.get("recipient")
                 or ("Revenir" if "Revenir" in friends else friends[0]))
    if recipient not in friends:
        recipient = friends[0]
    saved_font = state.get("font_px") if isinstance(state.get("font_px"), int) else None
    # Font family: explicit --font > saved choice > default (monospace, handled by Overlay).
    saved_family = args.font or (state.get("font_family")
                                 if isinstance(state.get("font_family"), str) else None)
    saved_size = ((state["w"], state["h"]) if isinstance(state.get("w"), int)
                  and isinstance(state.get("h"), int) else None)
    # Defend against a corrupted (grown) passive height leaking in from an older build:
    # the passive HUD should be short; an absurd height would make it (and the ×grow on
    # open) fill the screen. Clamp back to a sane default.
    if saved_size and saved_size[1] > 550:
        saved_size = (saved_size[0], 320)
    saved_pos = ((state["x"], state["y"]) if isinstance(state.get("x"), int)
                 and isinstance(state.get("y"), int) else None)
    party_group = _pick_party_group(crypto.list_groups(), args.party)

    # PASSIVE (floating-HUD) window geometry = the short size/position you leave it at while
    # gaming. Opening the chat grows it ~2x taller, bottom-anchored; closing restores this.
    # It's refreshed from the LIVE window (so dragging it sticks) and is the geometry we save.
    passive_geo = {
        "x": saved_pos[0] if saved_pos else 60,
        "y": saved_pos[1] if saved_pos else 700,
        "w": saved_size[0] if saved_size else 440,
        "h": saved_size[1] if saved_size else 320,
    }

    # Silence harmless Qt warnings: the host-portal registration (custom app-id with no
    # installed .desktop) and the AT-SPI accessibility adaptor. We need neither.
    os.environ.setdefault("QT_ACCESSIBILITY", "0")
    _rules = os.environ.get("QT_LOGGING_RULES", "")
    os.environ["QT_LOGGING_RULES"] = ((_rules + ";" if _rules else "")
                                      + "qt.qpa.services=false;qt.accessibility.atspi=false")
    # Set the Wayland app-id to "hytale-tunnel" so Hyprland window rules/binds can
    # match by class. app-id is set at window creation (reliable), unlike the title
    # which Qt sets slightly later -- the title race left rules unapplied at random.
    QtWidgets.QApplication.setDesktopFileName("hytale-tunnel")
    app = QtWidgets.QApplication(sys.argv)
    ui = Overlay(recipient, friends, font_px=saved_font, size=saved_size,
                 font_family=saved_family)
    ui.refresh_friends(friends, crypto.list_incoming_requests())   # surface leftover requests

    my_name = playername.detect(args.me)

    # Receive thread -> thread-safe queue -> drained on the Qt main thread.
    # Each item is (SYS, str) for a status line or (MSG, chatframe.Msg) for chat.
    SYS, MSG = object(), object()
    inbox: queue.Queue = queue.Queue()
    stop = threading.Event()
    proc_holder: list = []               # holds the elevated capture process (Linux)
    seen = None
    # Who /r replies to: last friend we privately messaged, or who last privately
    # messaged us (whichever happened most recently).
    last_contact = {"name": None}

    # Send by injecting straight onto the game's QUIC stream (ptrace) instead of typing
    # into the chatbox. The elevated (pkexec) injector is launched LAZILY on the first send
    # -- NOT at startup -- so its pkexec prompt doesn't collide with the receiver's (two
    # simultaneous pkexec prompts at launch made the receiver silently fail to start).
    _inject = {"proc": None}
    _inject_lock = threading.Lock()

    def get_injector():
        if args.send_method != "inject" or not LINUX:
            return None
        with _inject_lock:
            if _inject["proc"] is None:
                inbox.put((SYS, "starting injector (approve the pkexec prompt)…"))
                inj = inject_client.Injector(os.getpid())
                proc_holder.append(inj.proc)
                _inject["proc"] = inj
            return _inject["proc"]

    if LINUX:
        # Full mirror: capture every chat-log line at quiche's decrypt boundary (eBPF),
        # in the server's canonical order, with the real sender from the frame.
        from . import receiver_quiche
        scanner = threading.Thread(
            target=receiver_quiche.watch,
            kwargs=dict(
                on_message=lambda m: inbox.put((MSG, m)),
                on_ready=lambda: inbox.put((SYS, "ready — mirroring chat (quiche)")),
                stop=stop, proc_holder=proc_holder, my_name=my_name,
                show_system=args.show_system, tunnel_only=args.tunnel_only,
                debug_log=os.environ.get("HYTALE_DEBUG")),
            daemon=True,
        )
    else:
        # Windows fallback: memory scanning (best-effort, tunnel messages only). No
        # frame context, so no full mirror and no frame-order guarantee.
        seen = memscan.SeenStore()
        scanner = threading.Thread(
            target=memscan.watch,
            kwargs=dict(
                on_message=lambda sender, text: inbox.put(
                    (MSG, Msg(sender=sender, body=text, kind="whisper_in",
                              is_tunnel=True))),
                on_ready=lambda: inbox.put((SYS, "ready — watching for messages")),
                interval=args.interval, sweep_interval=args.sweep,
                max_region=args.max_region, seen=seen, workers=args.workers, stop=stop),
            daemon=True,
        )

    # Global collapse/expand toggle from anywhere (even when the game is focused):
    # a Hyprland keybind sends SIGUSR1 to this process. The flag is consumed by the
    # drain timer so the toggle happens on the Qt main thread.
    toggle = {"pending": False, "quit": False, "font": 0, "open": False,
              "open_slash": False}
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: toggle.__setitem__("pending", True))
    # Global font resize (SUPER+SHIFT+± via Hyprland -> real-time signals). RT signals
    # queue in the kernel, so rapid presses accumulate instead of being coalesced; the
    # handler just tallies the net delta, applied on the Qt thread by the drain timer.
    # SIGRTMIN+3 = "open & focus the chat" (the '\' quick-open bind).
    if hasattr(signal, "SIGRTMIN"):
        signal.signal(signal.SIGRTMIN + 1, lambda *_: toggle.__setitem__("font", toggle["font"] + 1))
        signal.signal(signal.SIGRTMIN + 2, lambda *_: toggle.__setitem__("font", toggle["font"] - 1))
        signal.signal(signal.SIGRTMIN + 3, lambda *_: toggle.__setitem__("open", True))
        # SIGRTMIN+4 = "open & focus the chat, pre-filled with '/'" (the slash quick-command key).
        signal.signal(signal.SIGRTMIN + 4, lambda *_: toggle.__setitem__("open_slash", True))
    # Graceful shutdown so the `finally` block runs and removes the PID file
    # (SIGTERM/SIGINT would otherwise kill us without cleanup, leaving a stale pid).
    for _signame in ("SIGTERM", "SIGINT"):
        if hasattr(signal, _signame):
            signal.signal(getattr(signal, _signame),
                          lambda *_: toggle.__setitem__("quit", True))

    def _focus_window(cls: str) -> None:
        """Give Hyprland keyboard focus to a window by class (Wayland won't let a client
        focus itself, so ask the compositor — same mechanism as the SUPER+SHIFT+P bind)."""
        if LINUX:
            subprocess.run(["hyprctl", "dispatch", "focuswindow", f"class:^({cls})$"],
                           capture_output=True)

    # --- Enter-hotkey (dynamic Hyprland bind) ---
    # Enter focuses the compose bar, but ONLY when the tunnel is EXPANDED and the GAME is
    # focused. When the tunnel itself has focus, Enter must submit (bind removed); in pill
    # mode it's off entirely. A permanently-global Enter bind would hijack Enter everywhere
    # and make submitting impossible, so we add/remove it via hyprctl as state changes.
    _bind_state = {"on": None}

    def _game_focused() -> bool:
        if not LINUX:
            return False
        try:
            aw = json.loads(subprocess.run(["hyprctl", "-j", "activewindow"],
                                           capture_output=True, text=True).stdout or "{}")
            return aw.get("class") == "HytaleClient"
        except Exception:                            # noqa: BLE001
            return False

    def _set_enter_bind(on: bool) -> None:
        # Manages BOTH quick-open keys, armed under the same condition (tunnel expanded + game
        # focused): Enter opens the compose bar; '/' opens it pre-filled with '/' for commands.
        if not LINUX or _bind_state["on"] is on:
            return
        _bind_state["on"] = on
        if on:
            pid = os.getpid()
            enter = f"/usr/bin/kill --signal RTMIN+3 {pid} 2>/dev/null"
            slash = f"/usr/bin/kill --signal RTMIN+4 {pid} 2>/dev/null"
            subprocess.run(["hyprctl", "keyword", "bind", f", Return, exec, {enter}"],
                           capture_output=True)
            subprocess.run(["hyprctl", "keyword", "bind", f", slash, exec, {slash}"],
                           capture_output=True)
        else:
            subprocess.run(["hyprctl", "keyword", "unbind", ", Return"], capture_output=True)
            subprocess.run(["hyprctl", "keyword", "unbind", ", slash"], capture_output=True)

    def _update_enter_bind() -> None:
        # Enter hotkey ON only while the tunnel is EXPANDED and the GAME is the focused
        # window -> so Enter focuses the compose bar while gaming, but never hijacks Enter
        # in the tunnel itself (submit) or in other apps (terminal, browser).
        _set_enter_bind((not ui._collapsed) and _game_focused())

    # Keep Hyprland's follow-mouse OFF for the whole tunnel session: hovering the overlay must
    # NOT focus it (that was auto-opening/growing it), and while typing the cursor can drift
    # over the game without stealing focus back. The user's original setting is restored on
    # exit. (Focus then changes only by click or by our hotkeys -- exactly what we want.)
    _mouse = {"off": False, "orig": None}

    def _mouse_focus_off(off: bool) -> None:
        if not LINUX or _mouse["off"] is off:
            return
        if _mouse["orig"] is None:
            try:
                out = subprocess.run(["hyprctl", "getoption", "input:follow_mouse"],
                                     capture_output=True, text=True).stdout
                _mouse["orig"] = next((int(w.split()[1]) for w in out.splitlines()
                                       if w.strip().startswith("int:")), 1)
            except Exception:                        # noqa: BLE001
                _mouse["orig"] = 1
        _mouse["off"] = off
        val = 0 if off else _mouse["orig"]
        subprocess.run(["hyprctl", "keyword", "input:follow_mouse", str(val)],
                       capture_output=True)

    def _refresh_mouse_focus() -> None:
        """Keep follow-mouse OFF while the GAME or the CHAT is the focused window (so hovering
        the overlay can't focus/open it, and typing is stable), and restore the user's setting
        when some other app is focused. Polled + called on focus changes."""
        _mouse_focus_off(ui.isActiveWindow() or _game_focused())

    def _cursor_over_overlay() -> bool:
        """True if the mouse cursor is currently over the tunnel window -- used to tell a
        CLICK on the overlay (cursor on it) from a hotkey focus (cursor elsewhere)."""
        if not LINUX:
            return False
        try:
            cp = json.loads(subprocess.run(["hyprctl", "-j", "cursorpos"],
                                           capture_output=True, text=True).stdout or "{}")
            clients = json.loads(subprocess.run(["hyprctl", "-j", "clients"],
                                                capture_output=True, text=True).stdout or "[]")
            w = next((c for c in clients if c.get("class") == "hytale-tunnel"), None)
            if not w:
                return False
            ax, ay = w["at"]
            ww, hh = w["size"]
            return ax <= cp.get("x", -1) < ax + ww and ay <= cp.get("y", -1) < ay + hh
        except Exception:                            # noqa: BLE001
            return False

    def _focus_overlay() -> None:
        _focus_window("hytale-tunnel")
        ui.raise_()
        ui.activateWindow()
        ui.input.setFocus()
        _set_enter_bind(False)                       # tunnel now focused -> Enter must submit

    def _focus_game() -> None:
        _focus_window("HytaleClient")
        QtCore.QTimer.singleShot(60, _update_enter_bind)   # let focus settle, then re-arm

    def _on_activation(active: bool) -> None:
        _glog("activation", active=active, opened=ui._opened, collapsed=ui._collapsed)
        _update_enter_bind()
        _refresh_mouse_focus()
        # If the PASSIVE HUD got focus from a mouse CLICK on it (cursor over it) rather than a
        # hotkey, the user didn't ask to open it -> bounce focus straight back to the game and
        # don't grow. (Only while the game is on this workspace, so a click elsewhere still
        # opens it normally.)
        if (active and not ui._opened and not ui._collapsed
                and _cursor_over_overlay() and _game_on_active_ws()):
            _glog("activation.bounce")
            QtCore.QTimer.singleShot(0, _focus_game)
            return
        changed = active != ui._opened
        ui.set_opened(active)                         # focused => full history; else => fading HUD
        if LINUX and changed and not ui._collapsed:
            _open_grow() if active else _close_shrink()   # taller when opened; short HUD when not
    ui.activation_changed.connect(_on_activation)

    # --- /friend: X25519 key-agreement handshake over the public /msg channel ---
    def _send_line(line: str) -> None:
        """Send one raw chat line via the public path (inject or type), off the Qt thread."""
        def _do() -> None:
            try:
                inj = get_injector()
                if inj is not None:
                    inj.send("public", None, line)
                else:
                    send.send_public(line, open_key=args.open_key,
                                     paste_method=args.paste_method, type_delay_ms=args.type_delay)
            except Exception as e:                   # noqa: BLE001
                inbox.put((SYS, f"send failed: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _do_friend(text: str) -> None:
        parts = text.split(None, 2)                  # ['/friend', 'add', 'Bob']
        sub = parts[1].lower() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        if sub not in ("add", "accept", "remove") or not name:
            inbox.put((SYS, "usage: /friend add|accept|remove <player>"))
            return
        if sub == "add":
            crypto.record_outgoing_request(name)
            _send_line(f"/msg {name} {crypto.hs_add_token()}")
            inbox.put((SYS, f"friend request sent to {name} — have them run: "
                            f"/friend accept {my_name or 'you'}"))
        elif sub == "accept":
            pub = crypto.take_incoming_request(name)
            if pub is None:
                inbox.put((SYS, f"no pending friend request from {name}"))
                return
            key = crypto.save_derived_friend_key(name, pub)
            _send_line(f"/msg {name} {crypto.hs_accept_token()}")
            inbox.put((SYS, f"now friends with {name} · key {crypto.key_fingerprint(key)} "
                            f"(verify it matches theirs)"))
        else:                                        # remove
            msg = (f"removed friend {name}" if crypto.remove_friend(name)
                   else f"no such friend: {name}")
            inbox.put((SYS, msg))
        ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())

    def _maybe_handshake(msg) -> bool:
        """If `msg` is an X25519 handshake line, process it and return True (swallow it)."""
        if msg.kind not in ("whisper_in", "whisper_out"):
            return False
        parsed = crypto.parse_hs_token(msg.body)
        if not parsed:
            return False
        if msg.is_self:
            return True                              # our own HXK echo -> don't display it
        marker, their_pub = parsed
        name = msg.sender
        if marker == crypto.HS_ADD:
            crypto.record_incoming_request(name, their_pub)
            inbox.put((SYS, f"{name} wants to be friends — run: /friend accept {name}"))
        elif not crypto.has_outgoing_request(name):
            inbox.put((SYS, f"ignored unexpected friend-accept from {name}"))
            return True
        else:                                        # HS_ACCEPT to an add we sent
            key = crypto.save_derived_friend_key(name, their_pub)
            crypto.clear_outgoing_request(name)
            inbox.put((SYS, f"{name} accepted — now friends · key {crypto.key_fingerprint(key)} "
                            f"(verify it matches theirs)"))
        ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())
        return True

    ui.friend_action.connect(lambda action, nm: _do_friend(f"/friend {action} {nm}"))

    def drain() -> None:
        if toggle["quit"]:
            persist()                        # save layout while the window is still mapped
            app.quit()
            return
        if toggle["pending"]:
            toggle["pending"] = False
            ui.toggle_collapsed()
        if toggle["open"]:                   # Enter hotkey: focus the compose bar
            toggle["open"] = False
            # Only focus from the PASSIVE HUD; in pill mode Enter must do nothing to the
            # tunnel (use SUPER+SHIFT+J to expand the pill). Guard also stops a stray Return
            # bind (left armed for a moment) from popping the chat open out of the pill.
            if not ui._collapsed:
                _focus_overlay()
        if toggle["open_slash"]:             # '/' hotkey: open + pre-fill '/' for a command
            toggle["open_slash"] = False
            if not ui._collapsed:            # like Enter, only opens from the passive HUD
                _focus_overlay()
                QtCore.QTimer.singleShot(80, lambda: ui.prefill("/"))
        if toggle["font"]:
            delta, toggle["font"] = toggle["font"], 0
            ui.bump_font(delta)
        try:
            while True:
                tag, payload = inbox.get_nowait()
                if tag is SYS:
                    ui.add_system(payload)
                elif _maybe_handshake(payload):
                    continue                     # /friend handshake -> consumed, not shown
                else:
                    ui.add_message(payload)
                    if payload.kind == "whisper_in" and not payload.is_self:
                        last_contact["name"] = payload.sender
        except queue.Empty:
            pass

    timer = QtCore.QTimer()
    timer.timeout.connect(drain)
    timer.start(200)

    # Make signal-driven hotkeys (SUPER+SHIFT+J collapse, the Enter/'\' quick-open, font
    # resize) react the INSTANT the signal lands instead of waiting up to one 200ms drain
    # tick -- that tick was why Enter felt laggy next to SUPER+SHIFT+P (which goes straight
    # through hyprctl). Python writes the signal number to a self-pipe; a QSocketNotifier
    # wakes the Qt loop and drains on the next turn (by which point the Python signal handler
    # has set the toggle flag). The 200ms timer above stays as a belt-and-suspenders fallback.
    _sig_socks = None
    if LINUX and hasattr(signal, "set_wakeup_fd"):
        _wsock, _rsock = socket.socketpair()
        _wsock.setblocking(False)
        _rsock.setblocking(False)
        signal.set_wakeup_fd(_wsock.fileno())
        _sig_notifier = QtCore.QSocketNotifier(
            _rsock.fileno(), QtCore.QSocketNotifier.Type.Read)

        def _on_signal_wakeup(*_) -> None:
            try:
                _rsock.recv(4096)                # clear the wake byte(s)
            except OSError:
                pass
            QtCore.QTimer.singleShot(0, drain)   # flags are set by then; process immediately
        _sig_notifier.activated.connect(_on_signal_wakeup)
        _sig_socks = (_wsock, _rsock, _sig_notifier)   # keep alive for the app's lifetime

    def _do_font(text: str) -> None:
        arg = text[len("/font"):].strip()
        if not arg:
            inbox.put((SYS, f"current font: {ui._font_family}.  Usage: /font <family> "
                            "(e.g. /font Fira Code).  '/font list' shows installed fonts; "
                            "'/font monospace' resets."))
            return
        if arg.lower() == "list":
            fams = ui.available_fonts()
            inbox.put((SYS, f"{len(fams)} fonts installed. A few: "
                            + ", ".join(fams[:40]) + (" …" if len(fams) > 40 else "")))
            return
        found = ui.set_font_family(arg)
        inbox.put((SYS, f"font set to {arg}" if found
                   else f"'{arg}' not found — applied closest match. '/font list' to see options."))
        persist()

    def on_submit(text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.split(None, 1)[0].lower() == "/friend":
            _do_friend(text)
            if LINUX:
                QtCore.QTimer.singleShot(60, _focus_game)
            return
        if text.split(None, 1)[0].lower() == "/font":
            _do_font(text)                       # keep the chat focused so you can try more
            return
        # A GIF is a normal ENCRYPTED private message whose plaintext is "HXG1 <url>";
        # the recipient's overlay detects the sentinel, downloads the URL and animates it.
        gif_url = ""
        if text.split(None, 1)[0].lower() == "/gif":
            gif_url = text[len("/gif"):].strip()
            if not gif_util.valid_url(gif_url):
                inbox.put((SYS, "usage: /gif <direct .gif URL> (http/https)"))
                return
            if ui.recipient not in crypto.list_psk_friends():
                inbox.put((SYS, "select a friend you share a key with — GIFs go over the "
                                "encrypted channel"))
                return
            gif_util.push_recent(gif_url)
            mode, friend, body = "private", ui.recipient, chatframe.GIF_SENTINEL + gif_url
        else:
            mode, friend, body = _parse_command(text, last_contact)
            if mode == "error":
                inbox.put((SYS, body))
                return

        # Windows/memscan dedups own sends by token hash so the scanner doesn't show
        # our own message back as if the friend sent it. On Linux the quiche frame
        # carries the real sender, so no ledger is needed -- our own line comes back
        # through the stream (in correct order) and renders as 'you'.
        def _ledger(blob: str) -> None:
            if seen is not None:
                b = blob[3:] if blob[:3] in (crypto.SYM_MARKER, crypto.CHUNK_MARKER) else blob
                seen.add(memscan.token_hash(b))

        # Sending sleeps + shells out (focus, paste, keystrokes); do it OFF the Qt
        # main thread so the overlay can never freeze if a subprocess stalls.
        if mode == "private":
            last_contact["name"] = friend
            # On Linux we DON'T echo optimistically: the server echoes our /msg back
            # through quiche, so showing it now would both duplicate it and risk wrong
            # ordering. On Windows (no stream) we echo immediately.
            if not LINUX:
                ui.add_message(Msg(sender="you", body=body, kind="whisper_out",
                                   is_self=True, is_tunnel=True, target=friend,
                                   is_gif=bool(gif_url), gif_url=gif_url))

            def _do_send() -> None:
                try:
                    inj = get_injector()
                    if inj is not None:
                        inj.send("private", friend, body)
                    else:
                        send.send_message(friend, body, open_key=args.open_key,
                                          pre_send=_ledger, paste_method=args.paste_method,
                                          type_delay_ms=args.type_delay)
                except Exception as e:               # noqa: BLE001 - surface to overlay
                    inbox.put((SYS, f"send failed: {e}"))
        elif mode == "party":                        # encrypted party (shared group key)
            group = party_group
            if not group:
                inbox.put((SYS, "no party key set up"))
                return
            # On Linux the quiche mirror echoes our own /p line back (my_name match ->
            # 'you'); on Windows there's no mirror, so echo immediately.
            if not LINUX:
                ui.add_message(Msg(sender="you", body=body, kind="party",
                                   is_self=True, is_tunnel=True))

            def _do_send() -> None:
                try:
                    inj = get_injector()
                    if inj is not None:
                        inj.send("party", group, body)
                    else:
                        send.send_party(group, body, open_key=args.open_key,
                                        pre_send=_ledger, paste_method=args.paste_method,
                                        type_delay_ms=args.type_delay)
                except Exception as e:               # noqa: BLE001 - surface to overlay
                    inbox.put((SYS, f"send failed: {e}"))
        else:  # public -- plain in-game chat, unencrypted
            # On Linux the quiche mirror echoes our own public line back (my_name
            # match -> is_self); no public mirror on Windows, so echo immediately.
            if not LINUX:
                ui.add_message(Msg(sender="you", body=body, kind="public", is_self=True))

            def _do_send() -> None:
                try:
                    inj = get_injector()
                    if inj is not None:
                        inj.send("public", None, body)
                    else:
                        send.send_public(body, open_key=args.open_key,
                                         paste_method=args.paste_method,
                                         type_delay_ms=args.type_delay)
                except Exception as e:               # noqa: BLE001 - surface to overlay
                    inbox.put((SYS, f"send failed: {e}"))
        threading.Thread(target=_do_send, daemon=True).start()
        # After sending, hand keyboard focus back to the game (so you can keep playing;
        # this also re-arms the Enter hotkey since the overlay is no longer focused).
        if LINUX:
            QtCore.QTimer.singleShot(60, _focus_game)

    ui.submitted.connect(on_submit)
    # GIF picker: clicking a GIF sends it (reuse the /gif path); ✕/☆ save/unsave a favorite.
    ui.gif_send.connect(lambda url: on_submit("/gif " + url))

    def _do_gif_action(action: str, url: str) -> None:
        if action == "add":
            gif_util.add_favorite(url)
        elif action == "unfav":
            gif_util.remove_favorite(url)
        elif action == "forget":              # delete entirely (favorites AND recents)
            gif_util.forget(url)
    ui.gif_action.connect(_do_gif_action)
    # Empty Enter in the compose box = "dismiss": hand focus back to the game (which also
    # re-arms the Enter hotkey), so a second Enter toggles the chat closed like SUPER+SHIFT+P.
    if LINUX:
        ui.dismissed.connect(lambda: QtCore.QTimer.singleShot(0, _focus_game))

    if memscan.find_client_pid() is None:
        ui.add_system("HytaleClient not running — waiting…")
    party_note = f" · party: {party_group}" if party_group else ""
    ui.add_system(f"tunnel up · recipient: {recipient} · friends: "
                  f"{', '.join(friends)}{party_note}")

    # PID file so a Hyprland keybind can signal exactly this process (SIGUSR1
    # toggles the overlay) without a broad pkill that could hit other processes.
    pidfile = crypto.CONFIG_DIR / "tunnel.pid"
    try:
        crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pidfile = None

    # Window sizing goes through Hyprland: it ignores a floating client's own resize()
    # (honoring only size *hints*, which is why the pill's setFixedSize works but a plain
    # resize doesn't), so we drive size with `resizewindowpixel` and then position with
    # `movewindowpixel` -- the same compositor-side path we already use for moving.
    _TOP_MARGIN = 8
    _GROW = 1.6                       # opened height = _GROW × passive height (bottom-anchored)
    # Geometry is "in flux" until this monotonic time; persist() won't read the live size
    # during the window, so a transient (grown, or mid-resize) height can't be captured as
    # the passive base -- that race is what leaked the ~2× height into overlay.json before.
    geo_state = {"settle_until": 0.0, "gen": 0}

    def _apply_geo(x_rel: int, y_rel: int, w: int, h: int) -> None:
        """Resize (Hyprland) + position (monitor-relative). Coords are monitor-relative,
        like _query_geometry_linux / _position_top_right. Runs twice: the second pass is a
        safety net for the moment just after a pill-expand, where Qt hasn't yet committed its
        cleared min/max size hints and the first resize could otherwise be clamped.

        Each call bumps a generation; a queued pass runs only if it's still the newest call.
        Without this, a fast open->close left the OPEN's 150ms pass to fire AFTER the close and
        re-grow the window while it was 'closed' -- persist() then captured that grown height as
        the passive base, so every cycle it crept taller and higher (the reported bug)."""
        geo_state["settle_until"] = time.monotonic() + 0.7   # don't persist size mid-resize
        geo_state["gen"] += 1
        my_gen = geo_state["gen"]
        def go() -> None:
            if my_gen != geo_state["gen"]:       # a newer geometry change superseded this one
                return
            if LINUX:
                # Resize AND move in ONE atomic --batch dispatch so there's no visible
                # move-then-resize two-step. Paired with `no_anim on`, the window snaps to the
                # new geometry instantly. movewindowpixel wants GLOBAL coords, so add the focused
                # monitor's origin to the monitor-relative x/y.
                ox = oy = 0
                try:
                    mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"],
                                                     capture_output=True, text=True).stdout)
                    mon = next((m for m in mons if m.get("focused")), mons[0] if mons else {})
                    ox, oy = int(mon.get("x", 0)), int(mon.get("y", 0))
                except Exception:                # noqa: BLE001
                    pass
                cls = "class:^(hytale-tunnel)$"
                subprocess.run(["hyprctl", "--batch",
                                f"dispatch resizewindowpixel exact {int(w)} {int(h)},{cls} ; "
                                f"dispatch movewindowpixel exact {ox + int(x_rel)} {oy + int(y_rel)},{cls}"],
                               capture_output=True)
            else:
                ui.resize(int(w), int(h))
                _position_top_right(ui, app, (x_rel, y_rel))
            if _GEO_DBG:
                _glog("apply", gen=my_gen, target=(int(x_rel), int(y_rel), int(w), int(h)),
                      collapsed=ui._collapsed, opened=ui._opened)
                QtCore.QTimer.singleShot(220, lambda: _glog(
                    "apply.result", target=(int(x_rel), int(y_rel), int(w), int(h)),
                    actual=_query_geometry_linux()))
        for delay in (0, 150):
            QtCore.QTimer.singleShot(delay, go)

    # open/close geometry is FULLY DETERMINISTIC: it computes purely from the stored
    # passive_geo and never reads the live window. That closes the feedback loop that made the
    # window creep up/taller -- a bad live read (mid-transition, during a desktop switch, focus
    # flapping) could otherwise get written back as the new base and compound every cycle. The
    # only thing that ever updates passive_geo from the live window is persist(), and only when
    # the window is confirmed to be AT passive size (so grown/transient geometry can't leak in).
    def _open_grow() -> None:
        """passive -> opened: grow taller (×_GROW), bottom-anchored, from the stored base."""
        # Pick up a fresh passive drag right now, so open uses the CURRENT spot instead of a
        # stale one (the persist poll can lag ~seconds behind a move). Guarded: only trust the
        # read when the live window is at ≈ passive size, else it's a transient/pill frame.
        g = _query_geometry_linux() if LINUX else None
        if g and abs(g[2] - passive_geo["w"]) <= 12 and abs(g[3] - passive_geo["h"]) <= 12:
            passive_geo["x"], passive_geo["y"] = g[0], g[1]
            _glog("open.sync", live=g)
        elif g:
            _glog("open.sync-reject", live=g, pw=passive_geo["w"], ph=passive_geo["h"])
        x, y = passive_geo["x"], passive_geo["y"]
        w, h = passive_geo["w"], passive_geo["h"]
        new_h = max(h, int(min(_GROW * h, y + h - _TOP_MARGIN)))   # don't run off the monitor top
        _glog("open.grow", pg=(x, y, w, h), target=(x, y + h - new_h, w, new_h))
        _apply_geo(x, y + h - new_h, w, new_h)       # keep the bottom fixed; top rises

    def _close_shrink() -> None:
        """opened -> passive: restore the passive geometry. If the OPENED window was dragged
        somewhere, capture that -- but ONLY when the live read really is the opened frame (size
        ≈ the grown size). A transient/mid-transition/passive-sized read is rejected, so nothing
        bogus can feed back into the base (no drift)."""
        x0, y0 = passive_geo["x"], passive_geo["y"]
        w0, h0 = passive_geo["w"], passive_geo["h"]
        oh = max(h0, int(min(_GROW * h0, y0 + h0 - _TOP_MARGIN)))   # the height we grew to
        g = _query_geometry_linux() if LINUX else None
        if g and abs(g[2] - w0) <= 12 and abs(g[3] - oh) <= 12:      # a genuine opened frame
            passive_geo["x"] = g[0]
            passive_geo["y"] = max(0, g[1] + g[3] - h0)             # keep the (maybe dragged) bottom
            _glog("close.capture", live=g, oh=oh, pg=(passive_geo["x"], passive_geo["y"]))
        else:
            _glog("close.reject", live=g, oh=oh, w0=w0, h0=h0)
        _apply_geo(passive_geo["x"], passive_geo["y"], passive_geo["w"], passive_geo["h"])

    def _place_passive() -> None:
        _glog("place_passive", pg=(passive_geo["x"], passive_geo["y"],
                                   passive_geo["w"], passive_geo["h"]))
        _apply_geo(passive_geo["x"], passive_geo["y"], passive_geo["w"], passive_geo["h"])

    def _game_on_active_ws() -> bool:
        """True if HytaleClient is on the currently-focused workspace. Lets collapse hand
        focus back to the game WHEN the pill was covering the game, without yanking you to
        the game's desktop if you collapse it from some other workspace."""
        if not LINUX:
            return False
        try:
            aw = json.loads(subprocess.run(["hyprctl", "-j", "activeworkspace"],
                                           capture_output=True, text=True).stdout or "{}")
            clients = json.loads(subprocess.run(["hyprctl", "-j", "clients"],
                                                capture_output=True, text=True).stdout or "[]")
            g = next((c for c in clients if c.get("class") == "HytaleClient"), None)
            return bool(g) and g.get("workspace", {}).get("id") == aw.get("id")
        except Exception:                            # noqa: BLE001
            return False

    def _on_collapsed_changed() -> None:
        _glog("collapsed_changed", collapsed=ui._collapsed, opened=ui._opened,
              pg=(passive_geo["x"], passive_geo["y"], passive_geo["w"], passive_geo["h"]))
        _update_enter_bind()                     # pill => Enter hotkey off; expanded => depends
        if ui._collapsed:                        # pill sizes itself; just place it at the spot
            for delay in (0, 130):
                QtCore.QTimer.singleShot(delay,
                                         lambda: _position_top_right(ui, app,
                                                                     (passive_geo["x"], passive_geo["y"])))
        elif ui._opened:                         # expanded straight into the focused state
            _open_grow()
        else:                                    # expanded into the passive HUD
            _place_passive()
        if ui._collapsed:
            # collapsed back to the pill -> hand focus back to the game so you can keep
            # playing without moving the mouse (but only if the game is on this workspace;
            # otherwise just drop focus, so collapsing from another desktop doesn't yank you).
            if _game_on_active_ws():
                QtCore.QTimer.singleShot(0, _focus_game)
            else:
                ui.clearFocus()
        else:
            # enlarged -> focus the compose bar so you can type right away
            QtCore.QTimer.singleShot(180, _focus_overlay)
    ui.collapsed_changed.connect(_on_collapsed_changed)

    # Persist layout so the overlay reopens exactly where it was left. Geometry is read
    # from Hyprland (Wayland hides the frame position from Qt); font/recipient come from
    # the widget. Skip while collapsed (that would save the tiny pill size). Written only
    # when something changed, and once more on exit.
    saved_state = {"last": dict(state)}

    def persist() -> None:
        if ui._collapsed:
            return
        st = {"font_px": ui._font_px, "recipient": ui.recipient,
              "font_family": ui._font_family}
        # Only the PASSIVE HUD size/pos is the persisted base. Capture geometry ONLY when
        # the window is stably at passive size: never while opened (grown ×_GROW), and never
        # during the post-transition settle window -- else the grown/mid-resize height leaks
        # in as the base (the bug that made "open" balloon to >2×). Font/recipient still save.
        geo = None
        if not ui._opened and time.monotonic() >= geo_state["settle_until"]:
            geo = _query_geometry_linux() if LINUX else (
                lambda g: (g.x(), g.y(), g.width(), g.height()))(ui.frameGeometry())
        if geo:
            gx, gy, gw, gh = geo
            # Capture a move ONLY when the live window is at (≈) passive size -- that proves
            # it's the settled passive HUD, not a grown/mid-transition frame. This is the ONLY
            # place passive_geo tracks the live window, so a bad read can never compound into
            # the base (which is what made it creep upward/taller). A pure size change (drag an
            # edge) isn't captured -- position drags are.
            captured = abs(gw - passive_geo["w"]) <= 12 and abs(gh - passive_geo["h"]) <= 12
            if captured:
                passive_geo["x"], passive_geo["y"] = gx, gy
            _glog("persist", live=geo, captured=captured,
                  pg=(passive_geo["x"], passive_geo["y"], passive_geo["w"], passive_geo["h"]))
            st["x"], st["y"] = passive_geo["x"], passive_geo["y"]
            st["w"], st["h"] = passive_geo["w"], passive_geo["h"]
        else:                                    # keep last-known geometry if unreadable/opened
            for k in ("x", "y", "w", "h"):
                if k in saved_state["last"]:
                    st[k] = saved_state["last"][k]
        if st != saved_state["last"]:
            saved_state["last"] = st
            try:
                STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                STATE_PATH.write_text(json.dumps(st))
            except OSError:
                pass

    persist_timer = QtCore.QTimer()
    persist_timer.timeout.connect(persist)
    persist_timer.start(4000)

    scanner.start()
    ui.show()
    _place_passive()
    _refresh_mouse_focus()               # hovering the overlay must not focus/open it

    # Arm the Enter hotkey from whoever is actually focused right now, and re-check every
    # second so a direct game->other-app switch (no overlay focus event) can't leave Enter
    # hijacked in that app OR leave follow-mouse off in some unrelated app.
    def _poll_focus_state() -> None:
        _update_enter_bind()
        _refresh_mouse_focus()
    QtCore.QTimer.singleShot(600, _poll_focus_state)
    enter_timer = QtCore.QTimer()
    enter_timer.timeout.connect(_poll_focus_state)
    enter_timer.start(1000)

    try:
        return app.exec()
    finally:
        persist()                            # capture wherever it was left
        _set_enter_bind(False)               # never leave Enter hijacked after we exit
        _mouse_focus_off(False)              # restore the user's follow-mouse setting
        stop.set()
        for p in proc_holder:                # stop the elevated capture process
            try:
                p.terminate()
            except Exception:
                pass
        if pidfile:
            try:
                pidfile.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
