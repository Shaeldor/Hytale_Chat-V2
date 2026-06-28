"""PyQt6 always-on-top overlay: decrypted transcript + compose box.

The widget is intentionally dumb: it renders messages and emits a signal when you
submit a line. All wiring (memory scanner, sending) lives in app.py.

ESC collapses the overlay to a small always-on-top pill (it is never unmapped, so
it stays reachable with no global hotkey); clicking the pill expands it again.
This works identically on Linux/Hyprland and Windows.
"""

import html

from PyQt6 import QtCore, QtGui, QtWidgets


class Overlay(QtWidgets.QWidget):
    submitted = QtCore.pyqtSignal(str)          # emitted with composed plaintext
    collapsed_changed = QtCore.pyqtSignal()     # emitted after collapse/expand (resize)

    def __init__(self, recipient: str, friends: list[str], font_size: int = 14):
        super().__init__()
        self.recipient = recipient
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
        body.addWidget(self.view, 1)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("message…  (Enter to send, Esc to collapse)")
        self.input.returnPressed.connect(self._on_submit)
        body.addWidget(self.input)
        root.addWidget(self.body, 1)

        self._apply_styles()

        QtGui.QShortcut(QtGui.QKeySequence("Esc"), self, activated=self._on_escape)
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

    # ---- public API (call from the Qt main thread only) ----

    def add_received(self, text: str, sender: str | None = None) -> None:
        self._append(sender or self.recipient, text, "#7ec8ff")
        self._note_activity()

    def add_sent(self, text: str) -> None:
        self._append("you", text, "#9bf6a0")

    def add_system(self, text: str) -> None:
        self._append("·", text, "#888")

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

    def _append(self, who: str, text: str, color: str) -> None:
        esc = html.escape(text).replace("\n", "<br>")
        self.view.append(f'<span style="color:{color}"><b>{html.escape(who)}:</b> </span>{esc}')
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)
