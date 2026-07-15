"""Glue: memory scanner thread -> overlay transcript; compose box -> encrypt+send.

Run via the `hytale-tunnel` launcher (which puts ~/.local/lib on PYTHONPATH).
"""

import argparse
import os
import queue
import signal
import base64
import html
import sys
import threading

from PyQt6 import QtCore, QtWidgets

from . import chatframe, crypto, memscan, playername, send, gif_util
from .chatframe import Msg
from .overlay import Overlay


def _parse_command(text: str, last_contact: dict, channel: str):
    """Classify compose-box input based strictly on the selected channel dropdown."""
    
    # Dropdown-driven routing
    if channel == "Public":
        return "public", None, text
    elif channel.lower() == "party":
        if crypto.load_group_psk("party") is None:
            return "error", None, "no 'party' key set in groups. Run: hytalecrypt setgroupkey party <key>"
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
    app.setQuitOnLastWindowClosed(False)
    
    settings = QtCore.QSettings("HytaleTunnel", "Overlay")
    font_size = args.font_size
    if settings.contains("font_size"):
        font_size = settings.value("font_size", type=int)

    ui = Overlay(recipient, friends, font_px=font_size)

    my_name = playername.detect(args.me)

    SYS, SYS_HTML, MSG = object(), object(), object()
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
                on_ready=lambda: inbox.put((SYS_HTML, '<span style="color:#4ade80;">Ready - Capturing Chat</span>')),
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
                    on_ready=lambda: inbox.put((SYS_HTML, '<span style="color:#4ade80;">Ready - Capturing Chat</span>')),
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
            def _memscan_on_message(sender: str, text: str):
                is_gif = False
                gif_url = ""
                if text.startswith(chatframe.GIF_SENTINEL):
                    is_gif = True
                    gif_url = text[len(chatframe.GIF_SENTINEL):].strip()
                elif text.startswith("HXG1"):
                    is_gif = True
                    gif_url = text[len("HXG1"):].strip()
                elif "http" in text and (".gif" in text.lower() or ".webp" in text.lower()):
                    is_gif = True
                    gif_url = text.strip()
                inbox.put((MSG, Msg(sender=sender, body=text, kind="whisper_in",
                                    is_tunnel=True, is_gif=is_gif, gif_url=gif_url)))

            scanner = threading.Thread(
                target=memscan.watch,
                kwargs=dict(
                    on_message=_memscan_on_message,
                    on_ready=lambda: inbox.put((SYS_HTML, '<span style="color:#4ade80;">Ready - Watching for Messages</span>')),
                    interval=args.interval, sweep_interval=args.sweep,
                    max_region=args.max_region, seen=seen, workers=args.workers, stop=stop),
                daemon=True,
            )

    injector = None
    if not sys.platform.startswith("linux") and use_quiche:
        try:
            from . import inject_client
            injector = inject_client.Injector(gap=args.type_delay / 1000.0)
            proc_holder.append(injector.proc)
        except Exception as e:
            inbox.put((SYS, f"injector unavailable: {e}"))

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
                elif tag is SYS_HTML:
                    ui.add_system_html(payload)
                else:
                    if payload.kind in ("whisper_in", "whisper_out"):
                        hs = crypto.parse_hs_token(payload.body)
                        if hs:
                            marker, their_pub = hs
                            if payload.kind == "whisper_in":
                                if marker == crypto.HS_ADD:
                                    crypto.record_incoming_request(payload.sender, their_pub)
                                    ui.add_system_html(f'<span style="color:#7ec8ff;">❗ Friend request from <b>{html.escape(payload.sender)}</b> — type <code>\\friend accept {html.escape(payload.sender)}</code></span>')
                                    ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())
                                elif marker == crypto.HS_ACCEPT:
                                    if crypto.has_outgoing_request(payload.sender):
                                        crypto.clear_outgoing_request(payload.sender)
                                        key = crypto.save_derived_friend_key(payload.sender, their_pub)
                                        ui.add_system_html(f'<span style="color:#7ec8ff;">❗ Friend request accepted! You are now securely connected to <b>{html.escape(payload.sender)}</b>. '
                                                      f'(Key fingerprint: {crypto.key_fingerprint(key)})</span>')
                                        ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())
                            continue  # don't display the raw token (whether inbound or outbound)

                        if payload.body.startswith(r"\party_invite "):
                            if payload.kind == "whisper_in":
                                parts = payload.body.split()
                                if len(parts) == 3:
                                    gname = parts[1]
                                    gb64 = parts[2]
                                    try:
                                        raw_key = base64.b64decode(gb64)
                                        if len(raw_key) == 32:
                                            crypto.set_group_psk(gname, gb64)
                                            ui.add_system_html(f'<span style="color:#7ec8ff;">🎉 <b>{html.escape(payload.sender)}</b> invited you to the party! You joined securely.</span>')
                                            ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())
                                    except Exception:
                                        pass
                            continue  # hide the invite payload from chat view

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
        # Intercept commands typed in chat
        if text.lower().startswith(r"\friend "):
            _do_friend(text)
            return
        if text.lower().startswith(r"\party "):
            _do_party(text)
            return

        # A GIF is a normal ENCRYPTED private message whose plaintext is "HXG1 <url>";
        # the receiver overlay sees the magic bytes and displays it as a GIF.
        gif_url = ""
        if text.split(None, 1)[0].lower() == r"\gif":
            gif_url = text[len(r"\gif"):].strip()
            if not gif_util.valid_url(gif_url):
                inbox.put((SYS, r"usage: \gif <direct .gif URL> (http/https)"))
                return
            if ui.recipient == "Public":
                inbox.put((SYS, "select a friend or party you share a key with — GIFs go over the "
                                "encrypted tunnel (so they shouldn't go in Public chat!)"))
                return
            gif_util.push_recent(gif_url)
            text = gif_url

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
                _is_gif = False
                _gif_url = ""
                if body.startswith(chatframe.GIF_SENTINEL):
                    _is_gif = True
                    _gif_url = body[len(chatframe.GIF_SENTINEL):].strip()
                elif body.startswith("HXG1"):
                    _is_gif = True
                    _gif_url = body[len("HXG1"):].strip()
                elif "http" in body and ".gif" in body.lower():
                    _is_gif = True
                    _gif_url = body.strip()
                ui.add_message(Msg(sender="you", body=body, kind="whisper_out",
                                   is_self=True, is_tunnel=True, target=friend,
                                   is_gif=_is_gif, gif_url=_gif_url))

            def _do_send() -> None:
                try:
                    if injector:
                        injector.send(mode, friend, body)
                    else:
                        send.send_message(friend, body, open_key=args.open_key,
                                          pre_send=_ledger, paste_method=args.paste_method,
                                          type_delay_ms=args.type_delay)
                except Exception as e:
                    inbox.put((SYS, f"send failed: {e}"))
        elif mode == "party_private":
            if not sys.platform.startswith("linux") and not use_quiche:
                _is_gif = False
                _gif_url = ""
                if body.startswith(chatframe.GIF_SENTINEL):
                    _is_gif = True
                    _gif_url = body[len(chatframe.GIF_SENTINEL):].strip()
                elif body.startswith("HXG1"):
                    _is_gif = True
                    _gif_url = body[len("HXG1"):].strip()
                elif "http" in body and ".gif" in body.lower():
                    _is_gif = True
                    _gif_url = body.strip()
                ui.add_message(Msg(sender="you", body=body, kind="party",
                                   is_self=True, is_tunnel=True, target="party",
                                   is_gif=_is_gif, gif_url=_gif_url))

            def _do_send() -> None:
                try:
                    if injector:
                        injector.send(mode, friend, body)
                    else:
                        send.send_party_message(body, open_key=args.open_key,
                                                pre_send=_ledger, paste_method=args.paste_method,
                                                type_delay_ms=args.type_delay)
                except Exception as e:
                    inbox.put((SYS, f"send failed: {e}"))
        else:  # public
            if not sys.platform.startswith("linux") and not use_quiche:
                _is_gif = False
                _gif_url = ""
                if body.startswith(chatframe.GIF_SENTINEL):
                    _is_gif = True
                    _gif_url = body[len(chatframe.GIF_SENTINEL):].strip()
                elif body.startswith("HXG1"):
                    _is_gif = True
                    _gif_url = body[len("HXG1"):].strip()
                elif "http" in body and ".gif" in body.lower():
                    _is_gif = True
                    _gif_url = body.strip()
                ui.add_message(Msg(sender="you", body=body, kind="public", is_self=True,
                                   is_gif=_is_gif, gif_url=_gif_url))

            def _do_send() -> None:
                try:
                    if injector:
                        injector.send(mode, friend, body)
                    else:
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
            
        if text.split(None, 1)[0].lower() == "/gif":
            gif_url = text[len("/gif"):].strip()
            if not gif_util.valid_url(gif_url):
                inbox.put((SYS, "usage: /gif <direct .gif URL> (http/https)"))
                return
            gif_util.push_recent(gif_url)
            text = gif_url
            
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

    def _do_friend(text: str) -> None:
        parts = text.split(None, 2)                  # ['\friend', 'add', 'Bob']
        sub = parts[1].lower() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        if sub not in ("add", "accept", "remove") or not name:
            inbox.put((SYS, r"usage: \friend add|accept|remove <player>"))
            return
            
        # Helper to inject public message without typing
        def _send_line(msg):
            if injector:
                injector.send("public", None, msg)
            else:
                send.send_public(msg, open_key=args.open_key, paste_method=args.paste_method, type_delay_ms=args.type_delay)
                
        if sub == "add":
            crypto.record_outgoing_request(name)
            _send_line(f"/msg {name} {crypto.hs_add_token()}")
            inbox.put((SYS, f"friend request sent to {name} — have them run: "
                            rf"\friend accept {my_name or 'you'}"))
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

    def _do_gif_action(action: str, url: str) -> None:
        if action == "add":
            gif_util.add_favorite(url)
        elif action == "unfav":
            gif_util.remove_favorite(url)
        elif action == "forget":
            gif_util.forget(url)

    def _do_party(text: str) -> None:
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "invite":
            friend = parts[2] if len(parts) > 2 else ""
            if not friend:
                inbox.put((SYS, r"usage: \party invite <friend> [party_name]"))
                return
            group_name = parts[3] if len(parts) > 3 else "party"
            
            group_key = crypto.load_group_psk(group_name)
            if not group_key:
                inbox.put((SYS, f"No group key for '{group_name}'. Set it up first using the CLI!"))
                return
            
            key_b64 = base64.b64encode(group_key).decode("utf-8")
            payload = f"\\party_invite {group_name} {key_b64}"
            
            try:
                enc = crypto.encrypt_sym(friend, payload)
            except KeyError:
                inbox.put((SYS, f"You must be friends with {friend} first! Run \\friend add {friend}"))
                return
                
            def _send_line(msg):
                if injector:
                    injector.send("public", None, msg)
                else:
                    send.send_public(msg, open_key=args.open_key, paste_method=args.paste_method, type_delay_ms=args.type_delay)
                    
            _send_line(f"/msg {friend} {enc}")
            inbox.put((SYS, f"Party invite for '{group_name}' sent to {friend}!"))
        else:
            inbox.put((SYS, r"usage: \party invite <friend> [party_name]"))

    ui.custom_encrypt_requested.connect(on_custom_encrypt)
    ui.custom_decrypt_requested.connect(on_custom_decrypt)
    ui.submitted.connect(on_submit)
    ui.dismissed.connect(send.focus_game)
    def _handle_friend_action(action: str, nm: str):
        if action == "invite":
            _do_party(rf"\party invite {nm}")
        elif action == "leave_party":
            from . import crypto
            group_key = crypto.load_group_psk("party")
            if group_key:
                try:
                    (crypto.GROUPS_DIR / "party.key").unlink(missing_ok=True)
                except Exception:
                    pass
                inbox.put((SYS, "You left the party."))
                ui.refresh_friends(crypto.list_psk_friends(), crypto.list_incoming_requests())
                # Also clean up legacy friends/party.key if it exists
                try:
                    (crypto.FRIENDS_DIR / "party.pub").unlink(missing_ok=True)
                    (crypto.FRIENDS_DIR / "party.key").unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            _do_friend(rf"\friend {action} {nm}")
            
    ui.friend_action.connect(_handle_friend_action)
    ui.gif_action.connect(_do_gif_action)
    ui.gif_send.connect(lambda url: on_submit(r"\gif " + url))

    if memscan.find_client_pid() is None:
        ui.add_system("HytaleClient not running — waiting…")
    
    def _fmt_hk(hk: str) -> str:
        return hk.title().replace("Shift+", "Sh+")

    instructions = (
        '<span style="color:#00d8ff; font-weight:bold;">TUNNEL UP</span><br>'
        f'<span style="color:#8fd;">Friends: {", ".join(friends) if friends else "None"}</span><br>'
        '<span style="color:#ffffff; font-size:12px;">'
        f'{_fmt_hk(args.hotkey_open)} - Open Chat<br>'
        f'{_fmt_hk(args.hotkey_close)} - Minimize Tunnel<br>'
        f'{_fmt_hk(args.hotkey_unfocus)} - Game Focus<br>'
        '\\gif "Link" - Send GIF'
        '</span>'
    )
    ui.add_system_html(instructions)

    pidfile = crypto.CONFIG_DIR / "tunnel.pid"
    try:
        crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pidfile = None

    def reposition() -> None:
        geom = settings.value("geometry")
        if geom:
            ui.restoreGeometry(geom)
            if not getattr(ui, "_collapsed", False) and hasattr(ui, "_expanded_size"):
                ui.resize(ui._expanded_size)
        else:
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
            import ctypes
            console_hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if console_hwnd:
                ctypes.windll.user32.ShowWindow(console_hwnd, 6)  # SW_MINIMIZE
                
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
        if not getattr(ui, "_collapsed", False):
            settings.setValue("geometry", ui.saveGeometry())
        settings.setValue("font_size", ui._font_px)
        
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
