"""Linux receive backend: full chat mirror via the quiche QUIC stream hook.

Spawns the root capture helper (eBPF on quiche_conn_stream_recv), which emits each
chat-log frame as ``C <hex>``. We parse the frame (chatframe), classify it
(player vs server/system), decrypt any embedded tunnel token, attribute the real
sender from the frame, and hand a ready-to-display Msg to the overlay.

Because every rendered line -- ours included -- passes through here in the server's
canonical order, the overlay's order matches the in-game chat exactly.

Privilege: the capture needs root, launched via pkexec (GUI prompt) or sudo. The
overlay stays a normal user process; only the eBPF capture is elevated, and it
handles no keys (tokens are ciphertext that was already on the wire).
"""

import os
import shutil
import subprocess
import sys

from . import chatframe, crypto

_CAPTURE = os.path.join(os.path.dirname(__file__), "quiche_capture.py")

# Which classified kinds count as "a player said something" (everything else is
# server/console noise that floods the chat -- filtered unless show_system=True).
_PLAYER_KINDS = {"public", "party", "whisper_in", "whisper_out", "emote"}


def _elevate_cmd() -> list[str]:
    py = sys.executable or "python3"
    # Pass our PID so the elevated capture self-terminates (and kills bpftrace) when
    # the overlay exits, instead of orphaning a root probe.
    me = str(os.getpid())
    if os.geteuid() == 0:                     # already root
        return [py, _CAPTURE, me]
    if shutil.which("pkexec"):                # GUI password prompt (best for the overlay)
        return ["pkexec", py, _CAPTURE, me]
    if shutil.which("sudo"):
        return ["sudo", py, _CAPTURE, me]
    return [py, _CAPTURE, me]


_MAX_FRAME = 1 << 20        # sanity cap; a real chat frame is a few KB


def _hold_split_frame(raw: bytes) -> bytes:
    """If the LAST d2 chat frame in `raw` is incomplete -- its declared length runs past the
    end of the buffer -- return that trailing fragment so the caller can stitch it onto the
    next stream_recv read; else b''.

    A frame is [u32 LE body_len][u32 type d2..][body], so it starts 4 bytes before the d2
    signature and spans 8 + body_len bytes. quiche_conn_stream_recv delivers the QUIC byte
    stream in chunks that don't respect these frame boundaries, so a long line (a big GIF
    chunk, the 20-name player list) routinely straddles two reads -- and parsing each read
    in isolation dropped or mangled it. Stitching makes the receive reliable regardless of
    how the stream was chunked.
    """
    last = None
    for m in chatframe._FAMILY_SIG.finditer(raw):
        last = m.start()
    if last is None or last < 4:
        return b""
    fstart = last - 4
    blen = int.from_bytes(raw[fstart:fstart + 4], "little")
    if blen <= 0 or blen > _MAX_FRAME:
        return b""                          # implausible length -> don't hold (avoid desync)
    if fstart + 8 + blen > len(raw):        # frame extends past this buffer -> incomplete
        return raw[fstart:]
    return b""


def _rescue_party(cl: chatframe.ChatLine, keyed) -> chatframe.ChatLine:
    """Rescue an encrypted party/chat line we didn't classify as a player line.

    Party-chat formats can vary, so a genuine encrypted party message may land as an
    unclassified 'system' line and get dropped. If such a line carries a token that
    decrypts with one of our keys (self-authenticating proof it's real and for us),
    re-tag it as a 'party' message with a best-effort sender so it still surfaces.
    Returns the (possibly re-tagged) ChatLine unchanged when no rescue applies.
    """
    if cl.kind in _PLAYER_KINDS:
        return cl
    m = chatframe.HX_TOKEN_RE.search(cl.full)
    if not m or crypto.try_decrypt_sym(m.group(0)[3:], keyed) is None:
        return cl
    cl.kind = "party"
    cl.sender = chatframe.sender_before_token(cl.full, m.start())
    cl.body = m.group(0)          # _build_msg will locate + decrypt this token
    return cl


