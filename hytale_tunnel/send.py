"""Platform dispatcher for the send/inject path (hyprctl+wtype on Linux,
SendInput on Windows). app.py imports send_message/find_game_window/focus_game
from here."""

import sys

if sys.platform.startswith("win"):
    from .send_win import find_game_window, focus_game, send_message
elif sys.platform.startswith("linux"):
    from .send_linux import find_game_window, focus_game, send_message
else:                                                       # pragma: no cover
    def _unsupported(*a, **k):
        raise RuntimeError(f"sending unsupported on platform: {sys.platform}")

    find_game_window = focus_game = send_message = _unsupported


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: python -m hytale_tunnel.send <friend> <message...>")
    w = find_game_window()
    print("game window:", w)
    print("sent:", send_message(sys.argv[1], " ".join(sys.argv[2:]))[:32], "...")
