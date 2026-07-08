"""Glue: memory scanner thread -> overlay transcript; compose box -> encrypt+send.

Run via the `hytale-tunnel` launcher (which puts ~/.local/lib on PYTHONPATH).
"""

import argparse
import os
import queue
import signal
import sys
import threading

from PyQt6 import QtCore, QtWidgets

from . import chatframe, crypto, memscan, playername, send
from .chatframe import Msg
from .overlay import Overlay


def _parse_command(text: str, last_contact: dict, channel: str):
    """Classify compose-box input based strictly on the selected channel dropdown."""
    
    # Dropdown-driven routing
    if channel == "Public":
        return "public", None, text
    elif channel.lower() == "party":
        if "party" not in crypto.list_psk_friends():
            return "error", None, "no 'party' key set. Run: hytalecrypt setkey party <key>"
        return "party_private", "party", text
    else:
        # A specific friend is selected
        return "private", channel, text


def _position_top_left(ui, app) -> None:
    """Place the overlay at the top-left. Wayland ignores client-set positions, so
    on Linux we ask Hyprland to move it; on Windows Qt's move() works natively."""
    margin_x, margin_y = 12, 50
    if sys.platform.startswith("linux"):
        import json
        import subprocess
        try:
            mons = json.loads(subprocess.run(["hyprctl", "-j", "monitors"],
                                             capture_output=True, text=True).stdout)
            mon = next((m for m in mons if m.get("focused")), mons[0])
            scale = mon.get("scale", 1) or 1
            x = int(mon["x"]) + margin_x
            y = int(mon["y"]) + margin_y
            subprocess.run(["hyprctl", "dispatch", "movewindowpixel",
                            f"exact {x} {y},class:^(hytale-tunnel)$"], capture_output=True)
        except Exception:
            pass
    else:
        scr = app.primaryScreen().availableGeometry()
        ui.move(scr.left() + margin_x, scr.top() + margin_y)


