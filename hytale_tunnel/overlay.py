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
import re
import threading

from PyQt6 import QtCore, QtGui, QtWidgets

from . import chatfilter, emoji_util, gif_util

# A direct GIF/WebP URL anywhere in a message body -> render it inline as an animated GIF.
_GIF_URL_RE = re.compile(r"https?://\S+\.(?:gif|webp)(?:\?\S*)?", re.IGNORECASE)

# The compact placeholder shown in the compose box for a picked GIF (expands to its URL on
# send, so a GIF mixes into any message -- "/msg Bo hey [GIF] gg", "/p sup [GIF]", or public).
GIF_TOKEN = "[GIF]"
_GIF_TOKEN_RE = re.compile(r"\[GIF\]")


def _split_gifs(text: str):
    """Split a message body into ('text', str) / ('gif', url) segments in order, so inline
    .gif/.webp URLs can be rendered as animated GIFs and the surrounding text left as text."""
    segs, i = [], 0
    for m in _GIF_URL_RE.finditer(text):
        if m.start() > i:
            segs.append(("text", text[i:m.start()]))
        segs.append(("gif", m.group(0)))
        i = m.end()
    if i < len(text):
        segs.append(("text", text[i:]))
    return segs

# GIF display sizing (px, longest side): larger in the opened transcript, smaller in the
# floating HUD so it stays unobtrusive over the game. Rendered as real QMovie widgets.
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


# ---- chat THEMES ---------------------------------------------------------------------------
# Each theme restyles the whole overlay: the palette (author-class colours), the shared panel
# background (a plain rgba OR a Qt qlineargradient), its border/corner-radius, the compose box,
# the font family + size, a decorative wrap around player names (prefix, suffix), and the lock
# glyph on encrypted lines. Real per-run in-game colours are still honoured; a theme recolours
# everything we own (our messages, system/whisper/party, chrome, tags). Pick with /theme <name>
# or the 🎨 button. Keys: font,size,panel,border,radius,input,text,you,other,tunnel,whisper,
# party,emote,dim,sys,name(=(pre,suf)),lock,accent.
_GRAD = "qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {0},stop:1 {1})"
THEMES = {
    "standard": dict(label="Standard", font="monospace", size=14,
        panel="rgba(10,12,16,110)", border="none", radius=6, input="rgba(20,24,30,180)",
        text="#e6e6e6", you="#9bf6a0", other="#c8d0dc", tunnel="#7ec8ff", whisper="#ff8fd0",
        party="#ffd479", emote="#b9a9ff", dim="#7a828f", sys="#888888", name=("", ""),
        lock="🔒 ", accent="#88ffdd"),
    "modern": dict(label="Modern", font="sans-serif", size=14,
        panel="rgba(22,26,33,150)", border="1px solid rgba(120,140,170,45)", radius=8,
        input="rgba(30,35,44,200)", text="#dfe6ee", you="#7fd6a6", other="#b8c4d4",
        tunnel="#6cb6ff", whisper="#e69ad0", party="#f2c66b", emote="#a9b6ff", dim="#8090a0",
        sys="#778699", name=("", ""), lock="🔒 ", accent="#7fd6ff"),
    "medieval": dict(label="Medieval", font="serif", size=15,
        panel=_GRAD.format("rgba(40,29,16,185)", "rgba(24,17,9,195)"),
        border="2px solid #8a6d3b", radius=4, input="rgba(40,30,18,210)", text="#ecd9b0",
        you="#e8c46a", other="#d8c39a", tunnel="#b9d1a0", whisper="#c98fb0", party="#c05a4a",
        emote="#c2a86a", dim="#9a855f", sys="#8a7a5a", name=("❧ ", ""), lock="⚜ ",
        accent="#d4a94a"),
    "futuristic": dict(label="Futuristic", font="monospace", size=14,
        panel="rgba(6,10,18,175)", border="1px solid #2ff3ff", radius=3, input="rgba(10,16,26,215)",
        text="#d6f4ff", you="#5cffb0", other="#9fd0e6", tunnel="#38f0ff", whisper="#ff5cf0",
        party="#ffd23f", emote="#b98fff", dim="#5a8ba0", sys="#4a7a8a", name=("⟨", "⟩"),
        lock="◈ ", accent="#2ff3ff"),
    "lofi": dict(label="Lo-Fi / Comfy", font="sans-serif", size=14,
        panel=_GRAD.format("rgba(44,32,46,155)", "rgba(30,24,34,165)"), border="none", radius=13,
        input="rgba(48,38,50,205)", text="#ece0e6", you="#b8e0b0", other="#d8ccd6",
        tunnel="#a8c8e0", whisper="#e6b0c8", party="#e8cfa0", emote="#c8b8e0", dim="#9a8ea0",
        sys="#8a808c", name=("", ""), lock="☕ ", accent="#d8a8c0"),
    "funny": dict(label="Funny", font="fantasy", size=15,
        panel="rgba(32,20,42,155)", border="2px dashed #ff7fbf", radius=11, input="rgba(42,28,52,205)",
        text="#fff0f8", you="#7fff7f", other="#ffe08a", tunnel="#7fdfff", whisper="#ff8fd0",
        party="#ffb84f", emote="#d08fff", dim="#b0a0c0", sys="#a090b0", name=("🤪 ", ""),
        lock="🎉 ", accent="#ff7fbf"),
    "colorful": dict(label="Colorful", font="sans-serif", size=14,
        panel=_GRAD.format("rgba(16,14,26,150)", "rgba(10,10,20,160)"), border="2px solid #ff5f8f",
        radius=8, input="rgba(20,16,30,200)", text="#f2ecff", you="#4dff88", other="#ffd24d",
        tunnel="#4dd2ff", whisper="#ff4dd2", party="#ff8f4d", emote="#b84dff", dim="#8f8f9f",
        sys="#9a7fbf", name=("🌈 ", ""), lock="🔒 ", accent="#ff4d88"),
    "bland": dict(label="Bland", font="sans-serif", size=14,
        panel="rgba(20,20,22,120)", border="none", radius=4, input="rgba(30,30,32,185)",
        text="#cccccc", you="#dddddd", other="#bbbbbb", tunnel="#cccccc", whisper="#c8c8c8",
        party="#bcbcbc", emote="#c0c0c0", dim="#888888", sys="#777777", name=("", ""),
        lock="· ", accent="#aaaaaa"),
    "cyberpunk": dict(label="Cyberpunk", font="monospace", size=14,
        panel="rgba(8,6,14,185)", border="1px solid #ff2fa0", radius=2, input="rgba(14,8,20,215)",
        text="#e0d0ff", you="#39ff14", other="#b0a0d0", tunnel="#00e5ff", whisper="#ff2fa0",
        party="#ffe14d", emote="#c04dff", dim="#7a5a8a", sys="#6a4a7a", name=("▐ ", ""),
        lock="⚡ ", accent="#ff2fa0"),
    "terminal": dict(label="Terminal", font="monospace", size=14,
        panel="rgba(2,8,3,200)", border="1px solid #29a329", radius=2, input="rgba(4,14,5,220)",
        text="#7dff7d", you="#b6ff9c", other="#7ee87e", tunnel="#59ffd0", whisper="#a0ff70",
        party="#d6ff5a", emote="#8aff8a", dim="#3f8f3f", sys="#2f6f2f", name=("[", "]"),
        lock="# ", accent="#29ff29"),
    "vaporwave": dict(label="Vaporwave", font="sans-serif", size=14,
        panel=_GRAD.format("rgba(40,20,50,155)", "rgba(20,30,55,165)"), border="1px solid #ff8fdf",
        radius=8, input="rgba(40,26,54,205)", text="#f0e0ff", you="#7fffd4", other="#d8b8f0",
        tunnel="#66e0ff", whisper="#ff9fdf", party="#ffd28f", emote="#c88fff", dim="#a088b8",
        sys="#8878a0", name=("✧ ", ""), lock="🌴 ", accent="#ff8fdf"),
    "forest": dict(label="Forest", font="serif", size=14,
        panel=_GRAD.format("rgba(16,26,16,175)", "rgba(10,18,10,185)"), border="1px solid #5a7a3a",
        radius=6, input="rgba(18,28,18,205)", text="#d6e6c0", you="#a8e06a", other="#c0d0a8",
        tunnel="#8fd0b0", whisper="#d0a0b0", party="#d8c070", emote="#b0c88f", dim="#7a8a6a",
        sys="#6a7a5a", name=("🌿 ", ""), lock="🍃 ", accent="#7aab4a"),
}
THEME_ORDER = ["standard", "modern", "medieval", "futuristic", "lofi", "funny",
               "colorful", "bland", "cyberpunk", "terminal", "vaporwave", "forest"]
