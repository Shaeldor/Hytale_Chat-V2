"""Diagnostic: what does HytaleClient load, and is the quiche hook target present?

The Windows overlay only uses the instant quiche hook if the client exposes the
function the hook attaches to. The Linux client links libquiche.so; the Windows build
may use a different QUIC library, or static-link it (no separate module). This lists
the client's modules and searches every module's exports for the hook target so we
know whether the instant path is possible. Run via diag-modules.bat (double-click).
"""

import sys
import traceback


def main() -> int:
    try:
        import frida
    except Exception as e:                       # noqa: BLE001
        print("frida is NOT importable in THIS python:", repr(e))
        print("python:", sys.executable)
        print("Fix: run setup-windows.bat (it installs frida), and make sure the same")
        print("python that runs the tunnel is the one with frida.")
        return 1

    print("frida", frida.__version__, "| python", sys.executable)
    dev = frida.get_local_device()
    procs = [p for p in dev.enumerate_processes() if "hytale" in p.name.lower()]
    if not procs:
        print("HytaleClient is NOT running. Start the game first, then re-run this.")
        return 2
    pid = procs[0].pid
    print(f"attached to {procs[0].name} (pid {pid})\n")

    s = frida.attach(pid)
    mods = s.enumerate_modules()
    print(f"total modules loaded: {len(mods)}\n")

    kw = ("quic", "quiche", "ssl", "tls", "crypto", "net", "msquic", "boring",
          "nghttp", "ngtcp", "curl", "schannel", "winhttp", "ws2", "wininet", "openssl")
    print("== network / crypto-ish modules ==")
    any_net = False
    for m in mods:
        if any(k in m.name.lower() for k in kw):
            any_net = True
            print(f"   {m.name}")
    if not any_net:
        print("   (none matched the keyword list)")

    print("\n== searching ALL module exports for the hook target ==")
    hits = []
    for m in mods:
        try:
            for ex in m.enumerate_exports():
                low = ex.name.lower()
                if "quiche" in low or "stream_recv" in low:
                    hits.append((m.name, ex.name))
        except Exception:                        # some modules refuse export enumeration
            pass
    if hits:
        for mod, name in hits:
            print(f"   FOUND  {name}   in   {mod}")
        print("\n-> the instant quiche hook IS possible; hook one of the above.")
    else:
        print("   no 'quiche' / '*stream_recv*' exports in ANY module")
        print("\n-> the Windows build likely static-links QUIC or uses a different lib.")
        print("   The instant hook needs a different target; otherwise keep the")
        print("   memory scanner (run the tunnel with: --sweep 0.4 --interval 0.1).")

    print("\n== all module names ==")
    for m in mods:
        print(f"   {m.name}")

    s.detach()
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:                            # noqa: BLE001
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
