"""Opt-out noise filter for the transcript.

By default every message shows. This lets you HIDE categories of spammy server/broadcast
chat (voting nags, rules, guides, join/leave, Discord ads, ...) and add your own string
rules (starts-with / ends-with / contains). Pure logic + JSON persistence (no Qt import), so
it is unit-testable; the overlay applies it display-side only. It never touches the wire, and
the overlay never hides YOUR OWN or ENCRYPTED (tunnel) messages -- only plain public/server
lines -- so this can't swallow a private DM or party message.

Storage (alongside the other ~/.hytalecrypt files):
    chatfilter.json  {"categories": {id: true}, "custom": [{"text","mode","on"}]}
"""
import colorsys
import json
import re

from . import crypto

# A number token (optional sign / decimal / thousands / %), e.g. "-298.0", "52.6", "1,024", "+25.3".
# The "number" filter mode hides stray HUD/combat numbers that leak onto the chat stream but aren't
# in the real in-game chat. Its "submode" (the pattern's 4th element) says WHERE the number sits:
#   whole (default) = the ENTIRE message is the number; startswith = the message BEGINS with a number
#   then more text ("+25.3 Combat XP"); endswith = ends with one; contains = has one anywhere.
_NUM_TOKEN = r"[+-]?\d[\d.,]*%?"
_NUM_RE = re.compile(r"^" + _NUM_TOKEN + r"$")           # whole-message number (kept for reuse)
_NUM_ANCHORS = {
    "whole":      re.compile(r"^(" + _NUM_TOKEN + r")$"),
    "startswith": re.compile(r"^(" + _NUM_TOKEN + r")"),
    "endswith":   re.compile(r"(" + _NUM_TOKEN + r")$"),
    "contains":   re.compile(r"(" + _NUM_TOKEN + r")"),
}

FILTER_JSON = crypto.CONFIG_DIR / "chatfilter.json"

