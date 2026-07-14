"""PyQt6 always-on-top overlay: decrypted transcript + compose box.

The widget is intentionally dumb: it renders messages and emits a signal when you
submit a line. All wiring (memory scanner, sending) lives in app.py.

ESC collapses the overlay to a small always-on-top pill (it is never unmapped, so
it stays reachable with no global hotkey); clicking the pill expands it again.
This works identically on Linux/Hyprland and Windows.
"""

import colorsys
import hashlib
import html
import os
import threading

from PyQt6 import QtCore, QtGui, QtWidgets

from . import emoji_util, gif_util

# GIF display sizing (px, longest side): larger in the opened transcript, smaller in the
# floating HUD so it's unobtrusive over the game.
GIF_MAX_OPENED = 260
GIF_MAX_HUD = 150

FONT_SIZE_PX = int(os.environ.get("HYTALE_TUNNEL_FONT_SIZE", "14"))

# Font family for all chat text. Default is the generic monospace; override with
# HYTALE_TUNNEL_FONT, the --font flag, or the in-overlay `/font <name>` command. Any
# installed family works (e.g. "Fira Code", "JetBrains Mono", "sans-serif").
FONT_FAMILY = os.environ.get("HYTALE_TUNNEL_FONT", "monospace")


def _norm_family(name: str | None) -> str:
    """Normalize a font-family choice; 'default'/'reset' map back to the configured default."""
    name = (name or "").strip() or FONT_FAMILY
    return FONT_FAMILY if name.lower() in ("default", "reset") else name


def _css_family(name: str) -> str:
    """A font-family value safe for a Qt stylesheet: quote real family names (handles spaces
    and odd characters), but leave the generic keywords (monospace/serif/sans-serif) unquoted
    so Qt resolves them as generics rather than hunting for a family literally named that."""
    name = (name or "monospace").strip()
    if name.lower() in ("monospace", "serif", "sans-serif", "cursive", "fantasy"):
        return name.lower()
    return f'"{name}"'

