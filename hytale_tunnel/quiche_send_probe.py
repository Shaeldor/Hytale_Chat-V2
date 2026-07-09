#!/usr/bin/env python3
"""Recon tool (OBSERVE-ONLY): learn the client->server Hytale chat wire format.

We already hook ``quiche_conn_stream_recv`` to READ incoming chat (quiche_capture.py).
This mirrors that, but hooks the send counterpart ``quiche_conn_stream_send`` at ENTRY --
the outgoing bytes are in the argument buffer at call time (unlike recv, which fills the
buffer on return). It prints the outbound buffers the game ALREADY sends so we can find
our own typed message and reverse-engineer its framing.

SAFETY: this only READS buffers (bpftrace ``printf(buf(...))``). It NEVER calls
stream_send, never writes to the connection, never injects. Same risk profile as the
receive capture. No keys are involved (outbound tokens are ciphertext we made ourselves).

Usage (user side -- it elevates the capture itself via pkexec/sudo):
    python3 -m hytale_tunnel.quiche_send_probe --marker SENDPROBE_PUB
Then, in-game, type your marker in public chat, ``/msg <friend> SENDPROBE_MSG``, and
``/p chat SENDPROBE_PARTY``; the framing around each hit is printed. Add ``--log FILE``
to keep every buffer, ``--all`` to summarize every send, ``--min-len N`` to trim noise,
and ``--symbol NAME`` to try a different export (e.g. quiche_h3_send_body,
quiche_conn_dgram_send) if chat doesn't ride raw stream_send.

Root capture path (invoked automatically; you normally don't run this yourself):
    sudo python3 hytale_tunnel/quiche_send_probe.py --capture --watch <pid>
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys

_SYMBOL = "quiche_conn_stream_send"     # send counterpart of quiche_conn_stream_recv
_WINDOW = 8192

# --- self-contained helpers (mirror quiche_capture.py so the --capture path can run as a
#     bare root script under pkexec, with no package/relative-import context) ---
_ESC = re.compile(rb"\\x([0-9a-fA-F]{2})|\\(.)")


def _unescape(s: bytes) -> bytes:
    """Reconstruct raw bytes from bpftrace %r output (printable literal, others \\xNN)."""
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


def find_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "HytaleClient"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def find_libquiche(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if "libquiche.so" in line:
                    return f"/proc/{pid}/root{line.split()[-1]}"
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ root capture side

def _bpftrace_prog(lib: str, pid: int, window: int, symbol: str, min_len: int) -> str:
    # arg0=conn, arg1=stream_id, arg2=buf (outgoing bytes), arg3=buf_len.
    # Capture ALL streams (no stream-id gate) so we discover which one chat uses; emit
    # "S <stream_id> <bytes>". Read-only: printf(buf(...)) -- no map/conn writes.
    return f"""
    uprobe:{lib}:{symbol} /pid == {pid}/ {{
        $n = (uint64)arg3;
        if ($n >= {min_len}) {{
            printf("S %llu %r\\n", arg1, buf(arg2, $n < {window} ? $n : {window}));
        }}
    }}
    """


def run_capture(symbol: str, min_len: int, watch_pid: int | None) -> int:
    """Root side: attach the uprobe and stream 'S <sid> <hex>' lines to stdout."""
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
        prog = _bpftrace_prog(lib, pid, window, symbol, min_len)
        env = dict(os.environ, BPFTRACE_MAX_STRLEN=str(window))
        master, slave = pty.openpty()
        # RAW mode: big hex lines must not hit the terminal's canonical length limit.
        tty.setraw(slave)
        proc = subprocess.Popen(["bpftrace", "-e", prog], stdout=slave,
                                stderr=subprocess.PIPE, env=env, close_fds=True)
        os.close(slave)
        try:
            err = proc.stderr.readline()        # "Attached N probes" or an error
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
            proc.terminate()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def _watcher_alive() -> bool:
        if watch_pid is None:
            return True
        try:
            os.kill(watch_pid, 0)
            return True
        except OSError:
            return False

    print("# send-capture ready", file=sys.stderr, flush=True)
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
                if line.startswith(b"S "):
                    # line = b"S <sid> <escaped-bytes>"; split off sid, unescape the rest.
                    try:
                        sid, payload = line[2:].split(b" ", 1)
                    except ValueError:
                        continue
                    raw = _unescape(payload)
                    try:
                        sys.stdout.write(f"S {sid.decode('ascii','ignore')} {raw.hex()}\n")
                        sys.stdout.flush()
                    except BrokenPipeError:
                        _stop()
        elif proc.poll() is not None:
            break
        if not _watcher_alive():
            break
    _stop()
    return 0


# ------------------------------------------------------------------ user analysis side

def _elevate_cmd(extra: list[str]) -> list[str]:
    py = sys.executable or "python3"
    here = os.path.abspath(__file__)
    if os.geteuid() == 0:
        return [py, here, *extra]
    if shutil.which("pkexec"):
        return ["pkexec", py, here, *extra]
    if shutil.which("sudo"):
        return ["sudo", py, here, *extra]
    return [py, here, *extra]


def _ascii(data: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)


def _hexdump(data: bytes, hi_start: int = -1, hi_len: int = 0,
             ctx_before: int = 32, ctx_after: int = 24, width: int = 16) -> str:
    """Hex+ASCII dump of a window around [hi_start, hi_start+hi_len). '>' marks rows that
    overlap the highlighted (marker) region so the framing bytes before it are obvious."""
    if hi_start < 0:
        start, end = 0, min(len(data), ctx_before + ctx_after)
    else:
        start = max(0, hi_start - ctx_before)
        end = min(len(data), hi_start + hi_len + ctx_after)
    start -= start % width
    lines = []
    for off in range(start, end, width):
        row = data[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in row)
        mark = ">" if (hi_start >= 0 and off < hi_start + hi_len
                       and off + width > hi_start) else " "
        lines.append(f"{mark}{off:6d}  {hexs:<{width*3}}  {_ascii(row)}")
    return "\n".join(lines)


def analyze(markers: list[str], symbol: str, min_len: int,
            log_path: str | None, show_all: bool) -> int:
    if not markers and not show_all:
        print("Nothing to look for. Pass --marker <TEXT> (then type it in-game) and/or --all.",
              file=sys.stderr)
        return 2
    # Each marker is hunted for as BOTH UTF-8 and UTF-16LE (inbound chat was UTF-16;
    # outbound encoding is unknown -- this tells us which it is).
    variants = []
    for m in markers:
        variants.append((m, "utf-8", m.encode("utf-8")))
        variants.append((m, "utf-16le", m.encode("utf-16-le")))

    logf = open(log_path, "a") if log_path else None
    cmd = _elevate_cmd(["--capture", "--watch", str(os.getpid()),
                        "--symbol", symbol, "--min-len", str(min_len)])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    except Exception as e:                          # noqa: BLE001
        print(f"failed to launch capture: {e}", file=sys.stderr)
        return 1
    print(f"# hooking {symbol}. In-game, type your marker(s): "
          f"{', '.join(markers) or '(none)'}   (Ctrl-C to stop)", file=sys.stderr)
    hits = 0
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("S "):
                continue
            try:
                sid_s, hexs = line[2:].split(" ", 1)
                sid = int(sid_s)
                raw = bytes.fromhex(hexs)
            except ValueError:
                continue
            if logf:
                logf.write(line + "\n")
                logf.flush()
            if show_all:
                print(f"S stream={sid} len={len(raw)}  {_ascii(raw[:64])}")
            for label, enc, mb in variants:
                idx = raw.find(mb)
                if idx >= 0:
                    hits += 1
                    print(f"\n=== MARKER {label!r} [{enc}] on STREAM {sid}, "
                          f"buflen {len(raw)}, at offset {idx} ===")
                    print(_hexdump(raw, idx, len(mb)))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if logf:
            logf.close()
        try:
            proc.terminate()
        except Exception:
            pass
    print(f"\n# done. {hits} marker hit(s)."
          + (f" full log: {log_path}" if log_path else ""), file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="quiche_send_probe",
        description="Observe-only recon of the outbound (client->server) Hytale chat wire "
                    "format by watching quiche_conn_stream_send. Reads only; never injects.")
    ap.add_argument("--capture", action="store_true",
                    help="(root) run the bpftrace capture and emit 'S <sid> <hex>' lines "
                         "(invoked automatically via pkexec/sudo; not for manual use)")
    ap.add_argument("--watch", type=int, default=None,
                    help="pid to watch; the root capture exits when it dies")
    ap.add_argument("--symbol", default=_SYMBOL,
                    help=f"quiche export to hook (default {_SYMBOL}; fallbacks if chat "
                         "isn't here: quiche_h3_send_body, quiche_conn_dgram_send)")
    ap.add_argument("--min-len", type=int, default=1,
                    help="skip sends shorter than N bytes (trim keepalive/movement noise)")
    ap.add_argument("--marker", action="append", default=[],
                    help="text to hunt for in outbound buffers (repeatable); type it in-game")
    ap.add_argument("--log", default=None,
                    help="append every captured 'S <sid> <hex>' line here for offline study")
    ap.add_argument("--all", action="store_true",
                    help="print a one-line summary of EVERY captured send, not just hits")
    args = ap.parse_args()
    if args.capture:
        return run_capture(args.symbol, args.min_len, args.watch)
    return analyze(args.marker, args.symbol, args.min_len, args.log, args.all)


if __name__ == "__main__":
    raise SystemExit(main())
