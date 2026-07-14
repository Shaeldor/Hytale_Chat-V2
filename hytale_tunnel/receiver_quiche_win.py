"""Windows receive backend: flawless, via the quiche QUIC stream hook (Frida).

The Windows-native equivalent of the Linux eBPF capture. Instead of bpftrace's
uprobe on quiche_conn_stream_recv in libquiche.so, this uses Frida to attach to the
running HytaleClient and Interceptor-hook the same exported function in quiche.DLL.
Every time the client pulls decrypted application data off a QUIC stream, we send
that buffer to Python to parse for chat frames.

Like the Linux path it sees only true *incoming* network data, so the client's local
echo of your own /msg is never picked up here (it never crosses the wire). No memory
scanning, no GC race. Frida attaches as the same user -- no admin needed for the
HytaleClient we target.

Requires `pip install frida` (bundled by setup-windows.bat). If frida or quiche.DLL
isn't present, app.py falls back to the memory scanner.
"""

import threading
import time

from . import chatframe, crypto, memio

# The exact C export both platforms hook: ssize_t quiche_conn_stream_recv(conn,
# stream_id, out, buf_len, fin[, out_error_code]). We read `out` (3rd arg) on entry
# and the returned length on exit, then send the buffer slice across the bridge.
_AGENT = r"""
function resolve(name) {
  for (const m of Process.enumerateModules()) {
    let e = null;
    try { e = m.findExportByName ? m.findExportByName(name) : null; } catch (_) {}
    if (!e) { try { e = m.getExportByName(name); } catch (_) {} }
    if (e) return { addr: e, mod: m.name };
  }
  return null;
}
const MAX = 1 << 16;               // max 64 KiB per stream read
const r = resolve('quiche_conn_stream_recv');
const r_send = resolve('quiche_conn_send');
const r_stream_send = resolve('quiche_conn_stream_send');

let active_conn = null;

if (!r) { send({t: 'noexport'}); }
else {
  send({t: 'ready', mod: r.mod, addr: r.addr.toString()});
  
  if (r_send) {
    Interceptor.attach(r_send.addr, {
      onEnter(a) { active_conn = a[0]; }
    });
  }
  
  if (r_stream_send) {
    const stream_send_func = new NativeFunction(r_stream_send.addr, 'ssize_t', ['pointer', 'uint64', 'pointer', 'size_t', 'bool', 'pointer']);
    rpc.exports = {
      injectChatFrame: function(frameBytes) {
        if (!active_conn || active_conn.isNull()) return -1;
        const buf = Memory.alloc(frameBytes.length);
        buf.writeByteArray(frameBytes);
        const res = stream_send_func(active_conn, 0, buf, frameBytes.length, 0, NULL);
        return parseInt(res.toString());
      }
    };
  }

  Interceptor.attach(r.addr, {
    onEnter(a) { this.out = a[2]; },
    onLeave(rv) {
      const n = rv.toInt32();
      if (n <= 0 || !this.out || this.out.isNull()) return;
      const ab = this.out.readByteArray(n < MAX ? n : MAX);
      send({t: 'buf'}, ab);
    }
  });
}
"""

# Tiny probe: resolve the hooked export across all loaded modules and report which
# module holds it. Run via a Frida script (JS-side Process.enumerateModules) on
# purpose -- Frida 17 dropped the Python Session.enumerate_modules(), so the old
# check raised AttributeError and silently disabled the fast path, sending everyone
# back to the slow memory scanner. JS enumeration still works and finds the export
# whether quiche ships as a separate DLL or is statically linked into the client.
_PROBE = r"""
function resolve(name){
  for (const m of Process.enumerateModules()){
    let e = null;
    try { e = m.findExportByName ? m.findExportByName(name) : null; } catch (_) {}
    if (!e) { try { e = m.getExportByName(name); } catch (_) {} }
    if (e) return m.name;
  }
  return null;
}
send({mod: resolve('quiche_conn_stream_recv')});
"""

def _export_module(pid: int):
    """Module name exporting quiche_conn_stream_recv in `pid`, or None if absent."""
    import frida

    proc = frida.attach(pid)
    box = {"mod": None}
    done = threading.Event()

    def on_msg(message, _data):
        if message.get("type") == "send":
            box["mod"] = (message.get("payload") or {}).get("mod")
        done.set()

    try:
        script = proc.create_script(_PROBE)
        script.on("message", on_msg)
        script.load()
        done.wait(3.0)
        return box["mod"]
    finally:
        try:
            proc.detach()
        except Exception:
            pass

def available() -> bool:
    """True if we can run the quiche hook: frida importable and the export resolvable."""
    try:
        import frida  # noqa: F401
    except Exception:
        return False
    pid = memio.find_client_pid()
    if pid is None:
        return True            # client not up yet; watch() will wait and retry
    try:
        mod = _export_module(pid)
        if mod:
            print(f"[quiche] hook available: quiche_conn_stream_recv in {mod}", flush=True)
            return True
        print("[quiche] quiche_conn_stream_recv not found in client modules", flush=True)
        return False
    except Exception as e:     # surface the reason; don't silently fall back
        print(f"[quiche] availability probe failed: {e!r}", flush=True)
        return False