def _build_msg(cl: chatframe.ChatLine, my_name: str | None,
               reasm: crypto.Reassembler, keyed) -> chatframe.Msg | None:
    """Turn a parsed line into a display Msg, decrypting an embedded token if any.

    Returns None when the line is one chunk of a not-yet-complete tunnel message.
    """
    # whisper_out has no sender in the frame ("[To X] ..."); it's always us.
    is_self = cl.kind == "whisper_out" or (bool(my_name) and cl.sender == my_name)
    sender = "you" if is_self else (cl.sender or cl.target or "?")
    colors = dict(rank=cl.rank, rank_color=cl.rank_color,
                  name_color=cl.name_color, body_color=cl.body_color,
                  name_runs=cl.name_runs, body_runs=cl.body_runs)

    m = chatframe.HX_TOKEN_RE.search(cl.body)
    if not m:                                   # plain chat -> show as-is
        return chatframe.Msg(sender=sender, body=cl.body, kind=cl.kind,
                             is_self=is_self, is_tunnel=False, target=cl.target, **colors)

    token = m.group(0)
    marker, body64 = token[:3], token[3:]
    dec = crypto.try_decrypt_sym(body64, keyed)
    if dec is None:
        # A real encrypted message, but not addressed to us (someone else's key /
        # no key). In a full mirror we still show the line; we just can't read it.
        return chatframe.Msg(sender=sender, body=cl.body, kind=cl.kind,
                             is_self=is_self, is_tunnel=False, target=cl.target, **colors)
    key_name, payload = dec
    # Reassemble chunks keyed on the actual sender from the frame -- for a party group
    # everyone shares ONE key, so keying on key_name would mix concurrent senders'
    # chunks; the frame sender (or key_name for whispers) keeps them separate.
    reasm_id = cl.sender or cl.target or key_name
    out = reasm.add_decrypted(reasm_id, marker, payload)
    if out is None:                             # a chunk; wait for the rest
        return None
    # The decrypted body is our own plaintext, not a game-rendered run -> no game
    # body colour for it (rank/name colour from the wire still applies).
    plain = out[1]
    return chatframe.Msg(sender=sender, body=plain, kind=cl.kind,
                         is_self=is_self, is_tunnel=True, target=cl.target,
                         rank=cl.rank, rank_color=cl.rank_color, name_color=cl.name_color,
                         name_runs=cl.name_runs)   # sender-name colours apply; decrypted body has none


