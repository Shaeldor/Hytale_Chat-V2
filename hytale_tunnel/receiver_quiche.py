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
                  name_color=cl.name_color, body_color=cl.body_color)

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
    is_gif = plain.startswith(chatframe.GIF_SENTINEL)
    gif_url = plain[len(chatframe.GIF_SENTINEL):].strip() if is_gif else ""
    return chatframe.Msg(sender=sender, body=plain, kind=cl.kind,
                         is_self=is_self, is_tunnel=True, target=cl.target,
                         rank=cl.rank, rank_color=cl.rank_color, name_color=cl.name_color,
                         is_gif=is_gif, gif_url=gif_url)


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
            # One stream-0 buffer can pack several chat lines at various offsets.
            lines = chatframe.parse_all(raw)
            if dbg:
                import re as _re
                nsig = len(chatframe._SIG_RE.findall(raw))
                ascii_preview = _re.sub(rb"[^\x20-\x7e]", b".", raw).decode()
                _log("BUF", f"n={len(raw)}", f"sigs={nsig}",
                     f"lines={len(lines)}", ascii_preview[:1500])
                # Capture hex of CHAT-LIKE buffers we currently MISS (a colour run and a
                # "name: " colon present, but no d2 signature) to learn the alt format.
                if nsig == 0 and b"\x07#" in raw and b": " in raw:
                    _log("MISSHEX", raw.hex())
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
                _log("EMIT", msg.kind, msg.sender, repr(msg.body[:80]))
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