# Background opacity behind the text (0 = fully transparent "just text", 255 = solid). A
# dark tone makes messages readable over any game background; text itself stays fully
# opaque. The compose box uses a bit more so it's easy to find. Tune (0-255) with
# HYTALE_TUNNEL_BG_ALPHA -- lower it for a more see-through overlay.
BG_ALPHA = max(0, min(255, int(os.environ.get("HYTALE_TUNNEL_BG_ALPHA", "110"))))

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
    activation_changed = QtCore.pyqtSignal(bool)  # window gained (True) / lost (False) focus
    dismissed = QtCore.pyqtSignal()             # Enter pressed with empty input -> unfocus
    friend_action = QtCore.pyqtSignal(str, str)  # (action, name): add / accept / remove
    gif_send = QtCore.pyqtSignal(str)           # a GIF url picked in the picker -> send it
    gif_action = QtCore.pyqtSignal(str, str)    # (action, url): add / remove favorite
    _gif_ready = QtCore.pyqtSignal(str)         # (url) a GIF finished downloading -> re-render

    def changeEvent(self, event) -> None:
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            self.activation_changed.emit(self.isActiveWindow())
        super().changeEvent(event)

    def __init__(self, recipient: str, friends: list[str],
                 font_px: int | None = None, size=None, font_family: str | None = None):
        super().__init__()
        self.recipient = recipient
        self._collapsed = False
        self._unread = 0
        self._entries = []          # ordered [(kind, payload)] so filters can re-render
        self._filter_idx = 0
        self._font_px = font_px or FONT_SIZE_PX
        self._font_family = _norm_family(font_family)
        self._gif_pumps = {}         # gif_id -> QMovie animating an <img> in the opened view
        self._gif_placed = set()     # gif_ids whose <img> is currently in the opened view
        self._gif_pending = set()    # GIF urls currently downloading
        self._hud_gif_by_url = {}    # url -> _FadingGif HUD line awaiting its download
        self._gif_picker = None
        self._gif_ready.connect(self._on_gif_ready)
        self._expanded_size = QtCore.QSize(*size) if size else QtCore.QSize(440, 320)
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
        # Don't let the layout's content minimum become the WINDOW's minimum size. Otherwise a
        # tall passive HUD (many/long messages) reports a big min-height, Hyprland refuses to
        # shrink the window back down on close, and it stays tall + creeps upward each cycle.
        # With no constraint the window can be any size and content just clips/scrolls.
        root.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetNoConstraint)

        # --- header (stays visible in both states; doubles as the collapse pill) ---
        self.header = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.title = QtWidgets.QLabel("")       # no name in expanded view (pill/unread reuse it)
        self.title.setStyleSheet("color:#8fd; font-weight:bold;")
        header.addWidget(self.title)
        # Filter button: click to cycle which messages the transcript shows.
        self.filter_btn = QtWidgets.QPushButton()
        self.filter_btn.setToolTip("filter shown messages (click to cycle)")
        self.filter_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.filter_btn.setStyleSheet(
            "QPushButton{color:#ddd; background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 8px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.filter_btn.clicked.connect(self._cycle_filter)
        self._update_filter_btn()
        header.addWidget(self.filter_btn)
        # Emoji picker: click to browse/insert a :shortcode: into the compose box.
        self.emoji_btn = QtWidgets.QPushButton("😀")
        self.emoji_btn.setToolTip("insert emoji (or type :shortcodes: like :fire:)")
        self.emoji_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.emoji_btn.setStyleSheet(
            "QPushButton{background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 6px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.emoji_btn.clicked.connect(self._open_emoji_picker)
        self._picker = None
        header.addWidget(self.emoji_btn)
        # GIF picker button: browse/send saved GIF favorites, add a GIF by URL.
        self.gif_btn = QtWidgets.QPushButton("🎬")
        self.gif_btn.setToolTip("GIFs — send a saved favorite, or add one by URL")
        self.gif_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.gif_btn.setStyleSheet(
            "QPushButton{background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 6px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.gif_btn.clicked.connect(self._open_gif_picker)
        header.addWidget(self.gif_btn)
        header.addStretch(1)
        self.arrow = QtWidgets.QLabel("→")
        header.addWidget(self.arrow)
        # Friends button: shows the current recipient; click for the friends panel
        # (pick recipient, /friend add / accept / remove). Replaces the old dropdown.
        self._friends = list(friends)
        self._requests = []
        self._friends_panel = None
        self.friends_btn = QtWidgets.QPushButton()
        self.friends_btn.setToolTip("friends — pick recipient, add / accept / remove")
        self.friends_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.friends_btn.setStyleSheet(
            "QPushButton{color:#ddd; background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 8px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.friends_btn.clicked.connect(self._open_friends_panel)
        self._update_friends_btn()
        header.addWidget(self.friends_btn)
        root.addWidget(self.header)

        # --- body (hidden when collapsed) ---
        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        # Scrollable full history -- shown only while OPENED (actively typing).
        self.view = QtWidgets.QTextEdit(readOnly=True)
        body.addWidget(self.view, 1)
        # Passive HUD: a bottom-anchored stack of individually-fading lines shown while the
        # game is focused (pure floating text, no chrome). The lines sit inside ONE shared,
        # semi-transparent panel (not per-message bubbles) so they read as a single chat
        # background; each line's text still fades out on its own timer.
        self.hud = QtWidgets.QWidget()
        hud_outer = QtWidgets.QVBoxLayout(self.hud)
        hud_outer.setContentsMargins(0, 0, 0, 0)
        hud_outer.setSpacing(0)
        hud_outer.addStretch(1)                  # pin the panel to the bottom
        self._hud_panel = QtWidgets.QWidget()
        self._hud_panel.setStyleSheet(
            f"background: rgba(10,12,16,{BG_ALPHA}); border-radius:6px;")
        self._hud_layout = QtWidgets.QVBoxLayout(self._hud_panel)
        self._hud_layout.setContentsMargins(8, 5, 8, 5)
        self._hud_layout.setSpacing(1)
        hud_outer.addWidget(self._hud_panel)
        self._hud_panel.setVisible(False)        # only shown once it holds a line
        self._hud_lines = []
        body.addWidget(self.hud, 1)
        self.input = _ComposeEdit()
        self.input.setPlaceholderText("Type your message here...")
        self.input.returnPressed.connect(self._on_submit)
        body.addWidget(self.input)
        root.addWidget(self.body, 1)
        self._apply_font()                       # size the transcript + input box

        # --- dynamic display ---
        # Two modes while expanded: OPENED (focused via Enter / SUPER+SHIFT+P) shows the full
        # scrollable history + compose box; PASSIVE (expanded but focus is back on the game) is
        # a chrome-less HUD showing only the most recent lines, each of which fades out on its
        # own timer so the overlay reads as floating chat over the game. Driven by set_opened().
        self._opened = False
        self.hud.setVisible(False)
        self.input.setVisible(False)             # shown only when opened (focused)
        self._sync_visibility()

        # ESC unfocuses the chat (hands focus back to the game), just like SUPER+SHIFT+P.
        # It's a WindowShortcut, so it only fires while the overlay is focused -> ESC can
        # only UNfocus, never focus (and it never reaches the game's menu while we're typing).
        QtGui.QShortcut(QtGui.QKeySequence("Esc"), self, activated=self.dismissed.emit)
        # Focused-only fallbacks for font size (the global SUPER+SHIFT+± binds go
        # through Hyprland -> SIGRTMIN; these work when the overlay itself has focus).
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+="), self, activated=lambda: self.bump_font(1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl++"), self, activated=lambda: self.bump_font(1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+-"), self, activated=lambda: self.bump_font(-1))

    # ---- font sizing ----
    _FONT_MIN, _FONT_MAX = 8, 40

    def _apply_font(self) -> None:
        """(Re)apply the current font family + size + transparency to the transcript + input."""
        in_alpha = min(235, BG_ALPHA + 70)      # compose box a touch more visible
        fam = _css_family(self._font_family)
        self.view.setStyleSheet(
            f"QTextEdit{{background:rgba(10,12,16,{BG_ALPHA}); color:#e6e6e6;"
            "border:none; border-radius:6px; padding:4px;"
            f"font-family:{fam}; font-size:{self._font_px}px;}}")
        self.input.setStyleSheet(
            f"QLineEdit{{background:rgba(20,24,30,{in_alpha}); color:#fff;"
            "border:1px solid rgba(90,100,120,110); border-radius:6px; padding:5px;"
            f"font-family:{fam}; font-size:{self._font_px}px;}}")

    def set_font_family(self, name: str) -> bool:
        """Change the chat font family, re-render, and report whether `name` is an installed
        family (Qt still applies + falls back to a close match if not)."""
        self._font_family = _norm_family(name)
        self._apply_font()
        if not self._collapsed and not self._opened:
            self._render_passive()               # reseed HUD lines in the new font
        if self._font_family.lower() in ("monospace", "serif", "sans-serif", "cursive", "fantasy"):
            return True                          # generics always resolve
        fams = QtGui.QFontDatabase.families()
        return any(self._font_family.lower() == f.lower() for f in fams)

    @staticmethod
    def available_fonts() -> list[str]:
        """Installed font families (for the /font list command)."""
        return list(QtGui.QFontDatabase.families())

    def bump_font(self, delta: int) -> None:
        """Grow/shrink the chat font by `delta` px (clamped). No-op at the limits."""
        new = max(self._FONT_MIN, min(self._FONT_MAX, self._font_px + delta))
        if new == self._font_px:
            return
        self._font_px = new
        self._apply_font()
        if not self._collapsed and not self._opened:
            self._render_passive()               # rebuild HUD lines at the new size
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- public API (call from the Qt main thread only) ----

    # Colours per author class.
    _C_YOU = "#9bf6a0"        # our own messages (green)
    _C_OTHER = "#c8d0dc"      # another player (soft white)
    _C_TUNNEL = "#7ec8ff"     # encrypted/decrypted tunnel (cyan)
    _C_WHISPER = "#ff8fd0"    # whispers (magenta)
    _C_PARTY = "#ffd479"      # party chat (amber)
    _C_EMOTE = "#b9a9ff"      # /me emotes (purple)
    _C_DIM = "#7a828f"        # connectives ("whispers:", "to")
    _C_SYS = "#888"           # server/console lines

    # ---- dynamic display tuning ----
    _FADE_MS = 900          # per-line fade-out animation length
    _LINGER_MS = 8000       # passive: each line stays fully visible this long, then fades
    _PASSIVE_MAX = 8        # passive HUD keeps at most this many recent lines on screen

    def add_message(self, msg) -> None:
        """Store + render a chatframe.Msg, honoring the current display filter."""
        self._entries.append(("msg", msg))
        if self._collapsed:
            pass                                 # pill: nothing to draw (unread badge below)
        elif self._opened:
            if self._passes(msg):
                if getattr(msg, "is_gif", False):
                    self._append_gif_opened(msg)
                else:
                    self._append_html(self._format_message(msg))
        elif self._passes(msg):
            if getattr(msg, "is_gif", False):
                self._hud_add_gif(msg)           # animated GIF as a fading HUD line
            else:
                self._hud_add(self._format_message(msg))   # own-timer fading HUD line
        if not msg.is_self:
            self._note_activity()

    def _format_message(self, msg) -> str:
        """Build the HTML line for a chatframe.Msg (no side effects)."""
        name = html.escape(msg.sender)
        # Expand emoji shortcodes/emoticons BEFORE escaping (glyphs are HTML-safe, and
        # "<3" must be matched before "<" would become "&lt;"). Display-side only, so the
        # encrypted wire keeps the compact shortcode.
        body = html.escape(emoji_util.emojize(msg.body)).replace("\n", "<br>")
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

        if msg.kind == "party":
            who = "you" if msg.is_self else name
            who_color = self._C_YOU if msg.is_self else self._C_PARTY
            line = (f'<span style="color:{self._C_PARTY}">{lock}[P] </span>'
                    f'<span style="color:{who_color}"><b>{html.escape(who)}:</b></span> {body}')
        elif msg.kind == "emote":
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
        return line

    def add_system(self, text: str) -> None:
        self._entries.append(("sys", text))
        if self._collapsed:
            return
        if self._opened:
            self._append_html(self._format_system(text))
        else:
            self._hud_add(self._format_system(text))

    def _format_system(self, text: str) -> str:
        return f'<span style="color:{self._C_SYS}">· {html.escape(text)}</span>'

    # ---- GIF rendering ----
    # A GIF message carries a URL (msg.gif_url). We download+cache it (gif_util, off-thread)
    # and animate it: in the opened QTextEdit via an <img> whose pixmap a QMovie swaps each
    # frame (text rendering untouched); in the passive HUD via a QLabel.setMovie (native).

    @staticmethod
    def _gif_id(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    def _ensure_gif(self, url: str) -> bool:
        """Return True if the GIF is already cached; else kick off a background download and
        return False (self._gif_ready fires when it lands, triggering a re-render)."""
        if not url or gif_util.is_cached(url):
            return bool(url) and gif_util.is_cached(url)
        if url not in self._gif_pending:
            self._gif_pending.add(url)

            def _work() -> None:
                gif_util.fetch(url)
                self._gif_ready.emit(url)        # queued back to the Qt thread
            threading.Thread(target=_work, daemon=True).start()
        return False

    def _on_gif_ready(self, url: str) -> None:
        # A GIF finished downloading. Update it IN PLACE (don't re-render the whole transcript
        # -- that would reorder messages that arrived while it loaded, and reset every HUD
        # fade). Opened: start the pump for its already-placed <img>. Passive: swap the movie
        # into the loading HUD line that's already sitting in the right spot.
        self._gif_pending.discard(url)
        if self._collapsed:
            return
        if self._opened:
            gid = self._gif_id(url)
            if gid in self._gif_placed:
                self._start_pump(gid, url)
        else:
            self._hud_gif_loaded(url)

    def _make_movie(self, url: str, max_px: int):
        """A started, aspect-scaled QMovie for a cached GIF, or None."""
        mv = QtGui.QMovie(str(gif_util.cache_path(url)))
        if not mv.isValid():
            return None
        mv.jumpToFrame(0)
        sz = mv.currentImage().size()
        if sz.width() > 0 and sz.height() > 0:
            mv.setScaledSize(sz.scaled(max_px, max_px,
                                       QtCore.Qt.AspectRatioMode.KeepAspectRatio))
        return mv

    def _clear_pumps(self) -> None:
        for mv in self._gif_pumps.values():
            mv.stop()
        self._gif_pumps = {}
        self._gif_placed = set()

    def _gif_placeholder(self):
        """A small 'loading' box used as the <img> resource until the GIF downloads, so the
        image sits in the transcript in order and just gets its frames swapped in when ready."""
        pm = QtGui.QPixmap(160, 90)
        pm.fill(QtGui.QColor(20, 24, 30, 140))
        p = QtGui.QPainter(pm)
        p.setPen(QtGui.QColor("#9aa4b2"))
        p.drawText(pm.rect(), int(QtCore.Qt.AlignmentFlag.AlignCenter), "🎞️ loading GIF…")
        p.end()
        return pm

    def _start_pump(self, gid: str, url: str) -> None:
        """Animate an <img src="gif://gid"> already in the opened QTextEdit by swapping its
        image resource each QMovie frame. One relayout to size it to the gif, then per-frame
        viewport repaints (constant size -> no more relayout)."""
        if gid in self._gif_pumps:
            return
        mv = self._make_movie(url, GIF_MAX_OPENED)
        if mv is None:
            return
        doc = self.view.document()
        rtype = QtGui.QTextDocument.ResourceType.ImageResource
        qurl = QtCore.QUrl(f"gif://{gid}")

        def _on_frame(_i) -> None:
            doc.addResource(rtype, qurl, mv.currentPixmap())
            self.view.viewport().update()
        doc.addResource(rtype, qurl, mv.currentPixmap())
        doc.markContentsDirty(0, doc.characterCount())     # size the <img> to the gif (once)
        mv.frameChanged.connect(_on_frame)
        mv.start()
        self._gif_pumps[gid] = mv

    def _gif_caption(self, msg) -> str:
        who = "you" if msg.is_self else html.escape(msg.sender)
        colour = self._C_YOU if msg.is_self else self._C_WHISPER
        return (f'🔒 <span style="color:{colour}"><b>{who}</b></span>'
                f'<span style="color:{self._C_DIM}">:</span>')

    def _append_gif_opened(self, msg) -> None:
        # Place the <img> in order NOW (with a loading placeholder as its resource); animate in
        # place when the download lands -- so messages that arrive meanwhile keep their order.
        gid = self._gif_id(msg.gif_url)
        self.view.document().addResource(
            QtGui.QTextDocument.ResourceType.ImageResource,
            QtCore.QUrl(f"gif://{gid}"), self._gif_placeholder())
        self._append_html(self._gif_caption(msg) + f'<br><img src="gif://{gid}">')
        self._gif_placed.add(gid)
        if self._ensure_gif(msg.gif_url):            # already cached -> animate immediately
            self._start_pump(gid, msg.gif_url)

    def _hud_add_gif(self, msg) -> None:
        """Add an animated GIF (with sender caption) as a fading HUD line. If it isn't cached
        yet, show a 'loading' line in place and swap the movie in when it lands (no reseed)."""
        line = _FadingGif(self._gif_caption(msg), self._font_px, self._font_family,
                          self._LINGER_MS, self._FADE_MS, self._hud_remove)
        if self._ensure_gif(msg.gif_url):
            mv = self._make_movie(msg.gif_url, GIF_MAX_HUD)
            if mv is not None:
                line.set_movie(mv)
        else:
            self._hud_gif_by_url[msg.gif_url] = line   # fill it in when the download completes
        self._hud_lines.append(line)
        self._hud_layout.addWidget(line)
        while len(self._hud_lines) > self._PASSIVE_MAX:
            self._hud_lines.pop(0).kill()
        self._hud_panel.setVisible(True)

    def _hud_gif_loaded(self, url: str) -> None:
        line = self._hud_gif_by_url.pop(url, None)
        if line is None or line not in self._hud_lines:
            return                                   # the line already faded away
        mv = self._make_movie(url, GIF_MAX_HUD)
        if mv is not None:
            line.set_movie(mv)

    # ---- opened (full history) vs passive (fading HUD) ----

    def set_opened(self, opened: bool) -> None:
        """Switch display mode. opened=True (focused): full scrollable history + compose box
        (and keyboard focus in the input, so you can type right away). opened=False (game
        focused): a chrome-less HUD of recent lines that each fade out on their own timer."""
        if opened == self._opened:
            return
        self._opened = opened
        self._sync_visibility()
        if opened:
            self._rebuild()                      # restore the full history
            # The compose box was just shown; give it keyboard focus so SUPER+SHIFT+P /
            # the Enter hotkey actually let you type (else you'd only *see* the box).
            self.input.setFocus()
        else:
            self._clear_pumps()                  # stop opened-view GIF animations (view hidden)
            self._render_passive()

    def _sync_visibility(self) -> None:
        """Show exactly the widgets the current collapsed/opened state calls for.
        PASSIVE (expanded, game-focused) hides all chrome -> pure floating text."""
        if self._collapsed:
            self.header.setVisible(True)         # the pill itself lives in the header
            self.body.setVisible(False)
            self.arrow.setVisible(False)
            for b in (self.filter_btn, self.emoji_btn, self.gif_btn, self.friends_btn):
                b.setVisible(False)
            return
        chrome = self._opened
        self.header.setVisible(chrome)           # passive -> no header/buttons
        self.arrow.setVisible(chrome)
        for b in (self.filter_btn, self.emoji_btn, self.gif_btn, self.friends_btn):
            b.setVisible(chrome)
        self.body.setVisible(True)
        self.view.setVisible(self._opened)       # opened -> scrollable history
        self.hud.setVisible(not self._opened)    # passive -> fading HUD
        self.input.setVisible(self._opened)

    # ---- passive HUD (individually-fading lines) ----

    def _hud_add(self, html_text: str) -> None:
        """Append one line to the passive HUD; it lingers, then fades on its own timer."""
        line = _FadingLine(html_text, self._font_px, self._LINGER_MS,
                           self._FADE_MS, self._hud_remove, self._font_family)
        self._hud_lines.append(line)
        self._hud_layout.addWidget(line)
        while len(self._hud_lines) > self._PASSIVE_MAX:
            self._hud_lines.pop(0).kill()
        self._hud_panel.setVisible(True)

    def _hud_remove(self, line) -> None:
        """A line finished fading (or is being culled): drop it from the HUD."""
        if line in self._hud_lines:
            self._hud_lines.remove(line)
        for u, ln in list(self._hud_gif_by_url.items()):   # forget any pending-GIF ref to it
            if ln is line:
                del self._hud_gif_by_url[u]
        line.kill()
        if not self._hud_lines:                  # nothing left -> hide the shared panel
            self._hud_panel.setVisible(False)

    def _hud_clear(self) -> None:
        for line in self._hud_lines:
            line.kill()
        self._hud_lines = []
        self._hud_gif_by_url = {}
        self._hud_panel.setVisible(False)

    def _render_passive(self) -> None:
        """Reseed the HUD with the most recent visible lines (each starts its own fade)."""
        self._hud_clear()
        shown = [(k, p) for (k, p) in self._entries
                 if k == "sys" or self._passes(p)]
        for k, p in shown[-self._PASSIVE_MAX:]:
            if k == "sys":
                self._hud_add(self._format_system(p))
            elif getattr(p, "is_gif", False):
                self._hud_add_gif(p)
            else:
                self._hud_add(self._format_message(p))

    # ---- display filter (cycled by the header button) ----
    # (key, short label). 'all' = everything; 'private' = party + whispers only;
    # 'tunnel' = only encrypted (decrypted) messages.
    _FILTERS = (
        ("all", "all"),
        ("private", "party+dm"),
        ("tunnel", "encrypted"),
    )

    def _passes(self, msg) -> bool:
        key = self._FILTERS[self._filter_idx][0]
        if key == "private":
            return msg.kind in ("party", "whisper_in", "whisper_out")
        if key == "tunnel":
            return bool(msg.is_tunnel)
        return True

    def _cycle_filter(self) -> None:
        self._filter_idx = (self._filter_idx + 1) % len(self._FILTERS)
        self._update_filter_btn()
        if not self._collapsed:
            self._rebuild() if self._opened else self._render_passive()

    def _update_filter_btn(self) -> None:
        self.filter_btn.setText(self._FILTERS[self._filter_idx][1])

    def _rebuild(self) -> None:
        """Re-render the whole transcript from stored entries under the current filter.
        System/status lines always show; chat messages are filtered."""
        self._clear_pumps()                          # stop old GIF animations before clearing
        self.view.clear()
        for kind, payload in self._entries:
            if kind == "sys":
                self.view.append(self._format_system(payload))
            elif self._passes(payload):
                if getattr(payload, "is_gif", False):
                    self._append_gif_opened(payload)
                else:
                    self.view.append(self._format_message(payload))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- emoji picker ----

    def _open_emoji_picker(self) -> None:
        if self._picker is None:
            self._picker = EmojiPicker(self, self._insert_shortcode)
        self._picker.popup(self.emoji_btn)

    def _insert_shortcode(self, ch: str) -> None:
        """Insert the picked emoji's :shortcode: at the compose-box cursor."""
        self.input.insert(emoji_util.to_shortcode(ch))
        self.input.setFocus()

    # ---- gif picker ----

    def _open_gif_picker(self) -> None:
        if self._gif_picker is None:
            self._gif_picker = GifPicker(self)
        self._gif_picker.rebuild()
        self._gif_picker.popup(self.gif_btn)

    # ---- collapse / expand ----

    _PILL_STYLE = ("color:#8fd; font-weight:bold; background:rgba(20,24,30,235);"
                   "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;")
    _TITLE_STYLE = "color:#8fd; font-weight:bold;"

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self.title.setText("🔒 ▸")
            self.title.setStyleSheet(self._PILL_STYLE)
            self._hud_clear()                    # stop any in-flight HUD fades
            self._clear_pumps()                  # stop opened-view GIF animations too
            self._sync_visibility()
            # Force the size: equal min==max makes Hyprland/Windows honor the shrink
            # (a soft resize/adjustSize is otherwise ignored, leaving a frosted box).
            self.setFixedSize(132, 34)
        else:
            self._unread = 0
            self.title.setText("")               # no name in expanded view
            self.title.setStyleSheet(self._TITLE_STYLE)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(self._expanded_size)
            self._sync_visibility()              # compose/history only in the opened state
            if self._opened:
                self._rebuild()
                self.input.setFocus()
            else:
                self._render_passive()           # HUD view until Enter/focus opens it
            self.raise_()
            self.activateWindow()
        self.collapsed_changed.emit()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

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
        self._update_friends_btn()

    # ---- friends panel ----

    def _update_friends_btn(self) -> None:
        """Label the friends button with the current recipient (+ a badge for requests)."""
        badge = f" ●{len(self._requests)}" if self._requests else ""
        self.friends_btn.setText(f"👥 {self.recipient or '—'}{badge}")

    def refresh_friends(self, friends: list, requests: list | None = None) -> None:
        """Update the known-friends list + pending inbound requests (from the app)."""
        self._friends = list(friends)
        self._requests = list(requests or [])
        self._update_friends_btn()
        if self._friends_panel is not None and self._friends_panel.isVisible():
            self._friends_panel.rebuild(self._friends, self._requests, self.recipient)

    def _open_friends_panel(self) -> None:
        if self._friends_panel is None:
            self._friends_panel = FriendsPanel(self, self._on_friend_event)
        self._friends_panel.rebuild(self._friends, self._requests, self.recipient)
        self._friends_panel.popup(self.friends_btn)

    def _on_friend_event(self, action: str, name: str) -> None:
        """Route a panel click. 'select' is local; add/accept/remove go to the app."""
        if action == "select":
            self._set_recipient(name)
        else:
            self.friend_action.emit(action, name)

    def _append_html(self, line: str) -> None:
        self.view.append(line)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def prefill(self, text: str) -> None:
        """Put `text` in the compose box with the cursor at the end (used by the '/' quick-
        command hotkey). Only meaningful when opened; the input holds it either way."""
        self.input.setText(text)
        self.input.setCursorPosition(len(text))
        self.input.deselect()
        self.input.setFocus()

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.remember(text)           # add to the Up/Down recall history
            self.input.clear()
            self.submitted.emit(text)
        else:
            self.dismissed.emit()               # empty Enter -> hand focus back to the game


class _ComposeEdit(QtWidgets.QLineEdit):
    """The compose box, with in-game-style Up/Down recall of previously sent lines.

    Up walks back through history (most-recent first); Down walks forward again and,
    past the newest, restores whatever draft you were typing. The recalled text just
    populates the box -- you can send it as-is or edit it first, like Hytale's own chat."""

    def __init__(self) -> None:
        super().__init__()
        self._history: list[str] = []   # submitted lines, oldest -> newest
        self._idx: int | None = None    # None = editing the live draft; else index in history
        self._draft = ""                # the draft saved when you start browsing up

    def remember(self, text: str) -> None:
        """Record a just-submitted line and reset the browse position to the draft."""
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._idx = None
        self._draft = ""

    def keyPressEvent(self, e) -> None:
        key = e.key()
        if key == QtCore.Qt.Key.Key_Up:
            self._browse(-1)
            return
        if key == QtCore.Qt.Key.Key_Down:
            self._browse(+1)
            return
        super().keyPressEvent(e)

    def _browse(self, direction: int) -> None:
        if not self._history:
            return
        if direction < 0:                        # Up: older
            if self._idx is None:                # leaving the draft -> newest history entry
                self._draft = self.text()
                self._idx = len(self._history) - 1
            elif self._idx > 0:
                self._idx -= 1
            else:
                return                           # already at the oldest
        else:                                    # Down: newer
            if self._idx is None:
                return                           # already at the draft
            self._idx += 1
            if self._idx >= len(self._history):  # past the newest -> back to the draft
                self._idx = None
                self.setText(self._draft)
                self.end(False)
                return
        self.setText(self._history[self._idx])
        self.end(False)                          # cursor to end, no selection


class _FadingLine(QtWidgets.QLabel):
    """One line in the passive HUD. It stays fully opaque for `linger_ms`, then fades to
    nothing over `fade_ms` and calls `on_dead(self)` so the HUD can drop it. Each line runs
    its own timer, so messages fade independently (not the whole transcript at once)."""

    def __init__(self, html_text: str, font_px: int, linger_ms: int,
                 fade_ms: int, on_dead, font_family: str = "monospace") -> None:
        super().__init__()
        self._on_dead = on_dead
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setWordWrap(True)
        # A word-wrapped QLabel must advertise height-for-width, else a layout gives it a
        # one-line height and a 3-line message overlaps the line below it. Force it so wrapped
        # lines get their full vertical space.
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        sp.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Minimum)
        self.setSizePolicy(sp)
        self.setText(html_text)
        self.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        # Don't intercept the mouse -- the game keeps it while these float on top.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Transparent: the shared HUD panel behind the lines provides the background, so the
        # messages read as one block rather than separate per-message boxes.
        self.setStyleSheet("background: transparent; color:#e6e6e6;"
                           f"font-family:{_css_family(font_family)}; font-size:{font_px}px;")
        self._eff = QtWidgets.QGraphicsOpacityEffect(self)
        self._eff.setOpacity(1.0)
        self.setGraphicsEffect(self._eff)
        self._anim = QtCore.QPropertyAnimation(self._eff, b"opacity", self)
        self._anim.setDuration(fade_ms)
        self._anim.finished.connect(self._finished)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._begin_fade)
        self._timer.start(linger_ms)

    def _begin_fade(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _finished(self) -> None:
        if self._eff.opacity() <= 0.01 and self._on_dead is not None:
            cb, self._on_dead = self._on_dead, None
            cb(self)

    def kill(self) -> None:
        """Stop timers/animation and remove the widget (idempotent)."""
        self._on_dead = None
        self._timer.stop()
        self._anim.stop()
        mv = self.movie()                        # a GIF line: stop its animation too
        if mv is not None:
            mv.stop()
        self.setParent(None)
        self.deleteLater()


class _FadingGif(QtWidgets.QWidget):
    """A HUD line for a GIF: a sender caption above the animated GIF (or a 'loading' note
    until it downloads), fading on its own timer just like _FadingLine. Kept as a small
    composite (caption + gif labels) because a single QLabel can't show both text and a movie."""

    def __init__(self, caption_html: str, font_px: int, font_family: str,
                 linger_ms: int, fade_ms: int, on_dead) -> None:
        super().__init__()
        self._on_dead = on_dead
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 1, 0, 1)
        v.setSpacing(1)
        self._cap = QtWidgets.QLabel(caption_html)
        self._cap.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self._cap.setWordWrap(True)
        self._cap.setStyleSheet("background:transparent; color:#e6e6e6;"
                                f"font-family:{_css_family(font_family)}; font-size:{font_px}px;")
        self._gif = QtWidgets.QLabel("🎞️ loading GIF…")
        self._gif.setStyleSheet("background:transparent; color:#7a828f;"
                                f"font-family:{_css_family(font_family)}; font-size:{font_px}px;")
        v.addWidget(self._cap)
        v.addWidget(self._gif)
        self._eff = QtWidgets.QGraphicsOpacityEffect(self)
        self._eff.setOpacity(1.0)
        self.setGraphicsEffect(self._eff)
        self._anim = QtCore.QPropertyAnimation(self._eff, b"opacity", self)
        self._anim.setDuration(fade_ms)
        self._anim.finished.connect(self._finished)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._begin_fade)
        self._timer.start(linger_ms)

    def set_movie(self, mv) -> None:
        self._gif.setText("")
        self._gif.setMovie(mv)
        mv.start()

    def _begin_fade(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _finished(self) -> None:
        if self._eff.opacity() <= 0.01 and self._on_dead is not None:
            cb, self._on_dead = self._on_dead, None
            cb(self)

    def kill(self) -> None:
        self._on_dead = None
        self._timer.stop()
        self._anim.stop()
        mv = self._gif.movie()
        if mv is not None:
            mv.stop()
        self.setParent(None)
        self.deleteLater()


class EmojiPicker(QtWidgets.QWidget):
    """A small popup grid of emoji. Clicking one inserts its :shortcode: via `on_pick`.

    Shows a curated set by default; the search box filters the full emoji database when the
    `emoji` lib is present. Frameless Qt.Popup so it closes when you click elsewhere.
    """

    _COLS = 8

    def __init__(self, parent, on_pick):
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self._on_pick = on_pick
        self.setStyleSheet("background:rgba(18,21,27,245);"
                           "border:1px solid #3a4150; border-radius:8px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(
            "search emoji…" if emoji_util.available() else "type :codes: (emoji lib not installed)")
        self.search.setStyleSheet(
            "QLineEdit{background:rgba(10,12,16,220); color:#fff;"
            "border:1px solid #3a4150; border-radius:6px; padding:4px;}")
        self.search.textChanged.connect(self._refresh)
        self.search.setEnabled(emoji_util.available())
        lay.addWidget(self.search)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self._host)
        self.grid.setSpacing(2)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self._host)
        lay.addWidget(self.scroll)

        self.setFixedSize(324, 300)
        self._refresh()

    def _clear(self) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def _refresh(self, *_) -> None:
        q = self.search.text().strip()
        chars = emoji_util.search(q) if q else emoji_util.PICKER_EMOJI
        self._clear()
        for i, ch in enumerate(chars):
            b = QtWidgets.QToolButton()
            b.setText(ch)
            b.setToolTip(emoji_util.to_shortcode(ch))
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton{border:none; font-size:20px; padding:3px;}"
                " QToolButton:hover{background:rgba(60,66,80,180); border-radius:4px;}")
            b.clicked.connect(lambda _=False, c=ch: self._pick(c))
            self.grid.addWidget(b, i // self._COLS, i % self._COLS)

    def _pick(self, ch: str) -> None:
        self._on_pick(ch)
        self.close()

    def popup(self, anchor) -> None:
        """Show just below the `anchor` widget."""
        gp = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        self.move(gp)
        self.show()
        self.raise_()
        self.search.setFocus()


class FriendsPanel(QtWidgets.QWidget):
    """Popup friends list: pick the whisper recipient, accept pending requests, and
    add/remove friends. Emits events through `on_event(action, name)` where action is
    one of 'select', 'add', 'accept', 'remove' (the app performs the X25519 handshake)."""

    def __init__(self, parent, on_event):
        super().__init__(parent, QtCore.Qt.WindowType.Popup)
        self._on_event = on_event
        self.setStyleSheet("background:rgba(18,21,27,245);"
                           "border:1px solid #3a4150; border-radius:8px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._host = QtWidgets.QWidget()
        self.rows = QtWidgets.QVBoxLayout(self._host)
        self.rows.setSpacing(3)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.addStretch(1)
        self.scroll.setWidget(self._host)
        lay.addWidget(self.scroll, 1)

        add = QtWidgets.QHBoxLayout()
        add.setSpacing(4)
        self.add_input = QtWidgets.QLineEdit()
        self.add_input.setPlaceholderText("add friend by name…")
        self.add_input.setStyleSheet(
            "QLineEdit{background:rgba(10,12,16,220); color:#fff;"
            "border:1px solid #3a4150; border-radius:6px; padding:4px;}")
        self.add_input.returnPressed.connect(self._do_add)
        add.addWidget(self.add_input, 1)
        add_btn = QtWidgets.QPushButton("add")
        add_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            "QPushButton{color:#9bf6a0; background:rgba(34,40,34,120);"
            "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;}"
            " QPushButton:hover{background:rgba(44,60,44,180);}")
        add_btn.clicked.connect(self._do_add)
        add.addWidget(add_btn)
        lay.addLayout(add)

        self.setFixedSize(260, 300)

    # ---- build ----

    def _clear_rows(self) -> None:
        while self.rows.count() > 1:                  # keep the trailing stretch
            item = self.rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def rebuild(self, friends: list, requests: list, recipient: str) -> None:
        self._clear_rows()
        idx = 0
        for name in requests:
            self.rows.insertWidget(idx, self._request_row(name)); idx += 1
        if requests and friends:
            sep = QtWidgets.QFrame(); sep.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            sep.setStyleSheet("color:#333;")
            self.rows.insertWidget(idx, sep); idx += 1
        if not friends:
            empty = QtWidgets.QLabel("no friends yet — add one below")
            empty.setStyleSheet("color:#7a828f; padding:6px;")
            self.rows.insertWidget(idx, empty); idx += 1
        for name in friends:
            self.rows.insertWidget(idx, self._friend_row(name, name == recipient)); idx += 1

    def _request_row(self, name: str) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(2, 2, 2, 2); h.setSpacing(4)
        lbl = QtWidgets.QLabel(f"⏳ {html.escape(name)}")
        lbl.setStyleSheet("color:#ffd479;")
        h.addWidget(lbl, 1)
        acc = QtWidgets.QPushButton("accept")
        acc.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        acc.setStyleSheet(
            "QPushButton{color:#9bf6a0; background:rgba(34,40,34,120);"
            "border:1px solid #3a4150; border-radius:5px; padding:2px 8px;}"
            " QPushButton:hover{background:rgba(44,60,44,180);}")
        acc.clicked.connect(lambda _=False, n=name: self._emit("accept", n))
        h.addWidget(acc)
        return row

    def _friend_row(self, name: str, is_current: bool) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(2, 2, 2, 2); h.setSpacing(4)
        pick = QtWidgets.QPushButton(("● " if is_current else "○ ") + name)
        pick.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        pick.setToolTip("set as whisper recipient")
        colour = "#9bf6a0" if is_current else "#ddd"
        pick.setStyleSheet(
            f"QPushButton{{color:{colour}; text-align:left; background:transparent;"
            "border:none; padding:3px 4px;}"
            " QPushButton:hover{background:rgba(44,49,60,150); border-radius:5px;}")
        pick.clicked.connect(lambda _=False, n=name: self._emit("select", n))
        h.addWidget(pick, 1)
        rm = QtWidgets.QPushButton("✕")
        rm.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        rm.setToolTip("remove friend")
        rm.setStyleSheet(
            "QPushButton{color:#ff8f8f; background:transparent; border:none; padding:3px 6px;}"
            " QPushButton:hover{background:rgba(70,40,40,180); border-radius:5px;}")
        rm.clicked.connect(lambda _=False, n=name: self._emit("remove", n))
        h.addWidget(rm)
        return row

    def _do_add(self) -> None:
        name = self.add_input.text().strip()
        if name:
            self.add_input.clear()
            self._emit("add", name)

    def _emit(self, action: str, name: str) -> None:
        self._on_event(action, name)
        # 'select' just changes recipient (keep panel open); the rest close it.
        if action != "select":
            self.close()

    def popup(self, anchor) -> None:
        gp = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        self.move(gp)
        self.show()
        self.raise_()


class GifPicker(QtWidgets.QWidget):
    """Popup GIF library: save GIFs by pasting a direct URL, browse your favorites +
    recents as thumbnails, click one to send it (encrypted) to the current recipient,
    or ✕ to remove a favorite. No search service -- purely your own saved GIFs."""

    _COLS = 3
    _THUMB = 96

    def __init__(self, overlay):
        super().__init__(overlay, QtCore.Qt.WindowType.Popup)
        self._ov = overlay
        self.setStyleSheet("background:rgba(18,21,27,245);"
                           "border:1px solid #3a4150; border-radius:8px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        add = QtWidgets.QHBoxLayout()
        add.setSpacing(4)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("paste a direct .gif URL, press Enter to save")
        self.url.setStyleSheet(
            "QLineEdit{background:rgba(10,12,16,220); color:#fff;"
            "border:1px solid #3a4150; border-radius:6px; padding:4px;}")
        self.url.returnPressed.connect(self._add)
        add.addWidget(self.url, 1)
        save = QtWidgets.QPushButton("save")
        save.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        save.setStyleSheet(
            "QPushButton{color:#9bf6a0; background:rgba(34,40,34,120);"
            "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;}"
            " QPushButton:hover{background:rgba(44,60,44,180);}")
        save.clicked.connect(self._add)
        add.addWidget(save)
        lay.addLayout(add)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self._host)
        self.grid.setSpacing(4)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self._host)
        lay.addWidget(self.scroll, 1)

        self._hint = QtWidgets.QLabel()
        self._hint.setStyleSheet("color:#7a828f; font-size:11px;")
        lay.addWidget(self._hint)

        self.setFixedSize(340, 380)
        # a thumbnail that finished downloading -> refresh the grid (if we're still open)
        overlay._gif_ready.connect(lambda _=None: self.isVisible() and self.rebuild())

    def _add(self) -> None:
        url = self.url.text().strip()
        if not gif_util.valid_url(url):
            self._hint.setText("needs a direct http(s) .gif URL")
            return
        self.url.clear()
        self._ov.gif_action.emit("add", url)         # app persists the favorite
        self._ov._ensure_gif(url)                    # start caching for the thumbnail
        self.rebuild()

    def _clear(self) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def rebuild(self) -> None:
        self._clear()
        urls = list(gif_util.favorites())
        for u in gif_util.recents():                 # append recents not already favorited
            if u not in urls:
                urls.append(u)
        for i, url in enumerate(urls):
            self.grid.addWidget(self._tile(url), i // self._COLS, i % self._COLS)
        self._hint.setText("click a GIF to send · ✕ removes a favorite"
                           if urls else "no GIFs yet — paste a direct .gif URL above")

    def _tile(self, url: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        thumb = QtWidgets.QToolButton()
        thumb.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("send this GIF")
        thumb.setFixedSize(self._THUMB, self._THUMB)
        thumb.setStyleSheet("QToolButton{border:1px solid #2a2f3a; border-radius:6px;"
                            "background:rgba(10,12,16,160);}"
                            " QToolButton:hover{border-color:#5a6a8a;}")
        if gif_util.is_cached(url):
            mv = self._ov._make_movie(url, self._THUMB - 6)   # static first frame as the icon
            if mv is not None:
                mv.jumpToFrame(0)
                thumb.setIcon(QtGui.QIcon(mv.currentPixmap()))
                thumb.setIconSize(mv.currentPixmap().size())
        else:
            thumb.setText("…")
            self._ov._ensure_gif(url)                # download; _gif_ready refreshes us
        thumb.clicked.connect(lambda _=False, u=url: self._send(u))
        v.addWidget(thumb)
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        fav = gif_util.is_favorite(url)
        star = QtWidgets.QPushButton("★" if fav else "☆")
        star.setToolTip("unfavorite" if fav else "save to favorites")
        star.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        star.setStyleSheet("QPushButton{color:%s; background:transparent; border:none;"
                           "font-size:12px;} QPushButton:hover{color:#fff;}"
                           % ("#ffd479" if fav else "#7a828f"))
        star.clicked.connect(lambda _=False, u=url, f=fav: self._toggle_fav(u, f))
        row.addWidget(star)
        row.addStretch(1)
        dele = QtWidgets.QPushButton("✕")
        dele.setToolTip("delete (remove from favorites and recents)")
        dele.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        dele.setStyleSheet("QPushButton{color:#ff8f8f; background:transparent; border:none;"
                           "font-size:11px;} QPushButton:hover{color:#fff;}")
        dele.clicked.connect(lambda _=False, u=url: self._delete(u))
        row.addWidget(dele)
        v.addLayout(row)
        return w

    def _send(self, url: str) -> None:
        self._ov.gif_send.emit(url)
        self.close()

    def _toggle_fav(self, url: str, is_fav: bool) -> None:
        self._ov.gif_action.emit("unfav" if is_fav else "add", url)
        self.rebuild()

    def _delete(self, url: str) -> None:
        self._ov.gif_action.emit("forget", url)      # purge from favorites AND recents
        self.rebuild()

    def popup(self, anchor) -> None:
        gp = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        self.move(gp)
        self.show()
        self.raise_()
        self.url.setFocus()