def watch(on_message, stop=None, on_ready=None, proc_holder=None,
          my_name=None, show_system=False, tunnel_only=False, debug_log=None):
    """Run the capture; call on_message(Msg) per displayable message.

    on_message receives a chatframe.Msg. `my_name` makes our own lines render as
    'you'. System/console lines are dropped unless show_system. tunnel_only keeps
    only encrypted tunnel messages (the classic private-channel behaviour).
    `debug_log` (a path) records every parsed line + its disposition for diagnosing
    dropped messages.
    """
    dbg = None
    if debug_log:
        try:
            dbg = open(debug_log, "a", buffering=1)
        except OSError:
            dbg = None

    def _log(*parts):
        if dbg:
            dbg.write("\t".join(str(p) for p in parts) + "\n")
    try:
        proc = subprocess.Popen(_elevate_cmd(), stdout=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as e:                     # noqa: BLE001
        if on_ready:
            on_ready()
        on_message(chatframe.Msg("·", f"capture failed to start: {e}", "system"))
        return
    if proc_holder is not None:
        proc_holder.append(proc)
    if on_ready:
        on_ready()
    reasm = crypto.Reassembler()
    carry = b""                              # held tail of a chat frame split across reads
    try:
        for line in proc.stdout:
            if stop is not None and stop.is_set():
                break
            line = line.strip()
            if not line.startswith("C "):
                continue
            try:
                raw = bytes.fromhex(line[2:])
            except ValueError:
                continue
            # Stitch a chat frame that straddled two stream_recv reads: prepend any held
            # tail, then hold back this buffer's own trailing incomplete frame (if any) for
            # the next read. Bounded by _MAX_FRAME so a lost read can't grow carry forever.
            if carry:
                raw = carry + raw
                carry = b""
            carry = _hold_split_frame(raw)
            if carry:
                raw = raw[:len(raw) - len(carry)]
                if len(carry) > _MAX_FRAME:  # frame never completed (e.g. a dropped read)
                    carry = b""
            # One stream-0 buffer can pack several chat lines at various offsets.
            lines = chatframe.parse_all(raw)
            if dbg:
                import re as _re
                nsig = len(chatframe._SIG_RE.findall(raw))
                ascii_preview = _re.sub(rb"[^\x20-\x7e]", b".", raw).decode()
                _log("BUF", f"n={len(raw)}", f"sigs={nsig}",
                     f"lines={len(lines)}", ascii_preview[:1500])
                # TRUNCATION CHECK: each d2 frame is [u32 LE body_len][u32 type d2..][body].
                # The frame starts 4 bytes before the signature; if its declared extent runs
                # past the captured buffer, the eBPF window clipped its tail -> lost/short line.
                for _m in chatframe._SIG_RE.finditer(raw):
                    fstart = _m.start() - 4
                    if fstart < 0:
                        continue
                    blen = int.from_bytes(raw[fstart:fstart + 4], "little")
                    fend = fstart + 8 + blen
                    if 0 < blen < 1_000_000 and fend > len(raw):
                        _log("TRUNC", f"fstart={fstart}", f"declared_body={blen}",
                             f"frame_end={fend}", f"buf={len(raw)}", f"short_by={fend - len(raw)}")
                # PARTYHEX: full buffer hex whenever a party line — or any non-player line that
                # still carries an HX token (the suspected duplicate "notification" frame) — is
                # present, so we can see exactly how the notification frame differs from the chat.
                if any(cl.kind == "party" or
                       (cl.kind not in _PLAYER_KINDS and chatframe.HX_TOKEN_RE.search(cl.full))
                       for cl in lines):
                    _log("PARTYHEX", f"kinds={[cl.kind for cl in lines]}", raw.hex())
                # Capture hex of CHAT-LIKE buffers we currently MISS (a colour run and a
                # "name: " colon present, but no d2 signature) to learn the alt format.
                if nsig == 0 and b"\x07#" in raw and b": " in raw:
                    _log("MISSHEX", raw.hex())
                # Full hex of Discord-relay buffers, so a capture pins the exact framing of the
                # (uncoloured) message text if it still shows empty.
                if b"[Discord]" in raw:
                    _log("DISCHEX", raw.hex())
            keyed = crypto.all_decrypt_keys()      # friend + party keys (re-read each buffer)
            for cl in lines:
                # Rescue an encrypted party message hiding in an unclassified line before
                # the system-drop filter can discard it.
                cl = _rescue_party(cl, keyed)
                if cl.kind not in _PLAYER_KINDS and not show_system:
                    _log("DROP-system", cl.kind, repr(cl.full[:80]))
                    continue
                msg = _build_msg(cl, my_name, reasm, keyed)
                if msg is None:
                    _log("HOLD-chunk", cl.kind, repr(cl.body[:40]))
                    continue
                if tunnel_only and not msg.is_tunnel:
                    _log("DROP-tunnelonly", cl.kind, repr(msg.body[:40]))
                    continue
                # Log the per-run colour segments too (text + '#rrggbb') so a capture reveals the
                # exact colours of markers like "[!]" -- what colour chat-filter rules need.
                _runs = [(t[:16], c) for t, c in (msg.body_runs or [])][:10]
                _log("EMIT", msg.kind, msg.sender, repr(msg.body[:80]),
                     "full=" + repr(cl.full[:120]), "runs=" + repr(_runs))
                on_message(msg)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else None
    watch(lambda m: print(f"[{m.kind}] {m.sender}{' 🔒' if m.is_tunnel else ''}: {m.body}"),
          my_name=name, show_system=True)
