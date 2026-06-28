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

from . import crypto, memscan, send
from .overlay import Overlay


def _position_top_right(ui, app) -> None:
    """Place the overlay at the top-right. Wayland ignores client-set positions, so
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
            logical_w = int(mon["width"] / scale)
            x = int(mon["x"]) + logical_w - ui.width() - margin_x
            y = int(mon["y"]) + margin_y
            subprocess.run(["hyprctl", "dispatch", "movewindowpixel",
                            f"exact {x} {y},class:^(hytale-tunnel)$"], capture_output=True)
        except Exception:
            pass
    else:
        scr = app.primaryScreen().availableGeometry()
        ui.move(scr.right() - ui.width() - margin_x, scr.top() + margin_y)


def main() -> int:
    ap = argparse.ArgumentParser(prog="hytale-tunnel",
                                 description="Encrypted in-game chat tunnel overlay.")
    ap.add_argument("-r", "--recipient", help="default friend to send to")
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
    ap.add_argument("--hotkey-size", default="win+shift+o",
                    help="Windows global hotkey to grow/shrink the overlay "
                         "(default: win+shift+j)")
    ap.add_argument("--hotkey-focus", default="win+shift+j",
                    help="Windows global hotkey to toggle focus between the overlay and "
                         "the game (default: win+shift+o; note win+shift+p is taken by "
                         "Windows)")
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
    ui = Overlay(recipient, friends, font_size=args.font_size)

    # Receive thread -> thread-safe queue -> drained on the Qt main thread.
    # System notices use the SYS sentinel as the "sender" slot.
    SYS = object()
    inbox: queue.Queue[tuple[object, str]] = queue.Queue()
    stop = threading.Event()
    proc_holder: list = []               # holds the elevated capture process (Linux)
    seen = None
    sent_tokens: set = set()             # our own outgoing tokens, to skip server echo
    if sys.platform.startswith("linux"):
        # Flawless path: capture incoming chat at quiche's decrypt boundary (eBPF).
        from . import receiver_quiche
        scanner = threading.Thread(
            target=receiver_quiche.watch,
            kwargs=dict(
                on_message=lambda sender, text: inbox.put((sender, text)),
                on_ready=lambda: inbox.put((SYS, "ready — capturing chat (quiche)")),
                stop=stop, proc_holder=proc_holder, seen=sent_tokens),
            daemon=True,
        )
    else:
        # Windows: flawless path is the quiche QUIC-stream hook (Frida), the native
        # equivalent of the Linux eBPF capture. Only fall back to memory scanning if
        # frida/quiche aren't available.
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
                    on_message=lambda sender, text: inbox.put((sender, text)),
                    on_ready=lambda: inbox.put((SYS, "ready — capturing chat (quiche)")),
                    stop=stop, proc_holder=proc_holder, seen=sent_tokens),
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
                    on_message=lambda sender, text: inbox.put((sender, text)),
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
                sender, text = inbox.get_nowait()
                if sender is SYS:
                    ui.add_system(text)
                else:
                    ui.add_received(text, sender=sender)
        except queue.Empty:
            pass

    timer = QtCore.QTimer()
    timer.timeout.connect(drain)
    timer.start(200)

    def on_submit(text: str) -> None:
        friend = ui.recipient
        # Only the memory-scan path can mistake our own send for an incoming message
        # (the quiche hook only sees true incoming, via a different function). So we
        # pre-ledger our token there; on Linux it's unnecessary.
        def _ledger(blob: str) -> None:
            # Linux/quiche: the receiver dedups by full "HX1…" token; add ours so the
            # server's echo of our own whisper isn't shown as the friend's reply.
            sent_tokens.add(blob)
            if seen is not None:           # Windows/memscan dedups by hash
                body = blob[len(crypto.SYM_MARKER):] if blob.startswith(crypto.SYM_MARKER) else blob
                seen.add(memscan.token_hash(body))

        ui.add_sent(text)
        # Sending sleeps + shells out (focus, paste, keystrokes); do it OFF the Qt
        # main thread so the overlay can never freeze if a subprocess stalls.
        def _do_send() -> None:
            try:
                send.send_message(friend, text, open_key=args.open_key,
                                  pre_send=_ledger, paste_method=args.paste_method,
                                  type_delay_ms=args.type_delay)
            except Exception as e:                   # noqa: BLE001 - surface to overlay
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
        if getattr(ui, "_user_moved", False):      # don't fight a manual drag
            return
        for delay in (120, 400, 750):
            QtCore.QTimer.singleShot(delay, lambda: _position_top_right(ui, app))
    if sys.platform.startswith("linux"):
        # Hyprland re-centers floating windows on resize, so re-pin on collapse. On
        # Windows the overlay is freely draggable, so we position once at startup only.
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
                size_spec=args.hotkey_size, focus_spec=args.hotkey_focus,
                notify=lambda m: inbox.put((SYS, m)))
        except Exception as e:                       # noqa: BLE001
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
