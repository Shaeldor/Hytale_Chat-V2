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

LINUX = sys.platform.startswith("linux")


def _parse_command(text: str, last_contact: dict):
    """Classify compose-box input.

    '/msg <name> <message>' or '/r <message>' (reply to whoever we last privately
    exchanged messages with) -> encrypted private send to a specific friend.
    Anything else -> plain public chat, typed in-game exactly as-is, unencrypted.
    Returns (mode, friend, body) with mode in {'private', 'public', 'error'}
    ('error': body is a user-facing message, friend is None).
    """
    if text.startswith("/msg "):
        parts = text[len("/msg "):].split(None, 1)
        if len(parts) < 2:
            return "error", None, "usage: /msg <name> <message>"
        name = parts[0]
        if name not in crypto.list_psk_friends():
            known = ", ".join(crypto.list_psk_friends()) or "(none)"
            return "error", None, f"unknown friend '{name}'. Known: {known}"
        return "private", name, parts[1]
    if text == "/r" or text.startswith("/r "):
        body = text[len("/r"):].strip()
        if not body:
            return "error", None, "usage: /r <message>"
        if not last_contact["name"]:
            return "error", None, "no one to reply to yet"
        return "private", last_contact["name"], body
    return "public", None, text


def _position_top_right(ui, app) -> None:
    """Place the overlay at a fixed spot on the focused monitor. Wayland ignores
    client-set positions, so on Linux we ask Hyprland to move it; on Windows Qt's
    move() works natively."""
    pos_x, pos_y = 60, 700
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
    ui = Overlay(recipient, friends)

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
    toggle = {"pending": False, "quit": False}
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: toggle.__setitem__("pending", True))
    # Graceful shutdown so the `finally` block runs and removes the PID file
    # (SIGTERM/SIGINT would otherwise kill us without cleanup, leaving a stale pid).
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
                b = blob[len(crypto.SYM_MARKER):] if blob.startswith(crypto.SYM_MARKER) else blob
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
                    send.send_message(friend, body, open_key=args.open_key,
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
                    send.send_public(body, open_key=args.open_key,
                                     paste_method=args.paste_method,
                                     type_delay_ms=args.type_delay)
                except Exception as e:               # noqa: BLE001 - surface to overlay
                    inbox.put((SYS, f"send failed: {e}"))
        threading.Thread(target=_do_send, daemon=True).start()

    ui.submitted.connect(on_submit)

    if memscan.find_client_pid() is None:
        ui.add_system("HytaleClient not running — waiting…")
    ui.add_system(f"tunnel up · recipient: {recipient} · friends: {', '.join(friends)}")

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
            QtCore.QTimer.singleShot(delay, lambda: _position_top_right(ui, app))
    ui.collapsed_changed.connect(reposition)

    scanner.start()
    ui.show()
    reposition()
    try:
        return app.exec()
    finally:
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
