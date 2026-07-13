"""Parse & classify Hytale chat-log frames (the data behind quiche_conn_stream_recv).

Cross-platform, pure logic (no I/O), so it can be unit-tested against captured
frames and reused by any capture backend (Linux quiche hook today; a Windows
libquiche.dll hook later).

WIRE FORMAT (discovered by recon, 2026-06-29)
---------------------------------------------
A rendered chat line arrives as ONE stream frame. Header bytes 4..10 are the
chat-log message type signature ``d2 00 00 00 01 00 40`` (the capture filters to
this so non-chat stream traffic never reaches us). The body is a sequence of
"rich-text runs", each a 7-bit-length-prefixed UTF-8 string immediately followed
by ``\\x07`` + a 7-bit-length-prefixed colour string like ``#ffffff``. The
displayed line is the concatenation of all run texts, e.g.::

    "[42968] Shaeldor" + ": " + "hello"   ->  "[42968] Shaeldor: hello"

CATEGORIES (from real traffic)
    public   : "[<int>] [<rank>]? <name>: <msg>"   <- a player talking
    whisper  : "[To <name>] <msg>" (outgoing) / "[From <name>] <msg>" (incoming)
    emote    : "* <name> <action>"                  (/me; player-driven)
    system   : everything else -> "[!] ...", "[Duel] ...", "[+] <join>",
               event banners, errors. These are the server/console spam.
"""

import re
from dataclasses import dataclass

# Chat-log message type signature: header[4]==0xd2, header[8]==0x01, header[10]==0x40.
TYPE_OFF, TYPE_B = 4, 0xD2
SUB1_OFF, SUB1_B = 8, 0x01
SUB2_OFF, SUB2_B = 10, 0x40

_RUN = re.compile(rb"\x07#([0-9a-fA-F]{6})")

# An encrypted tunnel token embedded in a message body (single 'HX1' or chunk 'HX2').
HX_TOKEN_RE = re.compile(r"HX[12][A-Za-z0-9+/=]{20,}")

# Line classifiers (applied to the reconstructed text).
_RE_WHISPER_OUT = re.compile(r"^\[To (\S+)\] (.*)$", re.DOTALL)
_RE_WHISPER_IN = re.compile(r"^\[From (\S+)\] (.*)$", re.DOTALL)
# Group 1 = rank tag if present (e.g. "Legend"), group 2 = name, group 3 = body.
_RE_PUBLIC = re.compile(
    r"^\[\d+\]\s+(?:\[([^\]]+)\]\s+)?([A-Za-z0-9_]{2,20}):\s(.*)$", re.DOTALL)
# Party chat, e.g. "[!] [Party] <name>: <msg>". The game prefixes party lines with the
# same "[!]" marker it uses for console output, and may add other bracket tags (a
# player id, etc.), so we allow ANY run of leading "[...]" tags before the "[Party]"
# tag, then an optional [<rank>] tag. Group 1 = rank (if any), 2 = name, 3 = body.
_RE_PARTY = re.compile(
    r"^(?:\[[^\]]*\]\s+)*?\[Party\]\s+(?:\[([^\]]+)\]\s+)?([A-Za-z0-9_]{2,20}):\s(.*)$",
    re.DOTALL)
_RE_EMOTE = re.compile(r"^\* ([A-Za-z0-9_]{2,20}) (.*)$", re.DOTALL)

# Best-effort "<Name>: " sender extraction, used to attribute a party/chat line whose
# exact prefix format we don't classify but that carries a decryptable tunnel token.
_RE_SENDER_BEFORE = re.compile(r"([A-Za-z0-9_]{2,20}):\s")


def sender_before_token(full: str, before: int) -> str:
    """The last '<Name>: ' that appears in `full` before offset `before` ('' if none).

    Lets us name the author of an encrypted party message even when its surrounding
    chat format isn't one we classify -- the token decrypting is what proves it's real,
    this just recovers who said it."""
    best = ""
    for m in _RE_SENDER_BEFORE.finditer(full, 0, max(0, before)):
        best = m.group(1)
    return best


