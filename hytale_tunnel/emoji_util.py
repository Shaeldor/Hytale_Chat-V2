"""Emoji support for the overlay: text shortcodes -> glyphs, plus a picker source list.

Hytale's own chat can't render emoji, but our Qt overlay can. We keep compact ASCII
shortcodes on the encrypted wire and expand them to glyphs only when RENDERING (display
side), so the payload stays small and shortcodes other players type render too.

Only explicit ``:name:`` codes (e.g. ``:sob:``, ``:fire:``) are expanded — NOT smiley
emoticons like ``:P``/``xD``/``<3``, which would fire on ordinary text. Uses the `emoji`
package for the full ``:name:`` set. It degrades gracefully: if the package isn't
installed, shortcodes are simply shown as-is and the picker inserts raw glyphs, so the
overlay never breaks (handy before a friend has `pip install emoji`'d).
    Install:  pacman -S python-emoji   (Arch/CachyOS)  |  pip install emoji  (Windows)
"""

try:
    import emoji as _LIB
except Exception:                                 # noqa: BLE001 - optional dependency
    _LIB = None


def available() -> bool:
    """True if the full :name: emoji database is present."""
    return _LIB is not None


def emojize(text: str) -> str:
    """Expand ``:name:`` shortcodes to emoji glyphs. Safe no-op for text with no codes,
    or when the emoji lib is absent. Smiley emoticons (``:P``, ``xD``, ``<3`` …) are
    deliberately NOT expanded — only explicit colon-delimited names like ``:sob:``."""
    if not text or _LIB is None:
        return text
    try:
        return _LIB.emojize(text, language="alias")
    except Exception:                             # noqa: BLE001 - never break rendering
        return text


def to_shortcode(ch: str) -> str:
    """Canonical ``:name:`` for an emoji glyph (what the picker inserts), else the glyph."""
    if _LIB is not None:
        try:
            sc = _LIB.demojize(ch, language="alias")
            if sc and sc != ch:
                return sc
        except Exception:                         # noqa: BLE001
            pass
    return ch


def search(query: str, limit: int = 120) -> list[str]:
    """Emoji glyphs whose alias/name contains `query` (needs the lib; [] without it)."""
    q = query.strip().lower().strip(":")
    if not q or _LIB is None:
        return []
    out: list[str] = []
    for ch, d in _LIB.EMOJI_DATA.items():
        names = [d.get("en", "")]
        alias = d.get("alias")
        if alias:
            names += alias
        if any(q in n.lower() for n in names):
            out.append(ch)
            if len(out) >= limit:
                break
    return out


# Curated, ordered set of popular emoji for the picker grid (typing supports the full lib
# set; this is just the default browse view). Grouped loosely: faces, gestures, hearts,
# people/activity, nature, food, objects, symbols/flags.
PICKER_EMOJI = [
    "😀", "😁", "😂", "🤣", "😊", "😇", "🙂", "😉", "😍", "🥰", "😘", "😜", "😝", "🤪",
    "🤨", "😎", "🤩", "🥳", "😏", "😒", "😞", "😔", "😟", "😕", "🙃", "😬", "😱", "😨",
    "😅", "😓", "😤", "😡", "🤬", "🤯", "😳", "🥺", "😢", "😭", "😴", "🤤", "😷", "🤒",
    "🤕", "🤢", "🤮", "🥴", "😵", "🤠", "🤡", "🤫", "🤔", "🤗", "🙄", "😐", "😶",
    "👍", "👎", "👌", "🤌", "✌️", "🤞", "🤟", "🤘", "👏", "🙌", "👐", "🙏", "🤝", "💪",
    "👋", "🖐️", "✋", "👊", "🤙", "👀", "👉", "👈", "☝️", "🫡",
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "💔", "❣️", "💕", "💘", "💯",
    "🔥", "✨", "⭐", "🌟", "💫", "⚡", "💥", "💦", "💨", "🎉", "🎊", "🏆", "🥇", "🎯",
    "🎮", "🕹️", "⚔️", "🛡️", "🗡️", "🏹", "💣", "💀", "☠️", "👑", "💎", "🔑", "🔒",
    "🌍", "🌙", "☀️", "🌈", "🍀", "🌸", "🐺", "🐉", "🦴",
    "🍕", "🍔", "🍟", "🌮", "🍺", "🍻", "☕", "🍎", "🍄",
    "✅", "❌", "⚠️", "❓", "❗", "💤", "💬", "👆", "🔊", "📌", "🏴", "🏳️",
]
