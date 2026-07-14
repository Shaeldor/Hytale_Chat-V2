#!/usr/bin/env python3
"""Root-only capture helper: emit incoming chat tokens from HytaleClient.

Hooks quiche_conn_stream_recv (the QUIC stream where decrypted application data --
including chat -- is delivered) via bpftrace, extracts HX1 tokens, and prints each
unique one to stdout. Runs as root (eBPF); holds NO keys -- the raw tokens are just
ciphertext that was already on the wire, so the user-side overlay does the decrypt.

Standalone:  sudo python3 -m hytale_tunnel.quiche_capture
"""
import os
import re
import signal
import subprocess
import sys


def find_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "HytaleClient"], capture_output=True, text=True).stdout.split()
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


def main() -> int:
    pid = find_pid()
    if not pid:
        print("# HytaleClient not running", file=sys.stderr, flush=True)
        return 1
    lib = find_libquiche(pid)
    if not lib:
        print("# libquiche.so not found in client", file=sys.stderr, flush=True)
        return 1

    prog = f"""
    uprobe:{lib}:quiche_conn_stream_recv /pid == {pid}/ {{ @b[tid] = arg2; }}
    uretprobe:{lib}:quiche_conn_stream_recv /pid == {pid} && @b[tid] != 0/ {{
        $n = (int64)retval;
        if ($n > 0) {{ printf("R %r\\n", buf(@b[tid], $n < 512 ? $n : 512)); }}
        delete(@b[tid]);
    }}
    """
    env = dict(os.environ, BPFTRACE_MAX_STRLEN="512")
    # bpftrace block-buffers stdout when it's a pipe (it uses C++ iostream, so stdbuf
    # doesn't help). Give it a pseudo-terminal so it line-buffers and flushes per
    # event -> live streaming. stderr stays inherited so attach errors are visible.
    import pty
    import select
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bpftrace", "-e", prog], stdout=slave,
                            stderr=None, env=env, close_fds=True)
    os.close(slave)

    def _stop(*_):
        try:
            proc.terminate()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print("# capture-ready", file=sys.stderr, flush=True)
    token_re = re.compile(rb"HX[12][A-Za-z0-9+/=]+")   # HX1 single, HX2 chunk
    seen: set[bytes] = set()
    buf = b""
    while True:
        r, _, _ = select.select([master], [], [], 1.0)
        if master in r:
            try:
                data = os.read(master, 8192)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                for tok in token_re.findall(line):
                    if len(tok) < 43 or tok in seen:   # 43 = "HX1" + 40 min base64
                        continue
                    seen.add(tok)
                    sys.stdout.write(tok.decode("ascii") + "\n")
                    sys.stdout.flush()
        elif proc.poll() is not None:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