def _color_at(runs: list[tuple[str, str]], pos: int) -> str:
    """Colour (as '#rrggbb') of the run covering offset `pos` in the concatenated
    run text, or '' if out of range. Lets us reuse the game's OWN per-run colours
    (rank tag, name, message body are each independently coloured runs) instead of
    guessing a rank->colour table."""
    if pos < 0:
        return ""
    cum = 0
    for text, color in runs:
        end = cum + len(text)
        if cum <= pos < end:
            return "#" + color
        cum = end
    return ""


# The chat-log message header signature, locatable at ANY offset within a QUIC
# stream-0 buffer (the game packs several framed messages into one stream_recv read,
# so a chat line is often NOT at the start -- searching for this is what stops
# packed chat messages from being silently dropped).
SIG = bytes([TYPE_B, 0, 0, 0, SUB1_B, 0, SUB2_B])   # d2 00 00 00 01 00 40
_SIG_RE = re.compile(re.escape(SIG))


def is_chat_frame(raw: bytes) -> bool:
    """True if `raw` begins with a chat-log frame at the legacy offset 4 layout."""
    return (len(raw) > SUB2_OFF
            and raw[TYPE_OFF] == TYPE_B
            and raw[SUB1_OFF] == SUB1_B
            and raw[SUB2_OFF] == SUB2_B)


_FF8 = b"\xff" * 8   # the 8x0xff anchor that immediately precedes each run's textfield


def _strip_len_prefix(tf: bytes) -> bytes:
    """A run's textfield is a C#-style 7-bit-length-prefixed string: a varint byte
    count (1 byte for <128, 2 bytes up to 16383) then the UTF-8 text. Strip it."""
    n = shift = i = 0
    while i < len(tf):
        b = tf[i]
        n |= (b & 0x7F) << shift
        i += 1
        if b < 0x80:
            break
        shift += 7
        if shift > 21:
            return tf            # not a sane varint; return as-is
    text = tf[i:]
    # Trust the count when it matches (it always should); otherwise best-effort.
    return text if n == len(text) else text


def parse_runs(raw: bytes) -> list[tuple[str, str]]:
    """Extract (text, colour) runs from a chat-log frame, in order.

    Each run is laid out as ``<4-byte LE textfield-length><8x 0xff><textfield>`` then
    a colour tag ``\\x07#rrggbb``. We anchor on the (unambiguous) colour tags, then
    take the textfield as the bytes between the nearest preceding ``8x 0xff`` anchor
    and the tag -- validated against the 4-byte length. This is exact for runs of any
    size (earlier code walked backwards for a 1-byte length, which mis-matched short
    tails of long runs -> 'missing the first words' and dropped >=128-byte runs).
    """
    runs: list[tuple[str, str]] = []
    for m in _RUN.finditer(raw):
        color = m.group(1).decode()
        end = m.start()
        a = raw.rfind(_FF8, 0, end)      # text never contains 0xff, so this is exact
        if a < 0:
            continue
        ts = a + 8
        textfield = raw[ts:end]
        if a >= 4:                       # cross-check with the declared length
            declared = int.from_bytes(raw[a - 4:a], "little")
            if declared != len(textfield) and 0 < declared <= end:
                textfield = raw[end - declared:end]
        text = _strip_len_prefix(textfield)
        runs.append((text.decode("utf-8", "replace"), color))
    return runs


@dataclass
class ChatLine:
    kind: str           # 'public' | 'party' | 'whisper_in' | 'whisper_out' | 'emote' | 'system'
    sender: str         # display name of who sent it ('' for system)
    body: str           # message text (no name/prefix); for system = whole line
    full: str           # the full reconstructed line
    target: str = ""    # whisper recipient (whisper_out only)
    rank: str = ""          # rank tag, e.g. "Legend" ('' if the line had none)
    rank_color: str = ""    # game's own colour for the rank tag ('' if unknown)
    name_color: str = ""    # game's own colour for the sender name ('' if unknown)
    body_color: str = ""    # game's own colour for the message body ('' if unknown)

    @property
    def is_player(self) -> bool:
        return self.kind != "system"