class _SessionHandle:
    """Adapter so app.py's proc_holder cleanup (`p.terminate()`) detaches Frida."""

    def __init__(self):
        self.session = None
        self.script = None

    def terminate(self):
        for obj, meth in ((self.script, "unload"), (self.session, "detach")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass

_shared_handle = None

def inject_frame(frame_bytes: bytes) -> bool:
    """Inject a raw chat frame directly into the QUIC stream via the active Frida script."""
    if not _shared_handle or not _shared_handle.script:
        return False
    try:
        res = _shared_handle.script.exports_sync.inject_chat_frame(list(frame_bytes))
        return int(res) >= 0
    except Exception as e:
        print(f"[quiche] inject_frame error: {e}")
        return False

_PLAYER_KINDS = {"public", "whisper_in", "whisper_out", "emote", "party"}

def _build_msg(cl: chatframe.ChatLine, my_name: str | None,
               reasm: crypto.Reassembler) -> chatframe.Msg | None:
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
    dec = crypto.try_decrypt_sym(body64, crypto.loaded_psks())
    if dec is None:
        # A real encrypted message, but not addressed to us (someone else's key /
        # no key). In a full mirror we still show the line; we just can't read it.
        return chatframe.Msg(sender=sender, body=cl.body, kind=cl.kind,
                             is_self=is_self, is_tunnel=False, target=cl.target, **colors)
    key_name, payload = dec
    # Reassemble chunks keyed on the friend's key (each friend = a distinct key).
    out = reasm.add_decrypted(key_name, marker, payload)
    if out is None:                             # a chunk; wait for the rest
        return None
    # The decrypted body is our own plaintext, not a game-rendered run -> no game
    # body colour for it (rank/name colour from the wire still applies).
    plain = out[1]
    is_gif = False
    gif_url = ""
    
    if plain.startswith(chatframe.GIF_SENTINEL):
        is_gif = True
        gif_url = plain[len(chatframe.GIF_SENTINEL):].strip()
    elif plain.startswith("HXG1"):
        is_gif = True
        gif_url = plain[len("HXG1"):].strip()
    elif "http" in plain and (".gif" in plain.lower() or ".webp" in plain.lower()):
        is_gif = True
        gif_url = plain.strip()
        
    return chatframe.Msg(sender=sender, body=plain, kind=cl.kind,
                         is_self=is_self, is_tunnel=True, target=cl.target,
                         rank=cl.rank, rank_color=cl.rank_color, name_color=cl.name_color,
                         is_gif=is_gif, gif_url=gif_url)


def watch(on_message, stop=None, on_ready=None, proc_holder=None,
          my_name=None, show_system=False, tunnel_only=False, debug_log=None,
          pid_getter=None):
    """Attach to HytaleClient and call on_message(Msg) per incoming
    message. Mirrors receiver_quiche.watch so app.py can wire either identically.
    """
    import frida

    if pid_getter is None:
        pid_getter = memio.find_client_pid
    reasm = crypto.Reassembler()
    
    global _shared_handle
    handle = _SessionHandle()
    _shared_handle = handle
    
    if proc_holder is not None:
        proc_holder.append(handle)
    ready_sent = False

    dbg = None
    if debug_log:
        try:
            dbg = open(debug_log, "a", buffering=1)
        except OSError:
            dbg = None

    def _log(*parts):
        if dbg:
            dbg.write("\t".join(str(p) for p in parts) + "\n")

    def on_msg(message, _data):
        nonlocal ready_sent
        if message.get("type") == "error":
            on_message(chatframe.Msg(sender="·", body=f"quiche hook error: {message.get('description')}", kind="system"))
            return
        p = message.get("payload") or {}
        t = p.get("t")
        if t == "ready":
            if on_ready and not ready_sent:
                on_ready()
                ready_sent = True
        elif t == "noexport":
            on_message(chatframe.Msg(sender="·", body="quiche_conn_stream_recv not found in client", kind="system"))
        elif t == "buf" and _data:
            raw = _data
            lines = chatframe.parse_all(raw)
            if dbg:
                import re as _re
                nsig = len(chatframe._SIG_RE.findall(raw))
                ascii_preview = _re.sub(rb"[^\x20-\x7e]", b".", raw).decode()
                _log("BUF", f"n={len(raw)}", f"sigs={nsig}",
                     f"lines={len(lines)}", ascii_preview[:1500])
                if nsig == 0 and b"\x07#" in raw and b": " in raw:
                    _log("MISSHEX", raw.hex())
            for cl in lines:
                if cl.kind not in _PLAYER_KINDS and not show_system:
                    _log("DROP-system", cl.kind, repr(cl.full[:80]))
                    continue
                msg = _build_msg(cl, my_name, reasm)
                if msg is None:
                    _log("HOLD-chunk", cl.kind, repr(cl.body[:40]))
                    continue
                if tunnel_only and not msg.is_tunnel:
                    _log("DROP-tunnelonly", cl.kind, repr(msg.body[:40]))
                    continue
                _log("EMIT", msg.kind, msg.sender, repr(msg.body[:80]))
                on_message(msg)

    # (Re)attach loop: wait for the client, install the hook, then idle until stop.
    while stop is None or not stop.is_set():
        pid = pid_getter()
        if pid is None:
            time.sleep(1.0)
            continue
        try:
            session = frida.attach(pid)
            handle.session = session
            script = session.create_script(_AGENT)
            handle.script = script
            script.on("message", on_msg)
            script.load()
        except Exception as e:                            # noqa: BLE001
            on_message(chatframe.Msg(sender="·", body=f"quiche capture failed to attach: {e}", kind="system"))
            time.sleep(2.0)
            continue
        # Hook is live; sleep until told to stop or the client dies.
        try:
            while stop is None or not stop.is_set():
                time.sleep(0.4)
                try:
                    if not session.is_detached:
                        continue
                except Exception:
                    pass
                break                                     # client exited -> re-attach
        finally:
            handle.terminate()
            handle.session = handle.script = None
        if stop is not None and stop.is_set():
            break
        time.sleep(1.0)                                   # client died; loop to re-attach

if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else None
    watch(lambda m: print(f"[{m.kind}] {m.sender}{' 🔒' if m.is_tunnel else ''}: {m.body}"),
          my_name=name, show_system=True)
