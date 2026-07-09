"""Glue: memory scanner thread -> overlay transcript; compose box -> encrypt+send.

Run via the `hytale-tunnel` launcher (which puts ~/.local/lib on PYTHONPATH).
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading

from PyQt6 import QtCore, QtWidgets

from . import chatframe, crypto, inject_client, memscan, playername, send
from .chatframe import Msg
from .overlay import Overlay

LINUX = sys.platform.startswith("linux")

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
    saved_size = ((state["w"], state["h"]) if isinstance(state.get("w"), int)
                  and isinstance(state.get("h"), int) else None)
    saved_pos = ((state["x"], state["y"]) if isinstance(state.get("x"), int)
                 and isinstance(state.get("y"), int) else None)
    party_group = _pick_party_group(crypto.list_groups(), args.party)

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
    ui = Overlay(recipient, friends, font_px=saved_font, size=saved_size)
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
    toggle = {"pending": False, "quit": False, "font": 0, "open": False}
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
        if not LINUX or _bind_state["on"] is on:
            return
        _bind_state["on"] = on
        if on:
            cmd = f"/usr/bin/kill --signal RTMIN+3 {os.getpid()} 2>/dev/null"
            subprocess.run(["hyprctl", "keyword", "bind", f", Return, exec, {cmd}"],
                           capture_output=True)
        else:
            subprocess.run(["hyprctl", "keyword", "unbind", ", Return"], capture_output=True)

    def _update_enter_bind() -> None:
        # Enter hotkey ON only while the tunnel is EXPANDED and the GAME is the focused
        # window -> so Enter focuses the compose bar while gaming, but never hijacks Enter
        # in the tunnel itself (submit) or in other apps (terminal, browser).
        _set_enter_bind((not ui._collapsed) and _game_focused())

    # While the tunnel is focused, turn OFF Hyprland's follow-mouse so the cursor can move
    # into the chat (to click/select) without the game stealing focus back. Restore the
    # user's original setting when the tunnel isn't focused.
    _mouse = {"free": None, "orig": None}

    def _set_free_mouse(free: bool) -> None:
        if not LINUX or _mouse["free"] is free:
            return
        if _mouse["orig"] is None:
            try:
                out = subprocess.run(["hyprctl", "getoption", "input:follow_mouse"],
                                     capture_output=True, text=True).stdout
                _mouse["orig"] = next((int(w.split()[1]) for w in out.splitlines()
                                       if w.strip().startswith("int:")), 1)
            except Exception:                        # noqa: BLE001
                _mouse["orig"] = 1
        _mouse["free"] = free
        val = 0 if free else _mouse["orig"]
        subprocess.run(["hyprctl", "keyword", "input:follow_mouse", str(val)],
                       capture_output=True)

    def _focus_overlay() -> None:
        _focus_window("hytale-tunnel")
        ui.raise_()
        ui.activateWindow()
        ui.set_opened(True)                          # show the compose bar first (it's hidden
                                                     # in passive HUD mode) so setFocus can land
        ui.input.setFocus()
        _set_enter_bind(False)                       # tunnel now focused -> Enter must submit
        _set_free_mouse(True)                        # cursor can roam the chat freely

    def _focus_game() -> None:
        _focus_window("HytaleClient")
        _set_free_mouse(False)
        QtCore.QTimer.singleShot(60, _update_enter_bind)   # let focus settle, then re-arm

    def _on_activation(active: bool) -> None:
        _set_free_mouse(active)
        _update_enter_bind()
        ui.set_opened(active)                         # focused => full history; else => fading HUD
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
        if toggle["open"]:                   # Enter hotkey: focus the compose bar (expand first)
            toggle["open"] = False
            ui.set_collapsed(False)
            QtCore.QTimer.singleShot(120, _focus_overlay)
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

    def on_submit(text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.split(None, 1)[0].lower() == "/friend":
            _do_friend(text)
            if LINUX:
                QtCore.QTimer.singleShot(60, _focus_game)
            return
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
                                   is_self=True, is_tunnel=True, target=friend))

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

    # Keep the overlay pinned top-right, including after a collapse/expand resize
    # (Hyprland re-centers floating windows on resize, so re-apply each time).
    # Fire a few times at increasing delays: Hyprland repositions the window itself
    # during its resize animation, so the last (post-animation) placement must win.
    def reposition() -> None:
        for delay in (120, 400, 750):
            QtCore.QTimer.singleShot(delay, lambda: _position_top_right(ui, app, saved_pos))

    def _on_collapsed_changed() -> None:
        reposition()
        _update_enter_bind()                     # pill => Enter hotkey off; expanded => depends
        if ui._collapsed:
            # collapsed back to the pill -> just drop the tunnel's focus; do NOT force the
            # game to the front (that yanks you back after switching desktops / minimizing).
            ui.clearFocus()
            _set_free_mouse(False)                # restore normal follow-mouse
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
        st = {"font_px": ui._font_px, "recipient": ui.recipient}
        geo = _query_geometry_linux() if LINUX else (
            lambda g: (g.x(), g.y(), g.width(), g.height()))(ui.frameGeometry())
        if geo:
            st["x"], st["y"], st["w"], st["h"] = geo
        else:                                    # keep last-known geometry if unreadable
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
    reposition()

    # Arm the Enter hotkey from whoever is actually focused right now, and re-check every
    # second so a direct game->other-app switch (no overlay focus event) can't leave Enter
    # hijacked in that app.
    QtCore.QTimer.singleShot(600, _update_enter_bind)
    enter_timer = QtCore.QTimer()
    enter_timer.timeout.connect(_update_enter_bind)
    enter_timer.start(1000)

    try:
        return app.exec()
    finally:
        persist()                            # capture wherever it was left
        _set_enter_bind(False)               # never leave Enter hijacked after we exit
        _set_free_mouse(False)               # restore the user's follow-mouse setting
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