# Built-in categories: (id, label, [patterns]).
# Each PATTERN is case-insensitive and is one of:
#     "substring"                       -> matches if the message CONTAINS it (anywhere) [default]
#     ("text", "startswith")            -> matches only if the message STARTS WITH "text"
#     ("text", "endswith")              -> matches only if the message ENDS WITH "text"
#     ("text", "contains")              -> explicit contains (same as a bare string)
#     ("text", "startswith", "orange")  -> ALSO requires a coloured run of that colour in the line
#     ("", "contains", "red")           -> colour-ONLY: any line containing a red run
#     ("", "number")                    -> the whole message is JUST a number (-298.0, 52.6, 100%)
#     ("", "number", "cyan")            -> a number-only message coloured cyan (safe HUD-number filter)
#     ("+", "number", "orange")         -> the WHOLE message is a number that starts with "+", orange
#     ("+", "number", "orange", "startswith") -> the message BEGINS with a "+"-number then more text
#                                          ("+25.3 Combat XP"), orange. The 4th "submode"
#                                          (startswith/endswith/contains) says WHERE the number sits;
#                                          default (3-tuple) = the whole message is the number. `text`
#                                          stays a required prefix of that number (e.g. "+", "-").
# The "number" mode hides stray HUD/combat numbers that leak onto the chat stream but aren't shown
# in the real in-game chat. It's safe: like every category (except those in PLAYER_CATEGORIES) it
# only touches SERVER/system lines, so a player who literally types "52.6" is never hidden.
# COLOUR is a name (red / orange / yellow / green / cyan / blue / purple / pink) or a "#rrggbb"
# hex. Use it to split lines that share a prefix by the colour of a marker -- e.g. "[!]" alerts
# whose "!" is yellow vs orange vs red go into different categories. A category hides a line if
# ANY of its patterns match; when a pattern has both text and colour, BOTH must hold. Patterns
# are heuristics (server formats vary) -- tighten, recolour, disable, or use a 🧹 custom rule.
#
# KNOWN "[!]" MARKER COLOURS on this server (from a debug capture) -- use these hex values to
# split the "[!]" alert lines by the colour of the "!":
#     "#ff5555"  light red  -> voting ("<player> just voted ... /vote"), announcements ("Clearing up fog!")
#     "#ffff55"  yellow     -> tips / rules / guides (numbered rules, /guide, /shop, tractor tips)
#     "#ffaa00"  orange     -> chat games ("A new game has started!", "won the game! (#1)")
#   (a 4th darker/richer red "!" for alerts wasn't captured yet -- grab its hex when you see one.)
# e.g.  ("[!]", "startswith", "#ff5555")  hides only the light-red voting-style "[!]" lines.
CATEGORIES = [
    ("voting",    "Voting",        [("just voted and supported the server! vote now using /vote", "contains"), 
                                    ("vote now using /vote", "contains"), ("vote party—", "startswith"), ("Vote Streak —", "startswith"), 
                                    ("vote party!", "endswith"), ("vote! everyone online receives", "contains"), ("we reached 100 votes!", "endswith"), 
                                    ("all online players receive", "startswith"), ("support the server by voting:/vote", "startswith"), 
                                    ("you haven't voted yet!use /vote", "endswith"), ("you have free rewards yet to be claimed!", "contains")]),
    ("rules",     "Sys Info",      [("[!]", "startswith", "#ffff55")]),
    ("pie",       "/Pie",          [("/pie", "contains"), ("boss has spawned:", "contains"), ("was defeated! rewards paid to all who struck it.", "endswith"), 
                                    ("wandered off - nobody joined the raid.", "endswith"), ("fled before it could be defeated...", "endswith"), ("fled...not enough damage dealt in time.", "endswith")]),
    ("welcome",   "Welcome",       [("welcome", "endswith"), ("has joined histatu for the first time! welcome!", "endswith")]),
    ("joinleave", "Join / Leave",  [("[+]", "startswith"), ("[-]", "endswith")]),
    ("discord",   "Discord",       [("[Discord]", "startswith")]),
    ("death",     "Death",         [("was killed by", "contains")]),
    ("chatgames", "Chat Games",    [("[!]", "startswith", "#ffaa00"), ("[!] a new game has started!", "contains"), ("[!] no one answered correctly!", "endswith")]),
    ("keys",      "Key Drops",     [("[keys received]", "startswith"), ("community-wide key giveaway complete!", "startswith"), ("[key distribution]", "startswith")]),
    ("console",   "Console Cmds",  [("[!] console activated", "startswith"), ("[!] clearing up fog! enjoy the sun!", "endswith"), ("[timer]", "startswith"),
                                    ("saving chunks & data. expect a quick lag spike!", "contains"), ("chunk saving complete!", "contains"), ("Hourly bonus:", "startswith"),
                                    ("You received $5,000.", "startswith")]),
    ("tractor",   "Tractor",       [("tractor", "contains"), ("/farm", "contains")]),
    ("building",  "Building Event",[("histatu skyblock build event", "contains"), ("histatu build event", "startswith")]),
    ("minigames", "Mini-Games",    [("[tnt-run]", "startswith"), ("[dac]", "startswith"), ("[tnt-tag]", "startswith"), ("[blockhunt]", "startswith"), ("[block-party]", "startswith"), ("[murder-mystery]", "startswith")]),
    ("cleanup",   "Clean Up",      [("[!] Server cleanup in 2s...", "startswith"), ("[!] Server cleanup in 1s...", "startswith"), ("[!] Clearing dropped items and hostile entities...", "startswith")],),
    ("dungeons",  "Dungeons",      [("reached Ascension", "contains"), ("histatu dungeon world", "contains"), ("the dungeon.", "endswith"), ("left the dungeon", "contains"), ("entered the dungeon", "contains")]),
    ("invisible", "Invisible Info",[("mmoskilltree.skill", "contains"), ("better crates/lootbox", "contains"), ("", "number", "cyan"), ("", "number", "green"), ("+", "number", "orange", "startswith"), ("", "number", "blue")]),
]

_CAT_PATTERNS = {cid: pats for cid, _label, pats in CATEGORIES}
_CAT_IDS = {cid for cid, _l, _p in CATEGORIES}

# Categories listed here ALSO filter real PLAYER chat; every OTHER category -- and ALL custom
# rules -- only ever hide NON-player (server/system) lines. So a player who types "vote now!" is
# never hidden by the Voting filter; only server broadcasts are. Add category ids here if you
# want them to affect player chat too.
PLAYER_CATEGORIES = {"welcome"}

MODES = ("contains", "startswith", "endswith", "number")

_cache = None            # in-memory copy so should_hide() doesn't read the file per message


def _read() -> dict:
    try:
        d = json.loads(FILTER_JSON.read_text())
        if isinstance(d, dict):
            cats = d.get("categories") if isinstance(d.get("categories"), dict) else {}
            custom = d.get("custom") if isinstance(d.get("custom"), list) else []
            return {"categories": {k: bool(v) for k, v in cats.items() if k in _CAT_IDS},
                    "custom": [_clean_rule(r) for r in custom if _clean_rule(r)]}
    except (OSError, ValueError):
        pass
    return {"categories": {}, "custom": []}