DEFAULT_THEME = "standard"


class Overlay(QtWidgets.QWidget):
    submitted = QtCore.pyqtSignal(str)          # emitted with composed plaintext
    collapsed_changed = QtCore.pyqtSignal()     # emitted after collapse/expand (resize)
    activation_changed = QtCore.pyqtSignal(bool)  # window gained (True) / lost (False) focus
    dismissed = QtCore.pyqtSignal()             # Enter pressed with empty input -> unfocus
    friend_action = QtCore.pyqtSignal(str, str)  # (action, name): add / accept / remove
    gif_action = QtCore.pyqtSignal(str, str)    # (action, url): add / unfav / forget favorite
    theme_changed = QtCore.pyqtSignal(str)      # (theme name) picked -> app persists it
    _gif_ready = QtCore.pyqtSignal(str)         # (url) a GIF finished downloading -> re-render

    def changeEvent(self, event) -> None:
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            self.activation_changed.emit(self.isActiveWindow())
        super().changeEvent(event)

    def __init__(self, recipient: str, friends: list[str],
                 font_px: int | None = None, size=None, font_family: str | None = None,
                 theme: str | None = None):
        super().__init__()
        self.recipient = recipient
        self._collapsed = False
        self._unread = 0
        self._entries = []          # ordered [(kind, payload)] so filters can re-render
        self._filter_idx = 0
        self._theme = theme if theme in THEMES else DEFAULT_THEME
        self._theme_picker = None
        self._font_px = font_px or FONT_SIZE_PX
        self._font_family = _norm_family(font_family)
        self._gif_pending = set()    # GIF urls currently downloading
        self._gif_picker = None
        self._fill_gen = 0           # generation token so a stale incremental fill can be cancelled
        self._pending_older = []     # older history entries still to be streamed into the opened view
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
        # Noise filter: opt-out toggles to hide spammy server chat (voting, join/leave,
        # Discord ads, ...) plus your own starts/ends/contains string rules.
        self.noise_btn = QtWidgets.QPushButton("🧹")
        self.noise_btn.setToolTip("hide noisy server messages (voting, join/leave, Discord, custom…)")
        self.noise_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.noise_btn.setStyleSheet(
            "QPushButton{background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 6px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.noise_btn.clicked.connect(self._open_noise_panel)
        self._noise_panel = None
        self._update_noise_btn()
        header.addWidget(self.noise_btn)
        # Theme picker: restyle the whole chat (modern, medieval, futuristic, lofi, …).
        self.theme_btn = QtWidgets.QPushButton("🎨")
        self.theme_btn.setToolTip("chat theme — restyle colours, fonts, background & tags")
        self.theme_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setStyleSheet(
            "QPushButton{background:rgba(34,34,34,40);"
            "border:1px solid rgba(58,65,80,70); border-radius:4px; padding:2px 6px;}"
            " QPushButton:hover{background:rgba(44,49,60,150);}")
        self.theme_btn.clicked.connect(self._open_theme_picker)
        header.addWidget(self.theme_btn)
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
        # Scrollable full history -- shown only while OPENED (actively typing). A QScrollArea
        # holding a bottom-anchored column of per-message WIDGETS (not a QTextEdit): GIFs are
        # real QMovie labels that size correctly and never overlap the text, and long
        # transcripts scroll normally. Text messages are word-wrapped QLabels.
        self.view = QtWidgets.QScrollArea()
        self.view.setWidgetResizable(True)
        self.view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.view.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transcript = QtWidgets.QWidget()
        self._transcript.setObjectName("hx_transcript")   # so the shared bg targets ONLY this
        self._tlayout = QtWidgets.QVBoxLayout(self._transcript)
        self._tlayout.setContentsMargins(6, 4, 6, 4)
        self._tlayout.setSpacing(3)
        self._tlayout.addStretch(1)          # index 0: keeps messages hugging the bottom
        self.view.setWidget(self._transcript)
        body.addWidget(self.view, 1)
        # Follow new content to the bottom ONLY while the reader is already at the bottom (so
        # scrolling up to read history isn't yanked back). We scroll on rangeChanged -- it fires
        # exactly when the scroll range grows after a widget is added or a GIF finishes loading,
        # by which point the new maximum is known (a deferred setValue would land short).
        self._follow_bottom = True
        self.view.verticalScrollBar().rangeChanged.connect(self._follow_range)
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
        self._hud_panel.setObjectName("hx_hud_panel")   # so its border can't cascade to each line
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
        self.apply_theme(self._theme)            # palette + font + panel/input styling
        if font_family or font_px:               # an explicit/saved font overrides the theme's font
            if font_family:
                self._font_family = _norm_family(font_family)
            if font_px:
                self._font_px = font_px
            self._apply_font()

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

    def apply_theme(self, name: str) -> None:
        """Restyle the whole overlay to theme `name`: palette, panel background/border, compose box,
        font family+size, name-tag decoration + lock glyph. Re-renders the current view."""
        th = THEMES.get(name) or THEMES[DEFAULT_THEME]
        self._theme = name if name in THEMES else DEFAULT_THEME
        self._C_YOU, self._C_OTHER, self._C_TUNNEL = th["you"], th["other"], th["tunnel"]
        self._C_WHISPER, self._C_PARTY, self._C_EMOTE = th["whisper"], th["party"], th["emote"]
        self._C_DIM, self._C_SYS = th["dim"], th["sys"]
        self._th_panel, self._th_border, self._th_radius = th["panel"], th["border"], th["radius"]
        self._th_input, self._th_text = th["input"], th["text"]
        self._th_name, self._th_lock, self._th_accent = th["name"], th["lock"], th["accent"]
        self._font_family = _norm_family(th["font"])
        self._font_px = th["size"]
        self._apply_font()
        # ID selector (#hx_hud_panel) so the panel's border stays on the panel and does NOT cascade
        # to every fading line (a bare `border:` rule in Qt applies to all child widgets). Raw
        # theme border -> "none" themes (standard) get no border at all.
        self._hud_panel.setStyleSheet(
            f"#hx_hud_panel{{background:{self._th_panel}; border:{self._th_border};"
            f" border-radius:{self._th_radius}px;}}")
        self._TITLE_STYLE = f"color:{self._th_accent}; font-weight:bold;"
        self.title.setStyleSheet(self._TITLE_STYLE)
        if getattr(self, "_opened", None) is None:
            return                               # during __init__: styled, nothing to re-render yet
        if self._collapsed:
            self.title.setStyleSheet(self._pill_style())
        else:
            self._rebuild() if self._opened else self._render_passive()

    def _pill_style(self) -> str:
        return (f"color:{self._th_accent}; font-weight:bold; background:rgba(20,24,30,235);"
                f"border:1px solid {self._th_accent}; border-radius:6px; padding:4px 10px;")

    def _apply_font(self) -> None:
        """(Re)apply the current theme's font family + size + panel/input styling."""
        fam = _css_family(self._font_family)
        # ONE translucent rounded panel behind ALL messages (like the HUD's shared panel), not a
        # box per message. The bg MUST be set DIRECTLY on the transcript widget (that auto-enables
        # WA_StyledBackground so it actually paints -- a background in the QScrollArea's own
        # stylesheet targeting a child never paints). The transcript fills the viewport via
        # widgetResizable, the scroll area/viewport stay transparent, and message widgets paint none.
        edge = self._th_border if self._th_border != "none" else "1px solid rgba(120,130,150,55)"
        self.view.setStyleSheet("QScrollArea{border:none; background:transparent;}")
        self.view.viewport().setStyleSheet("background:transparent;")
        self._transcript.setStyleSheet(
            f"#hx_transcript{{background:{self._th_panel}; border:{self._th_border};"
            f" border-radius:{self._th_radius}px;}}")
        self.input.setStyleSheet(
            f"QLineEdit{{background:{self._th_input}; color:{self._th_text};"
            f"border:{edge}; border-radius:{self._th_radius}px; padding:5px;"
            f"font-family:{fam}; font-size:{self._font_px}px;}}")

    def set_font_family(self, name: str) -> bool:
        """Change the chat font family, re-render, and report whether `name` is an installed
        family (Qt still applies + falls back to a close match if not)."""
        self._font_family = _norm_family(name)
        self._apply_font()
        if not self._collapsed:
            self._rebuild() if self._opened else self._render_passive()   # re-render at new font
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
        if not self._collapsed:
            self._rebuild() if self._opened else self._render_passive()   # re-render at new size

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
    _ENTRIES_MAX = 2000     # cap stored history (bounds memory on a spammy server)
    _OPENED_MAX = 300       # opened view builds at most this many recent widgets (bounds the
    #                         per-open cost -- rebuilding a widget for EVERY line ever seen is
    #                         what made open/close get progressively slower)
    _IMMEDIATE = 30         # of those, render this many synchronously on open (fills the view);
    _FILL_BATCH = 50        # the rest stream in above, this many per 0ms tick, so typing is instant

    def _record(self, entry) -> None:
        """Append to the stored history, trimming the oldest beyond the cap."""
        self._entries.append(entry)
        if len(self._entries) > self._ENTRIES_MAX:
            del self._entries[:len(self._entries) - self._ENTRIES_MAX]

    def add_message(self, msg) -> None:
        """Store + render a chatframe.Msg, honoring the current display filter."""
        self._record(("msg", msg))
        if self._collapsed:
            pass                                 # pill: nothing to draw (unread badge below)
        elif self._opened:
            if self._passes(msg):
                self._append_widget(self._message_block(msg, GIF_MAX_OPENED, True))
        elif self._passes(msg):
            self._hud_add_content(self._message_block(msg, GIF_MAX_HUD, False))
        if not msg.is_self:
            self._note_activity()

    def _body_html(self, text: str) -> str:
        """Render a message body to HTML for the caption/text line: text runs are emoji-expanded
        + escaped; inline .gif/.webp URLs are DROPPED (the GIF renders as its own animated widget
        below the text), so the raw URL never shows as text."""
        out = []
        for kind, seg in _split_gifs(text):
            if kind == "text":
                out.append(html.escape(emoji_util.emojize(seg)).replace("\n", "<br>"))
        return "".join(out).strip()

    def _runs_html(self, runs, drop_gifs: bool = False, bold: bool = False) -> str:
        """Build HTML from the game's (text, '#rrggbb') colour segments, so a multi-colour name
        or message keeps its per-character colours. Each segment is emoji-expanded + escaped and
        wrapped in its own (brightened) colour; inline GIF URLs are dropped when `drop_gifs`."""
        out = []
        for text, color in runs:
            pieces = _split_gifs(text) if drop_gifs else (("text", text),)
            for kind, seg in pieces:
                if kind == "gif":
                    continue
                h = html.escape(emoji_util.emojize(seg)).replace("\n", "<br>")
                if not h:
                    continue
                c = _brighten(color) if color else ""
                out.append(f'<span style="color:{c}">{h}</span>' if c else h)
        s = "".join(out)
        return f"<b>{s}</b>" if (bold and s) else s

    def _format_message(self, msg) -> str:
        """Build the HTML caption/text line for a chatframe.Msg (inline GIF URLs dropped -- they
        become animated widgets alongside). No side effects."""
        name = html.escape(msg.sender)
        lock = self._th_lock if msg.is_tunnel else ""

        def deco(inner: str) -> str:                # wrap a player name in the theme's tag glyphs
            pre, suf = self._th_name
            if not pre and not suf:
                return inner
            a = self._th_accent
            return (f'<span style="color:{a}">{html.escape(pre)}</span>{inner}'
                    f'<span style="color:{a}">{html.escape(suf)}</span>')
        # BODY: reproduce the game's OWN per-run colours (multi-colour messages) when we have
        # them (non-tunnel); else emoji-expand our text and apply the single flat body colour.
        if not msg.is_tunnel and getattr(msg, "body_runs", None):
            body = self._runs_html(msg.body_runs, drop_gifs=True)
        else:
            body = self._body_html(msg.body)
            if not msg.is_self and msg.body_color and not msg.is_tunnel:
                body = f'<span style="color:{_brighten(msg.body_color)}">{body}</span>'
        # NAME single-colour fallback (used when the wire gave no per-run name segments).
        name_color = self._C_YOU if msg.is_self else (
            _brighten(msg.rank_color or msg.name_color)
            or (self._C_TUNNEL if msg.is_tunnel else self._C_OTHER))

        def colored_name() -> str:
            """The sender name with the game's per-character colours, else a flat-coloured name."""
            if not msg.is_self and getattr(msg, "name_runs", None):
                inner = self._runs_html(msg.name_runs, bold=True)
                if inner:
                    return inner
            return f'<span style="color:{name_color}"><b>{name}</b></span>'

        if msg.kind == "party":
            who = (f'<span style="color:{self._C_YOU}"><b>you</b></span>'
                   if msg.is_self else deco(colored_name()))
            line = (f'<span style="color:{self._C_PARTY}">{lock}[P] </span>'
                    f'{who}<span style="color:{self._C_DIM}">:</span> {body}')
        elif msg.kind == "emote":
            who = "you" if msg.is_self else name
            line = (f'<span style="color:{self._C_EMOTE}"><i>{lock}* '
                    f'{html.escape(who)} {self._body_html(msg.body)}</i></span>')
        elif msg.kind == "whisper_in":
            nm = deco(f'<span style="color:{self._C_WHISPER}"><b>{name}</b></span>')
            line = (f'<span style="color:{self._C_WHISPER}">{lock}</span>{nm}'
                    f'<span style="color:{self._C_DIM}"> whispers:</span> {body}')
        elif msg.kind == "whisper_out":
            tgt = html.escape(msg.target or self.recipient)
            line = (f'<span style="color:{self._C_DIM}">{lock}to </span>'
                    f'<span style="color:{self._C_WHISPER}"><b>{tgt}</b></span>'
                    f'<span style="color:{self._C_DIM}">:</span> {body}')
        elif msg.kind == "system":
            # Server/console lines are often multi-colour -> mirror their runs; grey fallback.
            line = (self._runs_html(msg.body_runs, drop_gifs=True) if getattr(msg, "body_runs", None)
                    else "") or f'<span style="color:{self._C_SYS}">{self._body_html(msg.body)}</span>'
        else:  # public
            who = (f'<span style="color:{name_color}"><b>{name}</b></span>'
                   if msg.is_self else deco(colored_name()))
            line = f'{lock}{who}<span style="color:{self._C_DIM}">:</span> {body}'
        return line

    def add_system(self, text: str) -> None:
        self._record(("sys", text))
        if self._collapsed:
            return
        if self._opened:
            self._append_widget(self._text_label(self._format_system(text), True))
        else:
            self._hud_add_content(self._text_label(self._format_system(text), False))

    def _format_system(self, text: str) -> str:
        return f'<span style="color:{self._C_SYS}">· {html.escape(text)}</span>'

    # ---- GIF rendering ----
    # A GIF is a direct .gif/.webp URL sitting INLINE in a message body. Both views render a
    # message as a WIDGET: its text (caption + any words, GIF URLs dropped) as a QLabel, plus one
    # animated QMovie label (_GifLabel) per inline GIF stacked below it. Real widgets size
    # themselves, so a GIF never overlaps the text, and it animates in the passive HUD too.

    def _ensure_gif(self, url: str) -> bool:
        """Return True if the GIF is already cached; else kick off a background download and
        return False (self._gif_ready fires with the url when it lands, so any _GifLabel waiting
        on it can swap the animation in)."""
        if not url or gif_util.is_cached(url):
            return bool(url) and gif_util.is_cached(url)
        if url not in self._gif_pending:
            self._gif_pending.add(url)

            def _work() -> None:
                gif_util.fetch(url)
                self._gif_pending.discard(url)
                self._gif_ready.emit(url)        # queued back to the Qt thread
            threading.Thread(target=_work, daemon=True).start()
        return False

    def _make_movie(self, url: str, max_px: int):
        """A (not-yet-started) aspect-scaled QMovie for a cached GIF, or None."""
        mv = QtGui.QMovie(str(gif_util.cache_path(url)))
        if not mv.isValid():
            return None
        mv.jumpToFrame(0)
        sz = mv.currentImage().size()
        if sz.width() > 0 and sz.height() > 0:
            mv.setScaledSize(sz.scaled(max_px, max_px,
                                       QtCore.Qt.AspectRatioMode.KeepAspectRatio))
        return mv

    def _text_label(self, html_text: str, selectable: bool) -> QtWidgets.QLabel:
        """A word-wrapped RichText QLabel for a caption/text/system line in the current font."""
        lbl = QtWidgets.QLabel(html_text)
        lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        sp = lbl.sizePolicy()                    # advertise height-for-width so wrapped lines
        sp.setHeightForWidth(True)               # get their full vertical space (no overlap)
        sp.setVerticalPolicy(QtWidgets.QSizePolicy.Policy.Minimum)
        lbl.setSizePolicy(sp)
        lbl.setStyleSheet(f"background:transparent; color:{self._th_text};"
                          f"font-family:{_css_family(self._font_family)}; font-size:{self._font_px}px;")
        if selectable:
            lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        else:                                    # passive HUD: let the game keep the mouse
            lbl.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        return lbl

    def _message_block(self, msg, gif_max: int, selectable: bool) -> QtWidgets.QWidget:
        """Build a message widget: the text/caption line, then an animated GIF label for each
        inline .gif/.webp URL in the body (capped to `gif_max`)."""
        text_lbl = self._text_label(self._format_message(msg), selectable)
        gif_urls = _GIF_URL_RE.findall(msg.body or "")
        if not gif_urls:
            return text_lbl                          # common case: a bare label, no wrapper (fast)
        w = QtWidgets.QWidget()
        w.setStyleSheet("background:transparent;")   # no per-message box; the shared panel shows
        if not selectable:
            w.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(text_lbl)
        for url in gif_urls:
            row = QtWidgets.QHBoxLayout()        # keep the GIF left-aligned, not stretched
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(_GifLabel(self, url, gif_max, self._font_px, self._font_family))
            row.addStretch(1)
            v.addLayout(row)
        return w

    def _stop_transcript_gifs(self) -> None:
        """Pause every opened-transcript GIF (called when the view is hidden -> saves CPU; the
        view is rebuilt, restarting them, when it's shown again)."""
        for gl in self._transcript.findChildren(_GifLabel):
            gl.stop()

    def _append_widget(self, w: QtWidgets.QWidget) -> None:
        """Append a message/system widget to the transcript, following the newest only if the
        reader is already at the bottom (else keep their scroll position)."""
        self._follow_bottom = self._at_bottom()  # applied by _follow_range when the range grows
        self._tlayout.addWidget(w)               # after the leading stretch -> bottom of the list

    def _follow_range(self, _lo: int, hi: int) -> None:
        if self._follow_bottom:
            self.view.verticalScrollBar().setValue(hi)

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
            # Focus the compose box FIRST so you can type instantly -- don't make typing wait on
            # rendering history. Then _rebuild() shows the last few lines immediately and streams
            # the older ones in over the next event-loop ticks.
            self.input.setFocus()
            self._rebuild()
        else:
            self._cancel_fill()                  # stop any in-flight incremental history fill
            self._stop_transcript_gifs()         # pause opened-view GIF animations (view hidden)
            self.input.discard()                 # wipe leftover unsent text on unfocus
            self._render_passive()

    def _sync_visibility(self) -> None:
        """Show exactly the widgets the current collapsed/opened state calls for.
        PASSIVE (expanded, game-focused) hides all chrome -> pure floating text."""
        if self._collapsed:
            self.header.setVisible(True)         # the pill itself lives in the header
            self.body.setVisible(False)
            self.arrow.setVisible(False)
            for b in (self.filter_btn, self.emoji_btn, self.gif_btn, self.noise_btn, self.theme_btn, self.friends_btn):
                b.setVisible(False)
            return
        chrome = self._opened
        self.header.setVisible(chrome)           # passive -> no header/buttons
        self.arrow.setVisible(chrome)
        for b in (self.filter_btn, self.emoji_btn, self.gif_btn, self.noise_btn, self.theme_btn, self.friends_btn):
            b.setVisible(chrome)
        self.body.setVisible(True)
        self.view.setVisible(self._opened)       # opened -> scrollable history
        self.hud.setVisible(not self._opened)    # passive -> fading HUD
        self.input.setVisible(self._opened)

    # ---- passive HUD (individually-fading lines) ----

    def _hud_add_content(self, content: QtWidgets.QWidget) -> None:
        """Wrap a content widget (text label or a message block with animated GIFs) in a
        self-fading HUD line: it lingers, then fades out on its own timer."""
        line = _FadingWrap(content, self._LINGER_MS, self._FADE_MS, self._hud_remove)
        self._hud_lines.append(line)
        self._hud_layout.addWidget(line)
        while len(self._hud_lines) > self._PASSIVE_MAX:
            self._hud_lines.pop(0).kill()
        self._hud_panel.setVisible(True)

    def _hud_remove(self, line) -> None:
        """A line finished fading (or is being culled): drop it from the HUD."""
        if line in self._hud_lines:
            self._hud_lines.remove(line)
        line.kill()
        if not self._hud_lines:                  # nothing left -> hide the shared panel
            self._hud_panel.setVisible(False)

    def _hud_clear(self) -> None:
        for line in self._hud_lines:
            line.kill()
        self._hud_lines = []
        self._hud_panel.setVisible(False)

    def _render_passive(self) -> None:
        """Reseed the HUD with the most recent visible lines (each starts its own fade)."""
        self._hud_clear()
        shown = [(k, p) for (k, p) in self._entries
                 if k == "sys" or self._passes(p)]
        for k, p in shown[-self._PASSIVE_MAX:]:
            if k == "sys":
                self._hud_add_content(self._text_label(self._format_system(p), False))
            else:
                self._hud_add_content(self._message_block(p, GIF_MAX_HUD, False))

    # ---- display filter (cycled by the header button) ----
    # (key, short label). 'all' = everything; 'private' = party + whispers only;
    # 'tunnel' = only encrypted (decrypted) messages.
    _FILTERS = (
        ("all", "all"),
        ("private", "party+dm"),
        ("tunnel", "encrypted"),
    )

    def _passes(self, msg) -> bool:
        # Opt-out noise filter: hide junk server/broadcast chat, but NEVER your own or encrypted
        # (tunnel) messages -- so a DM/party line can't be swallowed by a rule. body_runs carries
        # the per-run colours so a rule can match on colour (e.g. "[!]" whose "!" is orange).
        # Real player chat (anything but a 'system' line) is only ever touched by a player-affecting
        # category (welcome) -- so a player typing "vote now!" isn't hidden by the Voting filter.
        if not msg.is_self and not msg.is_tunnel:
            is_player = msg.kind != "system"
            if chatfilter.should_hide(msg.body, getattr(msg, "body_runs", None), is_player):
                return False
        key = self._FILTERS[self._filter_idx][0]
        if key == "private":
            return msg.kind in ("party", "whisper_in", "whisper_out")
        if key == "tunnel":
            return bool(msg.is_tunnel)
        return True

    def _refresh_view(self) -> None:
        """Re-render the current view (after a filter change)."""
        if not self._collapsed:
            self._rebuild() if self._opened else self._render_passive()

    def _cycle_filter(self) -> None:
        self._filter_idx = (self._filter_idx + 1) % len(self._FILTERS)
        self._update_filter_btn()
        if not self._collapsed:
            self._rebuild() if self._opened else self._render_passive()

    def _update_filter_btn(self) -> None:
        self.filter_btn.setText(self._FILTERS[self._filter_idx][1])

    def _build_entry(self, entry) -> QtWidgets.QWidget:
        kind, payload = entry
        if kind == "sys":
            return self._text_label(self._format_system(payload), True)
        return self._message_block(payload, GIF_MAX_OPENED, True)

    def _cancel_fill(self) -> None:
        """Abandon any pending incremental history fill (bumping the generation invalidates it)."""
        self._fill_gen += 1
        self._pending_older = []

    def _rebuild(self) -> None:
        """Re-render the transcript under the current filter, but INCREMENTALLY: build the last
        few lines synchronously (so the view is usable at once) and stream the older ones in over
        the next event-loop ticks. System/status lines always show; chat messages are filtered."""
        self._follow_bottom = True                   # a full rebuild ends scrolled to the newest
        self._cancel_fill()
        while self._tlayout.count() > 1:             # drop every widget except the leading stretch
            item = self._tlayout.takeAt(1)
            w = item.widget()
            if w is not None:
                w.deleteLater()                      # child QMovies stop when the label is destroyed
        # Only build the most recent _OPENED_MAX; older history stays in _entries (for re-renders).
        visible = [(k, p) for (k, p) in self._entries if k == "sys" or self._passes(p)]
        visible = visible[-self._OPENED_MAX:]
        immediate = visible[-self._IMMEDIATE:]       # shown right now (bottom of the view)
        self._pending_older = visible[:-self._IMMEDIATE]   # streamed in above, newest-first
        for entry in immediate:
            self._tlayout.addWidget(self._build_entry(entry))
        if self._pending_older:
            gen = self._fill_gen
            QtCore.QTimer.singleShot(0, lambda: self._fill_older(gen))

    def _fill_older(self, gen: int) -> None:
        """Insert a chunk of older history ABOVE what's shown (called off a 0ms timer so the UI
        stays responsive). Newest-of-older first, so scrolling up reveals lines as they fill."""
        if gen != self._fill_gen or not self._pending_older or not self._opened:
            return
        batch = self._pending_older[-self._FILL_BATCH:]
        del self._pending_older[-self._FILL_BATCH:]
        for entry in reversed(batch):                # insert each just under the stretch (index 1)
            self._tlayout.insertWidget(1, self._build_entry(entry))
        if self._pending_older:
            QtCore.QTimer.singleShot(0, lambda: self._fill_older(gen))

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

    # ---- noise filter (opt-out: hide spammy server chat) ----

    def _open_noise_panel(self) -> None:
        if self._noise_panel is None:
            self._noise_panel = NoiseFilterPanel(self, self._on_noise_changed)
        self._noise_panel.rebuild()
        self._noise_panel.popup(self.noise_btn)

    def _on_noise_changed(self) -> None:
        """A category/custom rule was toggled or edited -> re-render + refresh the badge."""
        self._update_noise_btn()
        self._refresh_view()

    def _update_noise_btn(self) -> None:
        """Show a dot on the broom when at least one filter is actively hiding messages."""
        self.noise_btn.setText("🧹●" if chatfilter.any_active() else "🧹")

    # ---- theme picker ----

    def _open_theme_picker(self) -> None:
        if self._theme_picker is None:
            self._theme_picker = ThemePicker(self)
        self._theme_picker.rebuild()
        self._theme_picker.popup(self.theme_btn)

    def set_theme(self, name: str) -> None:
        """Apply a theme AND persist it (the app saves it via theme_changed)."""
        if name not in THEMES:
            return
        self.apply_theme(name)
        self.theme_changed.emit(name)

    # ---- collapse / expand ----

    _PILL_STYLE = ("color:#8fd; font-weight:bold; background:rgba(20,24,30,235);"
                   "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;")
    _TITLE_STYLE = "color:#8fd; font-weight:bold;"

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self.title.setText(f"{self._th_lock}▸")
            self.title.setStyleSheet(self._pill_style())
            self._hud_clear()                    # stop any in-flight HUD fades
            self._cancel_fill()                  # stop any in-flight incremental history fill
            self._stop_transcript_gifs()         # pause opened-view GIF animations too
            self.input.discard()                 # wipe leftover unsent text
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
            self.title.setText(f"{self._th_lock}● {self._unread}")

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

    def _at_bottom(self) -> bool:
        bar = self.view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4       # already scrolled to the newest

    def prefill(self, text: str) -> None:
        """Put `text` in the compose box with the cursor at the end (used by the '/' quick-
        command hotkey). Only meaningful when opened; the input holds it either way."""
        self.input.setText(text)
        self.input.setCursorPosition(len(text))
        self.input.deselect()
        self.input.setFocus()

    def insert_gif(self, url: str) -> None:
        """Drop a compact '[GIF]' token (standing in for `url`) at the compose-box cursor. It
        mixes into any message and expands to the URL on send -- so a GIF works in a private
        '/msg <friend> hey [GIF]', a party '/p sup [GIF]', or a bare public line -- and renders
        inline as the animated GIF (a compact [GIF] in the passive HUD)."""
        self.input.insert_gif_token(url)
        self.input.setFocus()

    def _on_submit(self) -> None:
        text = self.input.expanded_text().strip()   # [GIF] tokens -> their real URLs
        if text:
            self.input.remember(text)           # add to the Up/Down recall history
            self.input.discard()                # clear text + the pending-GIF map
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
        self._gifs: list[str] = []      # URLs for the "[GIF]" tokens, in insertion order

    def insert_gif_token(self, url: str) -> None:
        """Insert a compact '[GIF]' placeholder at the cursor and remember its URL; expanded_text
        swaps the placeholders back to their URLs (left-to-right) when the line is sent."""
        self._gifs.append(url)
        self.insert(GIF_TOKEN)

    def expanded_text(self) -> str:
        """The compose text with each '[GIF]' placeholder replaced (in order) by its saved URL.
        A literally-typed '[GIF]' with no saved URL is left as-is (harmless plain text)."""
        pending = list(self._gifs)

        def _swap(_m):
            return pending.pop(0) if pending else _m.group(0)
        return _GIF_TOKEN_RE.sub(_swap, self.text())

    def remember(self, text: str) -> None:
        """Record a just-submitted line and reset the browse position to the draft."""
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._idx = None
        self._draft = ""

    def discard(self) -> None:
        """Wipe any unsent text + reset Up/Down browse state (on unfocus, so an old scramble
        isn't sitting there next time) + the pending-GIF map. History is kept."""
        self.clear()
        self._idx = None
        self._draft = ""
        self._gifs = []

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


class _GifLabel(QtWidgets.QLabel):
    """Animates a cached GIF via QMovie. Until its URL finishes downloading it shows a small
    'loading' note and swaps the animation in when the overlay's _gif_ready fires for that URL.
    A real widget, so it sizes itself and never overlaps the surrounding text."""

    def __init__(self, overlay, url: str, max_px: int, font_px: int, font_family: str) -> None:
        super().__init__()
        self._ov = overlay
        self._url = url
        self._max = max_px
        # Passive-HUD GIFs must let the game keep the mouse; opened-view ones don't need it either.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background:transparent; color:#7a828f;"
                           f"font-family:{_css_family(font_family)}; font-size:{font_px}px;")
        if gif_util.is_cached(url):
            self._load()
        else:
            self.setText("🎞️ loading GIF…")
            overlay._gif_ready.connect(self._on_ready)   # auto-disconnected if this label dies
            overlay._ensure_gif(url)

    def _on_ready(self, url: str) -> None:
        if url != self._url:
            return
        try:
            self._ov._gif_ready.disconnect(self._on_ready)
        except (TypeError, RuntimeError):
            pass
        self._load()

    def _load(self) -> None:
        mv = self._ov._make_movie(self._url, self._max)
        if mv is None:
            self.setText("⚠ GIF failed to load")
            return
        self.setText("")
        self.setMovie(mv)
        mv.start()

    def stop(self) -> None:
        mv = self.movie()
        if mv is not None:
            mv.stop()