def main() -> int:
    ap = argparse.ArgumentParser(prog="hytale-tunnel",
                                 description="Encrypted in-game chat tunnel overlay.")
    ap.add_argument("-r", "--recipient", help="default friend to send to")
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
    ap.add_argument("--no-quiche", action="store_true",
                    help="Windows: skip the quiche/Frida hook and use the memory "
                         "scanner instead (fallback if the hook misbehaves)")
    ap.add_argument("--font-size", type=int, default=14,
                    help="overlay chat font size in px (default 14; adjust live in the "
                         "overlay with Ctrl++ / Ctrl+- / Ctrl+0)")
    ap.add_argument("--hotkey-open", default="shift+up",
                    help="Windows global hotkey to open the chat (default: shift+up)")
    ap.add_argument("--hotkey-close", default="shift+down",
                    help="Windows global hotkey to close the chat (default: shift+down)")
    ap.add_argument("--hotkey-unfocus", default="shift+left",
                    help="Windows global hotkey to unfocus the chat but leave it expanded (default: shift+left)")
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
    recipient = args.recipient or friends[0]

    os.environ.setdefault("QT_ACCESSIBILITY", "0")
    _rules = os.environ.get("QT_LOGGING_RULES", "")
    os.environ["QT_LOGGING_RULES"] = ((_rules + ";" if _rules else "")
                                      + "qt.qpa.services=false;qt.accessibility.atspi=false")
    QtWidgets.QApplication.setDesktopFileName("hytale-tunnel")
    app = QtWidgets.QApplication(sys.argv)
    ui = Overlay(recipient, friends, font_size=args.font_size)

    my_name = playername.detect(args.me)

    SYS, MSG = object(), object()
    inbox: queue.Queue = queue.Queue()
    stop = threading.Event()
    proc_holder: list = []               # holds the elevated capture process (Linux)
    seen = None
    sent_tokens: set = set()             # our own outgoing tokens, to skip server echo
    last_contact = {"name": None}

    if sys.platform.startswith("linux"):
        from . import receiver_quiche
        scanner = threading.Thread(
            target=receiver_quiche.watch,
            kwargs=dict(
                on_message=lambda m: inbox.put((MSG, m)),
                on_ready=lambda: inbox.put((SYS, "ready — capturing chat (quiche)")),
                stop=stop, proc_holder=proc_holder, my_name=my_name,
                show_system=args.show_system, tunnel_only=args.tunnel_only,
                debug_log=os.environ.get("HYTALE_DEBUG")),
            daemon=True,
        )
    else:
        use_quiche = False
        if not args.no_quiche:
            try:
                from . import receiver_quiche_win
                use_quiche = receiver_quiche_win.available()
            except Exception:
                use_quiche = False
        if use_quiche:
            scanner = threading.Thread(
                target=receiver_quiche_win.watch,
                kwargs=dict(
                    on_message=lambda m: inbox.put((MSG, m)),
                    on_ready=lambda: inbox.put((SYS, "ready — capturing chat (quiche)")),
                    stop=stop, proc_holder=proc_holder, my_name=my_name,
                    show_system=args.show_system, tunnel_only=args.tunnel_only,
                    debug_log=os.environ.get("HYTALE_DEBUG")),
                daemon=True,
            )
        else:
            if not args.no_quiche:
                inbox.put((SYS, "frida/quiche unavailable — using memory scan "
                                "(run setup-windows.bat to enable the quiche hook)"))
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

    toggle = {"pending": False, "quit": False}
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: toggle.__setitem__("pending", True))
    for _signame in ("SIGTERM", "SIGINT"):
        if hasattr(signal, _signame):
            signal.signal(getattr(signal, _signame),
                          lambda *_: toggle.__setitem__("quit", True))

    def drain() -> None:
        if toggle["quit"]:
            app.quit()
            return
        if toggle["pending"]:
            toggle["pending"] = False
            ui.toggle_collapsed()
        try:
            while True:
                tag, payload = inbox.get_nowait()
                if tag is SYS:
                    ui.add_system(payload)
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
        
        channel = ui.recipient
        mode, friend, body = _parse_command(text, last_contact, channel)
        
        if mode == "error":
            inbox.put((SYS, body))
            return

        def _ledger(blob: str) -> None:
            sent_tokens.add(blob)
            if seen is not None:
                b = blob[len(crypto.SYM_MARKER):] if blob.startswith(crypto.SYM_MARKER) else blob
                seen.add(memscan.token_hash(b))

        # We echo immediately if use_quiche is false, otherwise we suppress it.
        if mode == "private":
            last_contact["name"] = friend
            if not sys.platform.startswith("linux") and not use_quiche:
                ui.add_message(Msg(sender="you", body=body, kind="whisper_out",
                                   is_self=True, is_tunnel=True, target=friend))

            def _do_send() -> None:
                try:
                    send.send_message(friend, body, open_key=args.open_key,
                                      pre_send=_ledger, paste_method=args.paste_method,
                                      type_delay_ms=args.type_delay)
                except Exception as e:
                    inbox.put((SYS, f"send failed: {e}"))
        elif mode == "party_private":
            if not sys.platform.startswith("linux") and not use_quiche:
                ui.add_message(Msg(sender="you", body=body, kind="party",
                                   is_self=True, is_tunnel=True, target="party"))

            def _do_send() -> None:
                try:
                    send.send_party_message(body, open_key=args.open_key,
                                            pre_send=_ledger, paste_method=args.paste_method,
                                            type_delay_ms=args.type_delay)
                except Exception as e:
                    inbox.put((SYS, f"send failed: {e}"))
        else:  # public
            if not sys.platform.startswith("linux") and not use_quiche:
                ui.add_message(Msg(sender="you", body=body, kind="public", is_self=True))

            def _do_send() -> None:
                try:
                    send.send_public(body, open_key=args.open_key,
                                     paste_method=args.paste_method,
                                     type_delay_ms=args.type_delay)
                except Exception as e:
                    inbox.put((SYS, f"send failed: {e}"))
        threading.Thread(target=_do_send, daemon=True).start()

    def on_custom_encrypt(channel: str, text: str) -> None:
        if channel == "Public":
            inbox.put((SYS, "Cannot encrypt for the Public channel."))
            return
            
        friend = "party" if channel.lower() == "party" else channel
        if friend not in crypto.list_psk_friends():
            inbox.put((SYS, f"No key set for '{friend}'."))
            return
            
        tokens = crypto.encrypt_messages(friend, text)
        if not tokens:
            return
            
        from PyQt6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        # If it's a long message, they get multiple tokens. We space-separate them.
        cb.setText(" ".join(tokens))
        inbox.put((SYS, f"Encrypted message for {friend} copied to clipboard!"))
        
    def on_custom_decrypt(token: str) -> None:
        m = chatframe.HX_TOKEN_RE.search(token)
        if not m:
            inbox.put((SYS, "No valid HX token found in input."))
            return
            
        tok = m.group(0)
        marker, body64 = tok[:3], tok[3:]
        dec = crypto.try_decrypt_sym(body64, crypto.loaded_psks())
        if dec is None:
            inbox.put((SYS, "Failed to decrypt token (unknown key or invalid data)."))
            return
            
        key_name, payload = dec
        if len(payload) >= 2:
            try:
                text = payload[2:].decode("utf-8")
                inbox.put((SYS, f"Decrypted (Key: {key_name}): {text}"))
            except UnicodeDecodeError:
                inbox.put((SYS, f"Decrypted (Key: {key_name}) but invalid UTF-8."))
        else:
            inbox.put((SYS, f"Decrypted (Key: {key_name}) but payload too short."))
        
    ui.custom_encrypt_requested.connect(on_custom_encrypt)
    ui.custom_decrypt_requested.connect(on_custom_decrypt)
    ui.submitted.connect(on_submit)
    ui.escape_to_game.connect(send.focus_game)

    if memscan.find_client_pid() is None:
        ui.add_system("HytaleClient not running — waiting…")
    ui.add_system(f"tunnel up · recipient: {recipient} · friends: {', '.join(friends)}")

    pidfile = crypto.CONFIG_DIR / "tunnel.pid"
    try:
        crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pidfile = None

    def reposition() -> None:
        if getattr(ui, "_user_moved", False):      # don't fight a manual drag
            return
        for delay in (120, 400, 750):
            QtCore.QTimer.singleShot(delay, lambda: _position_top_left(ui, app))
    if sys.platform.startswith("linux"):
        ui.collapsed_changed.connect(reposition)

    scanner.start()
    ui.show()
    reposition()

    hotkeys = None
    if sys.platform == "win32":
        try:
            from . import hotkeys_win
            hotkeys = hotkeys_win.setup(
                app, ui, memscan.find_client_pid,
                open_spec=args.hotkey_open, close_spec=args.hotkey_close,
                unfocus_spec=args.hotkey_unfocus,
                notify=lambda m: inbox.put((SYS, m)))
        except Exception as e:
            inbox.put((SYS, f"global hotkeys unavailable: {e}"))
    try:
        return app.exec()
    finally:
        stop.set()
        if hotkeys is not None:
            try:
                hotkeys.unregister_all()
            except Exception:
                pass
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
