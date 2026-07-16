#!/usr/bin/env python3
"""Root-only capture helper: emit QUIC stream-0 buffers from HytaleClient.

Hooks quiche_conn_stream_recv (the QUIC stream where decrypted application data --
including chat -- is delivered) via bpftrace. Chat rides stream 0, but the game packs
SEVERAL framed messages into a single stream_recv read, so a chat line can sit at any
offset within a buffer (not just the start). We therefore emit every stream-0 body
buffer as ``C <hex>`` and let the user side locate chat at any offset -- doing the scan
in user space keeps it off the game's network thread. (An earlier version filtered to
chat at a fixed offset in-kernel and silently dropped packed chat messages.)

Runs as root (eBPF); holds NO keys -- any encrypted tunnel token inside a buffer is
ciphertext that was already on the wire, decrypted user-side by the overlay.

Standalone:  sudo python3 -m hytale_tunnel.quiche_capture
"""
import os
import re
import signal
import subprocess
import sys

# Reconstruct raw bytes from bpftrace %r output (printable literal, others \xNN).
_ESC = re.compile(rb"\\x([0-9a-fA-F]{2})|\\(.)")


def _unescape(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        m = _ESC.match(s, i)
        if m:
            if m.group(1) is not None:
                out.append(int(m.group(1), 16))
            else:
                out += {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"\\": b"\\"}.get(
                    m.group(2), m.group(2))
            i = m.end()
        else:
            out.append(s[i])
            i += 1
    return bytes(out)


def _is_zombie(pid: int) -> bool:
    # A leftover "<defunct>" client (state Z) keeps the name but has no maps. pgrep
    # lists it first (lower pid), so naively taking out[0] picks the dead one.
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] == b"Z"
    except (OSError, IndexError):
        return True


def find_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "HytaleClient"], capture_output=True, text=True).stdout.split()
    pids = [int(x) for x in out]
    # Prefer a live client that actually has libquiche.so mapped (skips the zombie
    # and any pre-map early-startup match); fall back to the first non-zombie.
    for p in pids:
        if find_libquiche(p):
            return p
    for p in pids:
        if not _is_zombie(p):
            return p
    return pids[0] if pids else None


def find_libquiche(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if "libquiche.so" in line:
                    return f"/proc/{pid}/root{line.split()[-1]}"
    except OSError:
        pass
    return None


# Capture window. A buffer may pack a chat line behind OTHER chat lines / server
# spam, and the chat-log signature for a line only exists at its own offset -- so
# if a line lands past the window it's truncated away before we ever see it. 2 KiB
# was sized for a quiet channel (chat seen packed up to offset ~700), but a busy
# channel (many players / event banners / joins) can pack far more ahead of a given
# line, silently dropping it (root cause of rare "message never showed up" reports,
# e.g. a normal-length message lost behind a burst of other chat). Bumped to 8 KiB;
# falls back through smaller windows if bpftrace rejects the strlen (verifier/BTF
# limits on some kernels).
_WINDOW = 8192


def _bpftrace_prog(lib: str, pid: int, window: int) -> str:
    # Emit every stream-0 body buffer (arg1 is the stream id; chat is on stream 0).
    # The chat scan happens in user space to keep the game's network thread cheap.
    return f"""
    uprobe:{lib}:quiche_conn_stream_recv /pid == {pid}/ {{
        if (arg1 == 0) {{ @b[tid] = arg2; }} else {{ @b[tid] = 0; }}
    }}
    uretprobe:{lib}:quiche_conn_stream_recv /pid == {pid} && @b[tid] != 0/ {{
        $n = (int64)retval;
        if ($n > 16) {{
            printf("C %r\\n", buf(@b[tid], $n < {window} ? $n : {window}));
        }}
        delete(@b[tid]);
    }}
    """


def main() -> int:
    pid = find_pid()
    if not pid:
        print("# HytaleClient not running", file=sys.stderr, flush=True)
        return 1
    lib = find_libquiche(pid)
    if not lib:
        print("# libquiche.so not found in client", file=sys.stderr, flush=True)
        return 1

    import pty
    import select
    import tty
    # Larger windows can trip the BPF verifier/strlen on some kernels; fall back.
    for window in (_WINDOW, 4096, 2048, 1024, 512):
        prog = _bpftrace_prog(lib, pid, window)
        # The client streams a LOT of non-chat traffic on stream 0 (MMO HUD/skill/inventory
        # updates); at the default 64-page perf buffer a burst (raid spam + the 20-name
        # player list + party GIF chunks) overflows the ring and silently drops events --
        # which is how whole chat lines went missing. A larger ring absorbs the bursts.
        env = dict(os.environ, BPFTRACE_MAX_STRLEN=str(window),
                   BPFTRACE_PERF_RB_PAGES="512")
        master, slave = pty.openpty()
        # RAW mode: bpftrace lines (big hex buffers) must not hit the terminal's
        # canonical line-length limit, which would truncate/drop them.
        tty.setraw(slave)
        proc = subprocess.Popen(["bpftrace", "-e", prog], stdout=slave,
                                stderr=subprocess.PIPE, env=env, close_fds=True)
        os.close(slave)
        # Give bpftrace a moment; if it dies immediately (verifier error), retry smaller.
        try:
            err = proc.stderr.readline()  # "Attached N probes" or an error
        except Exception:
            err = b""
        if proc.poll() is not None and b"Attached" not in err:
            sys.stderr.buffer.write(err + (proc.stderr.read() or b""))
            sys.stderr.flush()
            os.close(master)
            continue
        break
    else:
        print("# bpftrace failed to attach", file=sys.stderr, flush=True)
        return 1

    def _stop(*_):
        try:
            proc.terminate()                     # stop the root bpftrace (don't orphan it)
        except Exception:
            pass
        # The overlay (our stdout reader) has gone away, so stdout is a broken pipe. Point
        # fd 1 at /dev/null and hard-exit: os._exit skips the interpreter's shutdown flush,
        # which would otherwise retry the failed flush and spew
        # "Exception ignored while flushing sys.stdout: BrokenPipeError" to the terminal.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Watch the overlay PID (passed as argv[1]); when it exits, so do we -- this stops
    # the root bpftrace from orphaning (pkexec doesn't forward our death to it).
    watch_pid = None
    if len(sys.argv) > 1:
        try:
            watch_pid = int(sys.argv[1])
        except ValueError:
            watch_pid = None

    def _overlay_alive() -> bool:
        if watch_pid is None:
            return True
        try:
            os.kill(watch_pid, 0)
            return True
        except OSError:
            return False

    print("# capture-ready", file=sys.stderr, flush=True)
    buf = b""
    while True:
        r, _, _ = select.select([master], [], [], 1.0)
        if master in r:
            try:
                data = os.read(master, 16384)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.startswith(b"C "):
                    raw = _unescape(line[2:])
                    try:
                        sys.stdout.write("C " + raw.hex() + "\n")
                        sys.stdout.flush()
                    except BrokenPipeError:
                        _stop()
        elif proc.poll() is not None:
            break
        if not _overlay_alive():
            break
    _stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
