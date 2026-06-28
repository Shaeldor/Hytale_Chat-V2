"""Windows receive backend: flawless, via the quiche QUIC stream hook (Frida).

The Windows-native equivalent of the Linux eBPF capture. Instead of bpftrace's
uprobe on quiche_conn_stream_recv in libquiche.so, this uses Frida to attach to the
running HytaleClient and Interceptor-hook the same exported function in quiche.DLL.
Every time the client pulls decrypted application data off a QUIC stream, we scan
that buffer for our HX1/HX2 tokens and decrypt them with the user's keys.

Like the Linux path it sees only true *incoming* network data, so the client's local
echo of your own /msg is never picked up here (it never crosses the wire). No memory
scanning, no GC race. Frida attaches as the same user -- no admin needed for the
HytaleClient we target.

Requires `pip install frida` (bundled by setup-windows.bat). If frida or quiche.DLL
isn't present, app.py falls back to the memory scanner.
"""

import threading
import time

from . import crypto, memio

# The exact C export both platforms hook: ssize_t quiche_conn_stream_recv(conn,
# stream_id, out, buf_len, fin[, out_error_code]). We read `out` (3rd arg) on entry
# and the returned length on exit, then scan that slice. The JS does the scan so only
# matched token strings cross the Frida bridge (not megabytes of game-state stream).
_AGENT = r"""
function resolve(name) {
  // Frida 17 removed the old static Module.getExportByName; walk the modules.
  for (const m of Process.enumerateModules()) {
    let e = null;
    try { e = m.findExportByName ? m.findExportByName(name) : null; } catch (_) {}
    if (!e) { try { e = m.getExportByName(name); } catch (_) {} }
    if (e) return { addr: e, mod: m.name };
  }
  return null;
}
function isB64(c) {
  return (c >= 65 && c <= 90) || (c >= 97 && c <= 122) ||
         (c >= 48 && c <= 57) || c === 43 || c === 47 || c === 61;
}
const MAX = 1 << 16;               // scan up to 64 KiB of each stream read
const MIN_TOK = 43;                // "HX1" + 40-char minimum base64 body
const r = resolve('quiche_conn_stream_recv');
if (!r) { send({t: 'noexport'}); }
else {
  send({t: 'ready', mod: r.mod, addr: r.addr.toString()});
  Interceptor.attach(r.addr, {
    onEnter(a) { this.out = a[2]; },
    onLeave(rv) {
      const n = rv.toInt32();
      if (n <= 0 || !this.out || this.out.isNull()) return;
      const u = new Uint8Array(this.out.readByteArray(n < MAX ? n : MAX));
      // ASCII tokens (the on-the-wire form, same as the Linux bpftrace regex).
      for (let i = 0; i + 3 < u.length; i++) {
        if (u[i] === 72 && u[i + 1] === 88 && (u[i + 2] === 49 || u[i + 2] === 50)) {
          let j = i + 3;
          while (j < u.length && isB64(u[j])) j++;
          if (j - i >= MIN_TOK) {
            let s = ''; for (let k = i; k < j; k++) s += String.fromCharCode(u[k]);
            send({t: 'tok', tok: s});
          }
          i = j;
        }
      }
      // UTF-16LE tokens, in case the app frames chat strings wide on the wire.
      for (let i = 0; i + 5 < u.length; i++) {
        if (u[i] === 72 && u[i + 1] === 0 && u[i + 2] === 88 && u[i + 3] === 0 &&
            (u[i + 4] === 49 || u[i + 4] === 50) && u[i + 5] === 0) {
          let j = i + 4, s = '';
          while (j + 1 < u.length && u[j + 1] === 0 && isB64(u[j])) { s += String.fromCharCode(u[j]); j += 2; }
          if (s.length >= MIN_TOK) send({t: 'tok', tok: s});
          i = j;
        }
      }
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


def watch(on_message, stop=None, on_ready=None, proc_holder=None, seen=None,
          pid_getter=None):
    """Attach to HytaleClient and call on_message(sender, plaintext) per incoming
    message. Mirrors receiver_quiche.watch so app.py can wire either identically.

    `seen` is a shared set of already-handled "HX1..." token strings; the UI adds its
    own outgoing tokens there so a (hypothetical) echo is skipped. `on_ready()` fires
    once the hook is installed.
    """
    import frida

    if seen is None:
        seen = set()
    if pid_getter is None:
        pid_getter = memio.find_client_pid
    reasm = crypto.Reassembler()
    handle = _SessionHandle()
    if proc_holder is not None:
        proc_holder.append(handle)
    ready_sent = False

    def on_msg(message, _data):
        nonlocal ready_sent
        if message.get("type") == "error":
            on_message("·", f"quiche hook error: {message.get('description')}")
            return
        p = message.get("payload") or {}
        t = p.get("t")
        if t == "ready":
            if on_ready and not ready_sent:
                on_ready()
                ready_sent = True
        elif t == "noexport":
            on_message("·", "quiche_conn_stream_recv not found in client")
        elif t == "tok":
            tok = p.get("tok", "")
            if tok[:3] not in ("HX1", "HX2") or tok in seen:
                return
            seen.add(tok)
            res = reasm.feed(tok, crypto.loaded_psks())   # single + chunked
            if res is not None:
                on_message(res[0], res[1])

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
            on_message("·", f"quiche capture failed to attach: {e}")
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
    watch(lambda s, t: print(f"[{s}] {t}"))