def _clean_rule(r) -> dict | None:
    if not isinstance(r, dict):
        return None
    text = (r.get("text") or "").strip()
    if not text:
        return None
    mode = r.get("mode") if r.get("mode") in MODES else "contains"
    return {"text": text, "mode": mode, "on": bool(r.get("on", True))}


def load() -> dict:
    global _cache
    if _cache is None:
        _cache = _read()
    return _cache


def _save() -> None:
    try:
        crypto.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        FILTER_JSON.write_text(json.dumps(_cache))
    except OSError:
        pass


def reload() -> None:
    """Drop the in-memory cache (re-read on next use)."""
    global _cache
    _cache = None


# --------------------------------------------------------------------------- categories

def category_hidden(cid: str) -> bool:
    return bool(load()["categories"].get(cid))


def set_category(cid: str, hidden: bool) -> None:
    if cid not in _CAT_IDS:
        return
    load()["categories"][cid] = bool(hidden)
    _save()


# --------------------------------------------------------------------------- custom rules

def custom_rules() -> list:
    return list(load()["custom"])


def add_custom(text: str, mode: str = "contains") -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if mode not in MODES:
        mode = "contains"
    d = load()
    if any(r["text"].lower() == text.lower() and r["mode"] == mode for r in d["custom"]):
        return False                                   # already present
    d["custom"].append({"text": text, "mode": mode, "on": True})
    _save()
    return True


def toggle_custom(index: int, on: bool) -> None:
    d = load()
    if 0 <= index < len(d["custom"]):
        d["custom"][index]["on"] = bool(on)
        _save()


def remove_custom(index: int) -> None:
    d = load()
    if 0 <= index < len(d["custom"]):
        del d["custom"][index]
        _save()


# --------------------------------------------------------------------------- matching

def any_active() -> bool:
    """True if at least one category or custom rule is currently hiding messages (for a badge)."""
    d = load()
    return any(d["categories"].values()) or any(r["on"] for r in d["custom"])


_TEXT_MODES = ("startswith", "endswith", "contains")


def _pattern_parts(pat) -> tuple:
    """Normalize a pattern to (lowercased text, mode, colour, submode). Forms:
        "substring"                          -> (text, 'contains', '', 'startswith')
        ("text", "startswith")               -> (text, mode, '', ...)
        ("text", "startswith", "red")        -> also require a run of that COLOUR
        ("+", "number", "orange", "startswith") -> a NUMBER, orange, whose text also matches `text`
                                                   under the 4th "submode" (startswith/endswith/contains)
        {"text":.., "mode":.., "color":.., "submode":..}  -> dict form (any field optional)
    `submode` only applies to "number" mode (how the `text` prefix/suffix is matched; default
    startswith). `colour` is a name (red/orange/yellow/green/cyan/blue/purple/pink) or a '#rrggbb' hex."""
    if isinstance(pat, dict):
        text = str(pat.get("text", "")).lower()
        mode = pat.get("mode") if pat.get("mode") in MODES else "contains"
        color = str(pat.get("color", "")).strip().lower()
        submode = pat.get("submode") if pat.get("submode") in _TEXT_MODES else "whole"
        return text, mode, color, submode
    if isinstance(pat, (tuple, list)):
        text = str(pat[0]).lower() if len(pat) > 0 else ""
        mode = pat[1] if len(pat) > 1 and pat[1] in MODES else "contains"
        color = str(pat[2]).strip().lower() if len(pat) > 2 else ""
        submode = pat[3] if len(pat) > 3 and pat[3] in _TEXT_MODES else "whole"
        return text, mode, color, submode
    return str(pat).lower(), "contains", "", "whole"


def _matches(text: str, mode: str, t: str, submode: str = "whole") -> bool:
    """Does the (already-lowercased) message `t` match `text` under `mode`?"""
    if mode == "number":                               # a number, positioned per `submode`:
        m = _NUM_ANCHORS.get(submode, _NUM_ANCHORS["whole"]).search(t.strip())
        if not m:                                      #   whole msg / at start / at end / anywhere
            return False
        return not text or m.group(1).startswith(text)  # `text` = required prefix of the number ("+")
    if not text:
        return False
    if mode == "startswith":
        return t.startswith(text)
    if mode == "endswith":
        return t.endswith(text)
    return text in t                                   # contains (default)


