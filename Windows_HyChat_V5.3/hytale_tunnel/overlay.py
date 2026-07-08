"""PyQt6 always-on-top overlay: decrypted transcript + compose box.

The widget is intentionally dumb: it renders messages and emits a signal when you
submit a line. All wiring (memory scanner, sending) lives in app.py.

ESC collapses the overlay to a small always-on-top pill (it is never unmapped, so
it stays reachable with no global hotkey); clicking the pill expands it again.
This works identically on Linux/Hyprland and Windows.
"""

import colorsys
import html

from PyQt6 import QtCore, QtGui, QtWidgets

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
    custom_encrypt_requested = QtCore.pyqtSignal(str, str) # emitted when user requests clipboard encryption (channel, text)
    custom_decrypt_requested = QtCore.pyqtSignal(str)      # emitted when user wants to decrypt a pasted token
    collapsed_changed = QtCore.pyqtSignal()     # emitted after collapse/expand (resize)
    escape_to_game = QtCore.pyqtSignal()        # ESC collapsed us -> hand focus back to game

    def __init__(self, recipient: str, friends: list[str], font_size: int = 14):
        super().__init__()
        self.recipient = recipient
        self._friends_list = [f for f in friends if f.lower() != "party"]
        self._collapsed = False
        self._unread = 0
        self._font_size = font_size
        self._drag_off = None
        self._user_moved = False
        self._expanded_size = QtCore.QSize(440, 320)
        self.setWindowTitle("hytale-tunnel")    # Hyprland matches this for window rules
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(self._create_emoji_icon("🔐"))
        self.resize(self._expanded_size)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # --- header (stays visible in both states; doubles as the collapse pill) ---
        self.header = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.title = QtWidgets.QLabel("🔒 tunnel  ⠿")   # ⠿ hints "drag here to move"
        self.title.setStyleSheet("color:#8fd; font-weight:bold;")
        header.addWidget(self.title)
        header.addStretch(1)
        self.arrow = QtWidgets.QLabel("→")
        header.addWidget(self.arrow)
        self.recipient_box = QtWidgets.QComboBox()
        
        # Build the channel list: Public, Party, then all friends (excluding 'party' if it's there, to avoid duplicates)
        items = ["Public", "Party"]
        items += [f for f in friends if f.lower() != "party"]
        
        self.recipient_box.addItems(items)
        
        last_chan = None
        try:
            from . import crypto
            last_chan = (crypto.CONFIG_DIR / "last_channel.txt").read_text().strip()
        except OSError:
            pass

        if last_chan in items:
            self.recipient_box.setCurrentText(last_chan)
            self.recipient = last_chan
        elif recipient in items:
            self.recipient_box.setCurrentText(recipient)
        else:
            self.recipient_box.setCurrentText("Public")
            self.recipient = "Public"
            
        self.recipient_box.currentTextChanged.connect(self._set_recipient)
        self.recipient_box.setStyleSheet("color:#ddd; background:#222;")
        header.addWidget(self.recipient_box)
        
        self.btn_encrypt = QtWidgets.QPushButton("🔐")
        self.btn_encrypt.setToolTip("Encrypt a custom message for this channel to clipboard")
        self.btn_encrypt.setStyleSheet(
            "color:#8fd; font-weight:bold; background:rgba(20,24,30,235);"
            "border:1px solid #3a4150; border-radius:6px; padding:2px 6px;"
        )
        self.btn_encrypt.clicked.connect(self._on_encrypt_clicked)
        header.addWidget(self.btn_encrypt)
        
        self.btn_decrypt = QtWidgets.QPushButton("👁️")
        self.btn_decrypt.setToolTip("Manually decrypt a token from your clipboard")
        self.btn_decrypt.setStyleSheet(
            "color:#ffb84d; font-weight:bold; background:rgba(20,24,30,235);"
            "border:1px solid #3a4150; border-radius:6px; padding:2px 6px;"
        )
        self.btn_decrypt.clicked.connect(self._on_decrypt_clicked)
        header.addWidget(self.btn_decrypt)
        
        root.addWidget(self.header)

        # --- body (hidden when collapsed) ---
        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        self.view = QtWidgets.QTextEdit(readOnly=True)
        body.addWidget(self.view, 1)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText(
            "Type message... (Sh+Up chat | Sh+Left exit | Sh+Down shrink)")
        self.input.returnPressed.connect(self._on_submit)
        body.addWidget(self.input)
        root.addWidget(self.body, 1)

        self._apply_styles()

        QtGui.QShortcut(QtGui.QKeySequence("Ctrl++"), self,
                        activated=lambda: self.set_font_size(self._font_size + 1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+="), self,
                        activated=lambda: self.set_font_size(self._font_size + 1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+-"), self,
                        activated=lambda: self.set_font_size(self._font_size - 1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+0"), self,
                        activated=lambda: self.set_font_size(14))

    # ---- styling / font ----

    def _apply_styles(self) -> None:
        fs = self._font_size
        self.view.setStyleSheet(
            "QTextEdit{background:rgba(10,12,16,200); color:#e6e6e6;"
            "border:1px solid #2a2f3a; border-radius:6px; padding:4px;"
            f"font-family:monospace; font-size:{fs}px;}}")
        self.input.setStyleSheet(
            "QLineEdit{background:rgba(20,24,30,230); color:#fff;"
            f"border:1px solid #3a4150; border-radius:6px; padding:5px; font-size:{fs}px;}}")

    def set_font_size(self, px: int) -> None:
        self._font_size = max(8, min(40, int(px)))
        self._apply_styles()

    def _create_emoji_icon(self, emoji: str) -> QtGui.QIcon:
        pixmap = QtGui.QPixmap(64, 64)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        font = painter.font()
        font.setPixelSize(48)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, emoji)
        painter.end()
        return QtGui.QIcon(pixmap)

    # ---- public API (call from the Qt main thread only) ----

    # Colours per author class.
    _C_YOU = "#9bf6a0"        # our own messages (green)
    _C_OTHER = "#c8d0dc"      # another player (soft white)
    _C_TUNNEL = "#7ec8ff"     # encrypted/decrypted tunnel (cyan)
    _C_WHISPER = "#ff8fd0"    # whispers (magenta)
    _C_PARTY = "#ffb84d"      # party chat (orange)
    _C_EMOTE = "#b9a9ff"      # /me emotes (purple)
    _C_DIM = "#7a828f"        # connectives ("whispers:", "to")
    _C_SYS = "#888"           # server/console lines

    def add_message(self, msg) -> None:
        """Render a chatframe.Msg (the unified display message)."""
        name = html.escape(msg.sender)
        body = html.escape(msg.body).replace("\n", "<br>")
        lock = "🔒 " if msg.is_tunnel else ""
        # Override name colours to make friends stand out:
        # Green for you, Cyan for friends, White for strangers.
        if msg.is_self:
            name_color = self._C_YOU
        elif msg.sender in self._friends_list:
            name_color = self._C_TUNNEL
        else:
            name_color = self._C_OTHER
            
        if not msg.is_self and msg.body_color and not msg.is_tunnel:
            body = f'<span style="color:{_brighten(msg.body_color)}">{body}</span>'

        if msg.kind == "emote":
            who = "you" if msg.is_self else name
            line = (f'<span style="color:{self._C_EMOTE}"><i>{lock}* '
                    f'{html.escape(who)} {body}</i></span>')
        elif msg.kind == "party":
            line = (f'<span style="color:{self._C_PARTY}">{lock}<b>[Party] </b></span>'
                    f'<span style="color:{name_color}"><b>{name}:</b></span> {body}')
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
        pos = self.pos()                     # keep the top-left anchored across resize
        self._collapsed = collapsed
        self.body.setVisible(not collapsed)
        self.arrow.setVisible(not collapsed)
        self.recipient_box.setVisible(not collapsed)
        self.btn_encrypt.setVisible(not collapsed)
        self.btn_decrypt.setVisible(not collapsed)
        if collapsed:
            self.title.setText("🔒 ▸")
            self.title.setStyleSheet(self._PILL_STYLE)
            # Force the size: equal min==max makes Hyprland/Windows honor the shrink
            # (a soft resize/adjustSize is otherwise ignored, leaving a frosted box).
            self.setFixedSize(132, 34)
        else:
            self._unread = 0
            self.title.setText("🔒 tunnel  ⠿")
            self.title.setStyleSheet(self._TITLE_STYLE)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(self._expanded_size)
            self.input.setFocus()
            self.raise_()
            self.activateWindow()
        self.move(pos)
        self.collapsed_changed.emit()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    # ---- focus handling ----

    def focus_input(self) -> None:
        """Put the cursor in the compose box so you can type immediately."""
        if not self._collapsed:
            self.input.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def changeEvent(self, event) -> None:
        # When this window becomes active (global hotkey, Alt-Tab, or a click on it),
        # drop the cursor into the compose box -- otherwise the recipient combo grabs
        # focus on activation and you'd have to click the message bar first.
        if (event.type() == QtCore.QEvent.Type.ActivationChange
                and self.isActiveWindow() and not self._collapsed):
            self.input.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
        super().changeEvent(event)

    def _on_escape(self) -> None:
        if not self._collapsed:
            self.set_collapsed(True)
            self.escape_to_game.emit()         # return focus to the game (wired on Windows)

    # ---- dragging (frameless: grab the header / empty area, not the text widgets) ----

    def mousePressEvent(self, event) -> None:
        if self._collapsed:
            self.set_collapsed(False)          # click the pill to restore
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_off = (event.globalPosition().toPoint()
                              - self.frameGeometry().topLeft())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if (self._drag_off is not None
                and event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_off)
            self._user_moved = True
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_off = None
        super().mouseReleaseEvent(event)

    # ---- internals ----

    def _note_activity(self) -> None:
        if self._collapsed:
            self._unread += 1
            self.title.setText(f"🔒 ● {self._unread}")

    def _set_recipient(self, name: str) -> None:
        self.recipient = name
        try:
            from . import crypto
            crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (crypto.CONFIG_DIR / "last_channel.txt").write_text(name)
        except OSError:
            pass

    def _append_html(self, line: str) -> None:
        self.view.append(line)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)

    def _on_encrypt_clicked(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Custom Encrypted Message", 
            f"Enter message to encrypt for {self.recipient}:"
        )
        if ok and text:
            self.custom_encrypt_requested.emit(self.recipient, text)
            
    def _on_decrypt_clicked(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Manual Decryption", 
            "Paste an encrypted token (HX1...) here to decrypt it:"
        )
        if ok and text:
            self.custom_decrypt_requested.emit(text)
