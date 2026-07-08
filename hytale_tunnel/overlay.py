"""PyQt6 always-on-top overlay: decrypted transcript + compose box.

The widget is intentionally dumb: it renders messages and emits a signal when you
submit a line. All wiring (memory scanner, sending) lives in app.py.

ESC collapses the overlay to a small always-on-top pill (it is never unmapped, so
it stays reachable with no global hotkey); clicking the pill expands it again.
This works identically on Linux/Hyprland and Windows.
"""

import colorsys
import html
import os

from PyQt6 import QtCore, QtGui, QtWidgets

FONT_SIZE_PX = int(os.environ.get("HYTALE_TUNNEL_FONT_SIZE", "14"))

# Rank/name/body colours come straight off the wire -- tuned for the game's own UI,
# not our translucent near-black overlay (rgba ~10,12,16). A dark red (or any dark
# rank colour) that reads fine in-game can be almost illegible here, so bump any
# game-supplied colour up to a minimum lightness before rendering it.
_MIN_LIGHTNESS = 0.55


def _brighten(hex_color: str) -> str:
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return hex_color
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except ValueError:
        return hex_color
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if l >= _MIN_LIGHTNESS:
        return hex_color
    r, g, b = colorsys.hls_to_rgb(h, _MIN_LIGHTNESS, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


class Overlay(QtWidgets.QWidget):
    submitted = QtCore.pyqtSignal(str)          # emitted with composed plaintext
    collapsed_changed = QtCore.pyqtSignal()     # emitted after collapse/expand (resize)

    def __init__(self, recipient: str, friends: list[str]):
        super().__init__()
        self.recipient = recipient
        self._collapsed = False
        self._unread = 0
        self._expanded_size = QtCore.QSize(440, 320)
        self.setWindowTitle("hytale-tunnel")    # Hyprland matches this for window rules
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(self._expanded_size)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- header (stays visible in both states; doubles as the collapse pill) ---
        self.header = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.title = QtWidgets.QLabel("🔒 tunnel")
        self.title.setStyleSheet("color:#8fd; font-weight:bold;")
        header.addWidget(self.title)
        header.addStretch(1)
        self.arrow = QtWidgets.QLabel("→")
        header.addWidget(self.arrow)
        self.recipient_box = QtWidgets.QComboBox()
        self.recipient_box.addItems(friends or [recipient])
        if recipient in friends:
            self.recipient_box.setCurrentText(recipient)
        self.recipient_box.currentTextChanged.connect(self._set_recipient)
        self.recipient_box.setStyleSheet("color:#ddd; background:#222;")
        header.addWidget(self.recipient_box)
        root.addWidget(self.header)

        # --- body (hidden when collapsed) ---
        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        self.view = QtWidgets.QTextEdit(readOnly=True)
        self.view.setStyleSheet(
            "QTextEdit{background:rgba(10,12,16,200); color:#e6e6e6;"
            "border:1px solid #2a2f3a; border-radius:6px; padding:4px;"
            f"font-family:monospace; font-size:{FONT_SIZE_PX}px;}}"
        )
        body.addWidget(self.view, 1)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText(
            "message (public) · /msg name msg or /r msg (private) · Esc to collapse")
        self.input.setStyleSheet(
            "QLineEdit{background:rgba(20,24,30,230); color:#fff;"
            "border:1px solid #3a4150; border-radius:6px; padding:5px;"
            f"font-size:{FONT_SIZE_PX}px;}}"
        )
        self.input.returnPressed.connect(self._on_submit)
        body.addWidget(self.input)
        root.addWidget(self.body, 1)

        QtGui.QShortcut(QtGui.QKeySequence("Esc"), self, activated=self._on_escape)

    # ---- public API (call from the Qt main thread only) ----

    # Colours per author class.
    _C_YOU = "#9bf6a0"        # our own messages (green)
    _C_OTHER = "#c8d0dc"      # another player (soft white)
    _C_TUNNEL = "#7ec8ff"     # encrypted/decrypted tunnel (cyan)
    _C_WHISPER = "#ff8fd0"    # whispers (magenta)
    _C_EMOTE = "#b9a9ff"      # /me emotes (purple)
    _C_DIM = "#7a828f"        # connectives ("whispers:", "to")
    _C_SYS = "#888"           # server/console lines

    def add_message(self, msg) -> None:
        """Render a chatframe.Msg (the unified display message)."""
        name = html.escape(msg.sender)
        body = html.escape(msg.body).replace("\n", "<br>")
        lock = "🔒 " if msg.is_tunnel else ""
        # Mirror the game's own colours where we have them (name / body are each
        # independently coloured runs on the wire); fall back to our fixed palette
        # for tunnel-decrypted or otherwise colourless lines. The rank tag itself is
        # never shown -- we just borrow its colour for the name, so ranked players
        # show up name-coloured without taking up extra width.
        name_color = self._C_YOU if msg.is_self else (
            _brighten(msg.rank_color or msg.name_color)
            or (self._C_TUNNEL if msg.is_tunnel else self._C_OTHER))
        if not msg.is_self and msg.body_color and not msg.is_tunnel:
            body = f'<span style="color:{_brighten(msg.body_color)}">{body}</span>'

        if msg.kind == "emote":
            who = "you" if msg.is_self else name
            line = (f'<span style="color:{self._C_EMOTE}"><i>{lock}* '
                    f'{html.escape(who)} {body}</i></span>')
        elif msg.kind == "whisper_in":
            line = (f'<span style="color:{self._C_WHISPER}">{lock}<b>{name}</b></span>'
                    f'<span style="color:{self._C_DIM}"> whispers:</span> {body}')
        elif msg.kind == "whisper_out":
            tgt = html.escape(msg.target or self.recipient)
            line = (f'<span style="color:{self._C_DIM}">{lock}to </span>'
                    f'<span style="color:{self._C_WHISPER}"><b>{tgt}</b></span>'
                    f'<span style="color:{self._C_DIM}">:</span> {body}')
        elif msg.kind == "system":
            line = f'<span style="color:{self._C_SYS}">{body}</span>'
        else:  # public
            line = (f'<span style="color:{name_color}">{lock}<b>{name}:</b></span> {body}')

        self._append_html(line)
        if not msg.is_self:
            self._note_activity()

    def add_system(self, text: str) -> None:
        self._append_html(f'<span style="color:{self._C_SYS}">· {html.escape(text)}</span>')

    # ---- collapse / expand ----

    _PILL_STYLE = ("color:#8fd; font-weight:bold; background:rgba(20,24,30,235);"
                   "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;")
    _TITLE_STYLE = "color:#8fd; font-weight:bold;"

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.body.setVisible(not collapsed)
        self.arrow.setVisible(not collapsed)
        self.recipient_box.setVisible(not collapsed)
        if collapsed:
            self.title.setText("🔒 ▸")
            self.title.setStyleSheet(self._PILL_STYLE)
            # Force the size: equal min==max makes Hyprland/Windows honor the shrink
            # (a soft resize/adjustSize is otherwise ignored, leaving a frosted box).
            self.setFixedSize(132, 34)
        else:
            self._unread = 0
            self.title.setText("🔒 tunnel")
            self.title.setStyleSheet(self._TITLE_STYLE)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(self._expanded_size)
            self.input.setFocus()
            self.raise_()
            self.activateWindow()
        self.collapsed_changed.emit()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def _on_escape(self) -> None:
        if not self._collapsed:
            self.set_collapsed(True)

    def mousePressEvent(self, event) -> None:
        # Clicking the collapsed pill (or its title) restores the overlay.
        if self._collapsed:
            self.set_collapsed(False)
        super().mousePressEvent(event)

    # ---- internals ----

    def _note_activity(self) -> None:
        if self._collapsed:
            self._unread += 1
            self.title.setText(f"🔒 ● {self._unread}")

    def _set_recipient(self, name: str) -> None:
        self.recipient = name

    def _append_html(self, line: str) -> None:
        self.view.append(line)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)