# --- colour matching (for grouping e.g. "[!]" lines by the colour of the "!") ------------
# Server messages colour a marker (like "!") by severity; we bucket a run's hue to a name so a
# rule can say ("[!]", "startswith", "orange"). Runs come from the wire as '#rrggbb'.
_HUE_RANGES = [("red", 0, 15), ("orange", 15, 45), ("yellow", 45, 70), ("green", 70, 160),
               ("cyan", 160, 200), ("blue", 200, 255), ("purple", 255, 300), ("pink", 300, 345),
               ("red", 345, 360)]
_COLOR_ALIASES = {"grey": "gray", "gold": "yellow", "amber": "orange", "teal": "cyan",
                  "magenta": "pink", "violet": "purple", "lime": "green"}


def _rgb(hexcolor: str):
    h = (hexcolor or "").lstrip("#").strip()
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _color_name(rgb) -> str:
    """Coarse colour name of an (r,g,b), or 'gray' for near-grey/black/white."""
    r, g, b = (c / 255 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.25 or v < 0.20:
        return "gray"
    deg = h * 360
    for name, lo, hi in _HUE_RANGES:
        if lo <= deg < hi:
            return name
    return "red"


def _color_matches(run_color: str, spec: str) -> bool:
    """Does a run's '#rrggbb' colour match a spec (a colour NAME or a '#rrggbb' hex)?"""
    rc = _rgb(run_color)
    if rc is None:
        return False
    if spec.startswith("#"):                            # hex spec: tolerant RGB proximity
        sc = _rgb(spec)
        return sc is not None and sum((a - b) ** 2 for a, b in zip(rc, sc)) <= 72 ** 2
    return _color_name(rc) == _COLOR_ALIASES.get(spec, spec)


def _region_has_color(runs, start: int, end: int, spec: str) -> bool:
    """Does any run OVERLAPPING [start, end) of the concatenated text match colour `spec`? This
    checks the colour WHERE the pattern text matched (e.g. the "!" in "[!]"), not anywhere in the
    line -- crucial because e.g. yellow tip lines also contain red command-name runs elsewhere."""
    cum = 0
    for txt, c in (runs or []):
        rs, re_ = cum, cum + len(txt)
        cum = re_
        if rs < end and re_ > start and _color_matches(c, spec):
            return True
    return False


def _match_region(text: str, mode: str, raw_lower: str):
    """Where `text` matched in `raw_lower` (offsets aligned with the runs), or None for a
    colour-only / number pattern (colour may be anywhere in the line)."""
    if not text or mode == "number":                   # a number line is one colour -> anywhere
        return None
    if mode == "startswith":
        i = raw_lower.find(text)
    elif mode == "endswith":
        i = raw_lower.rfind(text)
    else:
        i = raw_lower.find(text)
    return (i, i + len(text)) if i >= 0 else (0, len(raw_lower))


def _pattern_hit(pat, t: str, raw_lower: str, runs) -> bool:
    """A pattern matches if its TEXT condition (if any) holds AND its COLOUR condition (if any)
    holds -- the colour being checked in the region where the text matched (or anywhere, for a
    colour-only pattern). An empty pattern never matches."""
    text, mode, color, submode = _pattern_parts(pat)
    has_text_cond = bool(text) or mode == "number"     # "number" needs no text string
    if not has_text_cond and not color:
        return False
    if has_text_cond and not _matches(text, mode, t, submode):
        return False
    if color:
        region = _match_region(text, mode, raw_lower)  # None for number/no-text -> colour anywhere
        if region is None:                             # colour-only: any run may carry it
            if not any(_color_matches(c, color) for _txt, c in (runs or [])):
                return False
        elif not _region_has_color(runs, region[0], region[1], color):
            return False
    return True


def should_hide(text: str, runs=None, is_player: bool = False) -> bool:
    """True if the message matches an ENABLED category or custom rule. `runs` is the message's
    [(text, '#rrggbb'), …] colour segments (needed only for colour rules). `is_player` is True
    for real player chat (public/party/whisper/emote) -- those are only ever hidden by a category
    in PLAYER_CATEGORIES (e.g. 'welcome'), never by other categories or by custom rules."""
    raw_lower = (text or "").lower()                   # unstripped -> offsets align with `runs`
    t = raw_lower.strip()
    if not t and not runs:
        return False
    d = load()
    for cid, on in d["categories"].items():
        if not on:
            continue
        if is_player and cid not in PLAYER_CATEGORIES:
            continue                                   # this category doesn't touch player chat
        if any(_pattern_hit(pat, t, raw_lower, runs) for pat in _CAT_PATTERNS.get(cid, ())):
            return True
    if not is_player:                                  # custom rules never touch player chat
        for r in d["custom"]:
            if r["on"] and _pattern_hit(r, t, raw_lower, runs):
                return True
    return False