class _FadingWrap(QtWidgets.QWidget):
    """One line in the passive HUD: holds a content widget (a text QLabel, or a message block
    with animated GIF labels), stays fully opaque for `linger_ms`, then fades to nothing over
    `fade_ms` and calls `on_dead(self)`. Each line runs its own timer so lines fade independently."""

    def __init__(self, content: QtWidgets.QWidget, linger_ms: int, fade_ms: int, on_dead) -> None:
        super().__init__()
        self._on_dead = on_dead
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background:transparent;")   # shared HUD panel shows, no per-line box
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(0)
        lay.addWidget(content)
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
        for gl in self.findChildren(_GifLabel):   # stop any animating GIFs on this line
            gl.stop()
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
    recents as thumbnails, click one to drop a '[GIF]' token into the compose box (mix it
    into any /msg, /p, or public line), or ✕ to delete it. No search service -- purely your
    own saved GIFs."""

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
        self._hint.setText("click a GIF → drops [GIF] in the compose box · ✕ deletes"
                           if urls else "no GIFs yet — paste a direct .gif URL above")

    def _tile(self, url: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        thumb = QtWidgets.QToolButton()
        thumb.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("put this GIF in the compose box")
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
        self._ov.insert_gif(url)          # into the compose box, not an instant send
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


class NoiseFilterPanel(QtWidgets.QWidget):
    """Popup 'hide noise' panel: opt-out checkboxes for spammy server-chat categories (voting,
    join/leave, deaths, Discord ads, ...) plus custom starts/ends/contains string rules. Edits
    go straight to chatfilter (persisted); `on_changed()` re-renders the transcript."""

    _MODE_LABELS = {"contains": "contains", "startswith": "starts with", "endswith": "ends with",
                    "number": "is a number"}

    def __init__(self, overlay, on_changed):
        super().__init__(overlay, QtCore.Qt.WindowType.Popup)
        self._on_changed = on_changed
        self.setStyleSheet("background:rgba(18,21,27,245);"
                           "border:1px solid #3a4150; border-radius:8px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QtWidgets.QLabel("Hide noisy messages  ·  everything shows unless ticked")
        head.setStyleSheet("color:#8fd; font-weight:bold; font-size:11px;")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._host = QtWidgets.QWidget()
        self.rows = QtWidgets.QVBoxLayout(self._host)
        self.rows.setSpacing(2)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.addStretch(1)
        self.scroll.setWidget(self._host)
        lay.addWidget(self.scroll, 1)

        add = QtWidgets.QHBoxLayout()
        add.setSpacing(4)
        self.custom_in = QtWidgets.QLineEdit()
        self.custom_in.setPlaceholderText("custom text to hide…")
        self.custom_in.setStyleSheet(
            "QLineEdit{background:rgba(10,12,16,220); color:#fff;"
            "border:1px solid #3a4150; border-radius:6px; padding:4px;}")
        self.custom_in.returnPressed.connect(self._add_custom)
        add.addWidget(self.custom_in, 1)
        self.mode_sel = QtWidgets.QComboBox()
        for m in chatfilter.MODES:
            self.mode_sel.addItem(self._MODE_LABELS[m], m)
        self.mode_sel.setStyleSheet(
            "QComboBox{color:#ddd; background:rgba(10,12,16,220);"
            "border:1px solid #3a4150; border-radius:6px; padding:3px 6px;}")
        add.addWidget(self.mode_sel)
        addbtn = QtWidgets.QPushButton("add")
        addbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        addbtn.setStyleSheet(
            "QPushButton{color:#9bf6a0; background:rgba(34,40,34,120);"
            "border:1px solid #3a4150; border-radius:6px; padding:4px 10px;}"
            " QPushButton:hover{background:rgba(44,60,44,180);}")
        addbtn.clicked.connect(self._add_custom)
        add.addWidget(addbtn)
        lay.addLayout(add)

        self.setFixedSize(320, 420)

    # ---- build ----

    def _clear_rows(self) -> None:
        while self.rows.count() > 1:                   # keep the trailing stretch
            item = self.rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def rebuild(self) -> None:
        self._clear_rows()
        idx = 0
        for cid, label, _pats in chatfilter.CATEGORIES:
            self.rows.insertWidget(idx, self._category_row(cid, label)); idx += 1
        custom = chatfilter.custom_rules()
        if custom:
            sep = QtWidgets.QLabel("custom rules")
            sep.setStyleSheet("color:#7a828f; font-size:10px; padding-top:4px;")
            self.rows.insertWidget(idx, sep); idx += 1
        for i, rule in enumerate(custom):
            self.rows.insertWidget(idx, self._custom_row(i, rule)); idx += 1

    def _checkbox(self, text: str, checked: bool) -> QtWidgets.QCheckBox:
        cb = QtWidgets.QCheckBox(text)
        cb.setChecked(checked)
        cb.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cb.setStyleSheet("QCheckBox{color:#ddd; padding:2px;} "
                         "QCheckBox::indicator{width:14px; height:14px;}")
        return cb

    def _category_row(self, cid: str, label: str) -> QtWidgets.QWidget:
        cb = self._checkbox(label, chatfilter.category_hidden(cid))
        cb.setToolTip(f"hide {label} messages")
        cb.toggled.connect(lambda on, c=cid: self._set_cat(c, on))
        return cb

    def _custom_row(self, index: int, rule: dict) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0); h.setSpacing(4)
        cb = self._checkbox("", rule["on"])
        cb.toggled.connect(lambda on, i=index: self._toggle_custom(i, on))
        h.addWidget(cb)
        # Delete (✕) + mode first, pinned to the right at their natural size, so a long rule text
        # can never push them off the (fixed-width) panel. The text label then WRAPS into whatever
        # width is left instead of overflowing horizontally (there's no horizontal scrollbar).
        rm = QtWidgets.QPushButton("✕")
        rm.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        rm.setToolTip("delete this rule")
        rm.setStyleSheet("QPushButton{color:#ff8f8f; background:transparent; border:none;"
                         "padding:2px 6px;} QPushButton:hover{color:#fff;}")
        rm.clicked.connect(lambda _=False, i=index: self._remove_custom(i))
        mode = QtWidgets.QLabel(self._MODE_LABELS[rule["mode"]])
        mode.setStyleSheet("color:#7a828f; font-size:10px;")
        lbl = QtWidgets.QLabel(f'“{html.escape(rule["text"])}”')
        lbl.setStyleSheet("color:#e6e6e6;")
        lbl.setToolTip(f'{rule["text"]} · {self._MODE_LABELS[rule["mode"]]}')
        lbl.setWordWrap(True)                        # long rules wrap instead of overflowing
        sp = lbl.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Ignored)   # take given width, don't demand its own
        sp.setHeightForWidth(True)
        lbl.setSizePolicy(sp)
        h.addWidget(lbl, 1)
        h.addWidget(mode)
        h.addWidget(rm)
        h.setAlignment(rm, QtCore.Qt.AlignmentFlag.AlignTop)
        h.setAlignment(mode, QtCore.Qt.AlignmentFlag.AlignTop)
        return row

    # ---- actions (persist via chatfilter, then re-render) ----

    def _set_cat(self, cid: str, on: bool) -> None:
        chatfilter.set_category(cid, on)
        self._on_changed()

    def _toggle_custom(self, index: int, on: bool) -> None:
        chatfilter.toggle_custom(index, on)
        self._on_changed()

    def _remove_custom(self, index: int) -> None:
        chatfilter.remove_custom(index)
        self.rebuild()
        self._on_changed()

    def _add_custom(self) -> None:
        text = self.custom_in.text().strip()
        if not text:
            return
        if chatfilter.add_custom(text, self.mode_sel.currentData()):
            self.custom_in.clear()
            self.rebuild()
            self._on_changed()

    def popup(self, anchor) -> None:
        gp = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        self.move(gp)
        self.show()
        self.raise_()


class ThemePicker(QtWidgets.QWidget):
    """Popup list of chat themes. Each entry is a live preview: it is styled in that theme's own
    panel background, accent colour, border, font, lock glyph and name-tag, so you see the look
    before you pick it. Clicking applies + persists the theme."""

    def __init__(self, overlay):
        super().__init__(overlay, QtCore.Qt.WindowType.Popup)
        self._ov = overlay
        self.setStyleSheet("background:rgba(18,21,27,248);"
                           "border:1px solid #3a4150; border-radius:8px;")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        head = QtWidgets.QLabel("Chat theme")
        head.setStyleSheet("color:#8fd; font-weight:bold; font-size:12px;")
        lay.addWidget(head)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._host = QtWidgets.QWidget()
        self.rows = QtWidgets.QVBoxLayout(self._host)
        self.rows.setSpacing(5)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.addStretch(1)
        self.scroll.setWidget(self._host)
        lay.addWidget(self.scroll, 1)
        self.setFixedSize(300, 420)

    def _clear(self) -> None:
        while self.rows.count() > 1:                    # keep the trailing stretch
            item = self.rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def rebuild(self) -> None:
        self._clear()
        cur = self._ov._theme
        for i, name in enumerate(THEME_ORDER):
            self.rows.insertWidget(i, self._card(name, THEMES[name], name == cur))

    def _card(self, name: str, th: dict, current: bool) -> QtWidgets.QWidget:
        pre, suf = th["name"]
        tick = "✓ " if current else ""
        btn = QtWidgets.QPushButton(f"  {tick}{th['lock']}{pre}{th['label']}{suf}")
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        edge = th["border"] if th["border"] != "none" else "1px solid rgba(120,130,150,60)"
        btn.setStyleSheet(
            f"QPushButton{{text-align:left; background:{th['panel']}; color:{th['accent']};"
            f"border:{edge}; border-radius:{th['radius']}px; padding:9px 11px; font-weight:bold;"
            f"font-family:{_css_family(_norm_family(th['font']))}; font-size:14px;}}"
            f"QPushButton:hover{{border:2px solid {th['accent']};}}")
        btn.clicked.connect(lambda _=False, n=name: self._pick(n))
        return btn

    def _pick(self, name: str) -> None:
        self._ov.set_theme(name)
        self.rebuild()                                  # move the ✓ to the new selection
        self.close()

    def popup(self, anchor) -> None:
        gp = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        self.move(gp)
        self.show()
        self.raise_()
