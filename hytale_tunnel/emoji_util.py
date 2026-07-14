"""Emoji support for the overlay: text shortcodes -> glyphs, plus a picker source list.

Hytale's own chat can't render emoji, but our Qt overlay can. We keep compact ASCII
shortcodes on the encrypted wire and expand them to glyphs only when RENDERING (display
side), so the payload stays small and emoticons other players type render too.

Uses the `emoji` package for the full ``:name:`` set. It degrades gracefully: if the
package isn't installed, shortcodes are simply shown as-is and the picker inserts raw
glyphs, so the overlay never breaks (handy before a friend has `pip install emoji`'d).
    Install:  pacman -S python-emoji   (Arch/CachyOS)  |  pip install emoji  (Windows)
"""

import re

try:
    import emoji as _LIB
except Exception:                                 # noqa: BLE001 - optional dependency
    _LIB = None


def available() -> bool:
    """True if the full :name: emoji database is present."""
    return _LIB is not None


# Classic emoticons the :name: database doesn't cover. Matched only as WHITESPACE-BOUNDED
# tokens (see _EMOTICON_RE) so they never fire inside a word, a time like "12:30", or a
# ``:name:`` code like ":partying_face:". Expanded on RAW text (before HTML escaping) so
# "<3" is matched before "<" would become "&lt;".
_EMOTICONS = {
    ":-)": "🙂", ":)": "🙂", ":-D": "😄", ":D": "😄", ";-)": "😉", ";)": "😉",
    ":-(": "🙁", ":(": "🙁", ":-P": "😛", ":P": "😛", ":p": "😛",
    "xD": "😆", "XD": "😆", "<3": "❤️", ":'(": "😢", ":o": "😮", ":O": "😮",
    ":|": "😐", ":/": "😕", ":3": "😺",
}
# Longest keys first so ":-)" wins over ":)"; bounded by non-whitespace lookarounds so the
# token must stand alone (preceded/followed by a space or the string edge).
_EMOTICON_RE = re.compile(
    r"(?<!\S)(" + "|".join(re.escape(k) for k in sorted(_EMOTICONS, key=len, reverse=True))
    + r")(?!\S)")


def emojize(text: str) -> str:
    """Turn text shortcodes into emoji glyphs. Safe no-op for unmatched text / missing lib.

    ``:name:`` codes are resolved first by the emoji lib (its own strict parser), then the
    whitespace-bounded emoticons — so neither can corrupt the other.
    """
    if not text:
        return text
    if _LIB is not None:
        try:
            text = _LIB.emojize(text, language="alias")
        except Exception:                         # noqa: BLE001 - never break rendering
            pass
    return _EMOTICON_RE.sub(lambda m: _EMOTICONS[m.group(1)], text)


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