def classify(full: str, runs: list[tuple[str, str]] | None = None) -> ChatLine:
    """Turn a reconstructed line into a ChatLine (sender/body/kind).

    When `runs` (the per-run (text, colour) list the line was built from) is given,
    also pull the game's own colours for the rank/name/body spans so the overlay can
    mirror in-game rank/message colouring exactly instead of guessing.
    """
    runs = runs or []

    def color_for(m, group) -> str:
        return _color_at(runs, m.start(group)) if m.group(group) else ""

    m = _RE_WHISPER_OUT.match(full)
    if m:
        return ChatLine("whisper_out", "", m.group(2), full, target=m.group(1),
                         body_color=color_for(m, 2))
    m = _RE_WHISPER_IN.match(full)
    if m:
        return ChatLine("whisper_in", m.group(1), m.group(2), full,
                         name_color=color_for(m, 1), body_color=color_for(m, 2))
    m = _RE_PARTY.match(full)
    if m:
        return ChatLine("party", m.group(2), m.group(3), full,
                         rank=m.group(1) or "", rank_color=color_for(m, 1),
                         name_color=color_for(m, 2), body_color=color_for(m, 3))
    m = _RE_PUBLIC.match(full)
    if m:
        return ChatLine("public", m.group(2), m.group(3), full,
                         rank=m.group(1) or "", rank_color=color_for(m, 1),
                         name_color=color_for(m, 2), body_color=color_for(m, 3))
    m = _RE_EMOTE.match(full)
    if m:
        return ChatLine("emote", m.group(1), m.group(2), full,
                         name_color=color_for(m, 1), body_color=color_for(m, 2))
    return ChatLine("system", "", full, full)


def parse(raw: bytes) -> ChatLine | None:
    """Parse a single chat-log frame assumed to start at the offset-4 layout."""
    if not is_chat_frame(raw):
        return None
    runs = parse_runs(raw)
    if not runs:
        return None
    full = "".join(t for t, _ in runs)
    return classify(full, runs)


def parse_all(raw: bytes) -> list[ChatLine]:
    """Extract EVERY chat line packed into one QUIC stream-0 buffer.

    The game coalesces multiple framed messages into a single stream_recv read, so a
    chat line can sit at any offset (and several can share a buffer). We locate each
    chat-log signature and parse the run region between it and the next one. Segments
    that hold no rich-text runs (a spurious signature match in binary/compressed data)
    are skipped.
    """
    offsets = [m.start() for m in _SIG_RE.finditer(raw)]
    lines: list[ChatLine] = []
    if not offsets:
        # No d2 chat-log signature in this buffer -- but some chat lines arrive in a
        # variant framing WITHOUT the d2 header (they still carry \x07#colour runs).
        # Previously these were silently dropped (the "MISSHEX" case); recover the line
        # from its runs so every message is displayed.
        runs = parse_runs(raw)
        if runs:
            lines.append(classify("".join(t for t, _ in runs), runs))
        return lines
    for i, off in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < len(offsets) else len(raw)
        runs = parse_runs(raw[off:end])
        if not runs:
            continue
        full = "".join(t for t, _ in runs)
        lines.append(classify(full, runs))
    return lines


@dataclass
class Msg:
    """A message ready for display (after classification + any decryption)."""
    sender: str         # who to show as the author ('you' handled via is_self)
    body: str           # text to display (decrypted if is_tunnel)
    kind: str           # public | party | whisper_in | whisper_out | emote | system
    is_self: bool = False     # we sent it -> render as 'you'
    is_tunnel: bool = False   # was an encrypted tunnel token -> show the lock
    target: str = ""          # whisper recipient (whisper_out)
    rank: str = ""            # rank tag, e.g. "Legend" ('' if none)
    rank_color: str = ""      # game's own colour for the rank tag ('' if unknown)
    name_color: str = ""      # game's own colour for the sender name ('' if unknown)
    body_color: str = ""      # game's own colour for the message body ('' if unknown)
