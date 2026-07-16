#!/usr/bin/env python3
"""Direct chat SEND via ptrace injection (no chatbox typing, no Frida).

Recon (quiche_send_probe.py) reversed the outbound frame the client writes on QUIC
stream 0 for a chat line:

    [u32 LE body_len][u32 LE 0x000000d3][0x01][LEB128 text_len][UTF-8 text]

where body_len = len(0x01 + text_len-varint + text), and `text` is the RAW line the
client would send (commands included: "/p chat ...", "/msg NAME ..."). The server does
the command parsing, so injecting == handing the client-serialized frame to quiche.

Frida can't inject into this Flatpak-sandboxed client (its auto-injector mis-writes across
the namespace), but RAW ptrace works fine as root. So we drive ptrace directly:

  1. Find the dedicated **QuicheSend** thread (by name) and PTRACE_ATTACH just it -- it is
     the one thread that calls quiche_conn_send, so nothing else touches `conn` while we
     hold it stopped (no cross-thread race on the not-thread-safe connection).
  2. Set a HARDWARE breakpoint (DR0/DR7 -- per-thread, so no other thread can trip it) on
     quiche_conn_send. When QuicheSend hits it we're stopped at a clean API boundary with
     a live `conn` in rdi.
  3. Hijack that stopped thread to call quiche_conn_stream_send(conn, 0, frame, len, 0,
     err): write the frame to a scratch area below rsp (unused stack), set the arg regs +
     a non-canonical return address, PTRACE_CONT, and catch the SIGSEGV when it "returns"
     into that address. Read the result, restore the saved registers.
  4. Resume the thread -- it proceeds to run the very quiche_conn_send we caught, which
     packetizes our just-queued stream-0 bytes immediately. Done.

We never open a connection or touch crypto; the client encrypts our frame with its own
live session keys, exactly as if you had typed the line.

MODES
  python3 -m hytale_tunnel.inject --check              # SAFE: attach + grab a conn, no send
  python3 -m hytale_tunnel.inject --send "/p chat hi"  # inject one or more lines (test)
  (root, via pkexec)  --inject                         # read chat lines from stdin, inject
"""
import argparse
import ctypes
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time


def _install_exit_signals() -> None:
    """Turn SIGTERM/SIGINT into SystemExit so `finally` (detach) runs -- otherwise a
    killed root helper stays attached to the game thread and blocks the next run."""
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, lambda *_: sys.exit(0))
        except Exception:                           # noqa: BLE001
            pass

# ------------------------------------------------------------------- frame builder

CHAT_MSG_TYPE = 0x000000D3
_FLAG = 0x01


def _leb128(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def build_chat_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    body = bytes([_FLAG]) + _leb128(len(data)) + data
    return struct.pack("<II", len(body), CHAT_MSG_TYPE) + body


# ------------------------------------------------------------------- ptrace plumbing

_PT = dict(POKEUSER=6, CONT=7, SINGLESTEP=9, GETREGS=12, SETREGS=13, ATTACH=16, DETACH=17,
           SEIZE=0x4206, INTERRUPT=0x4207)
_WALL = 0x40000000                       # wait on threads (not just child procs)
_DEBUGREG = 848                          # offsetof(struct user, u_debugreg) on x86-64
_MAGIC_RET = 0x0000DEAD                   # canonical + unmapped (< mmap_min_addr): the
#   injected call's `ret` jumps here -> instruction-fetch #PF (SIGSEGV), and the fault's
#   rip == this. We ALSO confirm via rsp (the ret popped our 8-byte return address).

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.ptrace.restype = ctypes.c_long
_libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]


def _ptrace(req: int, tid: int, addr: int = 0, data: int = 0) -> int:
    ctypes.set_errno(0)
    r = _libc.ptrace(req, tid, ctypes.c_void_p(addr), ctypes.c_void_p(data))
    e = ctypes.get_errno()
    if r == -1 and e:
        raise OSError(e, os.strerror(e), f"ptrace(req={req}, tid={tid})")
    return r


class Regs(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8",
        "rax", "rcx", "rdx", "rsi", "rdi", "orig_rax", "rip", "cs", "eflags",
        "rsp", "ss", "fs_base", "gs_base", "ds", "es", "fs", "gs")]


def _getregs(tid: int) -> Regs:
    r = Regs()
    _ptrace(_PT["GETREGS"], tid, 0, ctypes.addressof(r))
    return r


def _setregs(tid: int, r: Regs) -> None:
    _ptrace(_PT["SETREGS"], tid, 0, ctypes.addressof(r))


def _wait(tid: int):
    _, status = os.waitpid(tid, _WALL)
    return status


# --- ptrace memory access (target thread must be ptrace-STOPPED) ---

def _peek(tid: int, addr: int) -> int:
    ctypes.set_errno(0)
    v = _libc.ptrace(2, tid, ctypes.c_void_p(addr), None)      # PTRACE_PEEKDATA
    e = ctypes.get_errno()
    if v == -1 and e:
        raise OSError(e, os.strerror(e), f"peek@{addr:#x}")
    return v & 0xFFFFFFFFFFFFFFFF


def _poke(tid: int, addr: int, word: int) -> None:
    _ptrace(5, tid, addr, word)                                # PTRACE_POKEDATA


def _read_mem(tid: int, addr: int, n: int) -> bytes:
    a0, a1 = addr & ~7, (addr + n + 7) & ~7
    raw = bytearray()
    for a in range(a0, a1, 8):
        raw += struct.pack("<Q", _peek(tid, a))
    off = addr - a0
    return bytes(raw[off:off + n])


def _write_mem(tid: int, addr: int, data: bytes) -> None:
    a0, a1 = addr & ~7, (addr + len(data) + 7) & ~7
    raw = bytearray(_read_mem(tid, a0, a1 - a0))               # read-modify-write edge words
    off = addr - a0
    raw[off:off + len(data)] = data
    for i in range(0, len(raw), 8):
        _poke(tid, a0 + i, struct.unpack("<Q", bytes(raw[i:i + 8]))[0])


# ------------------------------------------------------------------- target lookup

def _is_zombie(pid: int) -> bool:
    # A leftover "<defunct>" client (state Z) shares the name but has no maps.
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] == b"Z"
    except (OSError, IndexError):
        return True


def _has_libquiche(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/maps") as f:
            return any("libquiche.so" in line for line in f)
    except OSError:
        return False


def _game_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "HytaleClient"],
                         capture_output=True, text=True).stdout.split()
    pids = [int(x) for x in out]
    # pgrep lists the lowest pid first, which may be a leftover <defunct> zombie
    # that has no maps. Pick the live client that actually links libquiche.so.
    for p in pids:
        if _has_libquiche(p):
            return p
    for p in pids:
        if not _is_zombie(p):
            return p
    return pids[0] if pids else None


def _quiche_send_tid(pid: int) -> int | None:
    """The tid of the dedicated 'QuicheSend' thread (the only caller of conn_send)."""
    taskdir = f"/proc/{pid}/task"
    for tid in os.listdir(taskdir):
        try:
            with open(f"{taskdir}/{tid}/comm") as f:
                if f.read().strip() == "QuicheSend":
                    return int(tid)
        except OSError:
            continue
    return None


def _lib_maps(pid: int, needle: str) -> list[tuple[int, int, int]]:
    """(map_offset, start, size) for each mapping of `needle` in the target."""
    out = []
    with open(f"/proc/{pid}/maps") as f:
        for line in f:
            if needle not in line:
                continue
            parts = line.split()
            span, off = parts[0], int(parts[2], 16)
            a, b = (int(x, 16) for x in span.split("-"))
            out.append((off, a, b - a))
    return out


def _sym_offsets(libpath: str) -> dict:
    """File offsets of the quiche symbols we need (via nm; hardcoded fallback)."""
    want = {"quiche_conn_send", "quiche_conn_stream_send"}
    res = {}
    try:
        out = subprocess.run(["nm", "-D", libpath], capture_output=True, text=True).stdout
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 3 and p[-1] in want:
                res[p[-1]] = int(p[0], 16)
    except Exception:                                   # noqa: BLE001
        pass
    res.setdefault("quiche_conn_send", 0x16D120)        # observed on this build
    res.setdefault("quiche_conn_stream_send", 0x16F580)
    return res


# ------------------------------------------------------------------- the injector

class Injector:
    """SEIZE every thread, set a software breakpoint on quiche_conn_send, and inject a
    framed chat line on whichever thread lands there (it holds the shared-conn lock, so
    no other thread is inside the connection at that moment)."""

    def __init__(self, pid: int):
        self.pid = pid
        maps = _lib_maps(pid, "libquiche.so")
        if not maps:
            raise RuntimeError("libquiche.so not mapped")
        libpath = next((l.rsplit(None, 1)[-1] for l in open(f"/proc/{pid}/maps")
                        if "libquiche.so" in l), "libquiche.so")
        offs = _sym_offsets(libpath.strip())
        self.addr_conn_send = self._resolve(maps, offs["quiche_conn_send"])
        self.addr_stream_send = self._resolve(maps, offs["quiche_conn_stream_send"])
        self.seized: list[int] = []
        self._orig_word = None                           # saved 8 bytes at conn_send

    @staticmethod
    def _resolve(maps, sym_value: int) -> int:
        # nm gives the symbol's VIRTUAL address (st_value), not a file offset. Runtime =
        # load_bias + st_value, where load_bias is the base of the mapping at file offset 0
        # (its p_vaddr is 0). Using a later segment's file offset is wrong -- the exec
        # segment's p_vaddr can differ from its p_offset by the page-alignment gap.
        base = next((start for off, start, _ in maps if off == 0),
                    min(m[1] for m in maps))
        return base + sym_value

    def _tids(self) -> list[int]:
        try:
            return [int(t) for t in os.listdir(f"/proc/{self.pid}/task")]
        except OSError:
            return []

    def _live(self) -> int:
        for tid in self.seized:
            if os.path.exists(f"/proc/{tid}"):
                return tid
        raise RuntimeError("no live seized thread")

    def attach(self) -> None:
        for tid in self._tids():
            try:
                _ptrace(_PT["SEIZE"], tid)                # non-stopping attach
                self.seized.append(tid)
            except OSError:
                pass
        if not self.seized:
            raise RuntimeError("could not seize any thread")

    def detach(self) -> None:
        self._remove_bp_if_set()
        for tid in list(self.seized):
            sig = 0
            try:
                wp, st = os.waitpid(tid, _WALL | os.WNOHANG)     # pending stop?
                if wp == 0:
                    _ptrace(_PT["INTERRUPT"], tid)               # stop it so we can detach
                    os.waitpid(tid, _WALL)
                elif os.WIFSTOPPED(st):
                    s = os.WSTOPSIG(st)
                    if s not in (5, 19, 21, 22):                 # a real signal -> re-deliver
                        sig = s
            except (OSError, ChildProcessError):
                pass
            try:
                _ptrace(_PT["DETACH"], tid, 0, sig)
            except OSError:
                pass
        self.seized = []

    # ptrace memory ops need a STOPPED thread, so install/remove the int3 by briefly
    # interrupting one seized thread.
    def _install_bp(self) -> None:
        tid = self._live()
        _ptrace(_PT["INTERRUPT"], tid)
        _wait(tid)
        try:
            self._orig_word = _peek(tid, self.addr_conn_send)
            _poke(tid, self.addr_conn_send, (self._orig_word & ~0xFF) | 0xCC)
        finally:
            _ptrace(_PT["CONT"], tid)

    def _remove_bp_if_set(self) -> None:
        if self._orig_word is None:
            return
        try:
            tid = self._live()
            _ptrace(_PT["INTERRUPT"], tid)
            _wait(tid)
            try:
                _poke(tid, self.addr_conn_send, self._orig_word)
            finally:
                _ptrace(_PT["CONT"], tid)
        except Exception:                                # noqa: BLE001
            pass
        self._orig_word = None

    def _wait_bp(self, timeout: float):
        """Wait for a seized thread to hit the conn_send int3 (leaving it STOPPED). Returns
        (tid, saved_regs, conn) with rip backed up to conn_send. Other stops pass through."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                wpid, status = os.waitpid(-1, _WALL | os.WNOHANG)
            except ChildProcessError:
                time.sleep(0.005)
                continue
            if wpid == 0:
                time.sleep(0.002)
                continue
            if not os.WIFSTOPPED(status):
                continue
            sig = os.WSTOPSIG(status)
            if sig == 5:                                 # SIGTRAP
                r = _getregs(wpid)
                if r.rip == self.addr_conn_send + 1:     # our int3
                    r.rip = self.addr_conn_send
                    return wpid, r, r.rdi
                _ptrace(_PT["CONT"], wpid)
            elif sig in (19, 21, 22):
                _ptrace(_PT["CONT"], wpid)
            else:
                _ptrace(_PT["CONT"], wpid, 0, sig)
        raise RuntimeError(f"no quiche_conn_send hit in {timeout:.0f}s "
                           "(move in-game / send a chat line to generate traffic)")

    def _drain_stragglers(self) -> None:
        """Resume any other thread that also stopped on the (now-removed) int3."""
        for _ in range(128):
            try:
                wpid, status = os.waitpid(-1, _WALL | os.WNOHANG)
            except ChildProcessError:
                break
            if wpid == 0:
                break
            if os.WIFSTOPPED(status):
                sig = os.WSTOPSIG(status)
                if sig == 5:
                    r = _getregs(wpid)
                    if r.rip == self.addr_conn_send + 1:
                        r.rip = self.addr_conn_send
                        _setregs(wpid, r)
                    _ptrace(_PT["CONT"], wpid)
                else:
                    _ptrace(_PT["CONT"], wpid, 0, 0 if sig in (19, 21, 22) else sig)

    def _call_stream_send(self, tid: int, saved: "Regs", conn: int, frame: bytes) -> int:
        """quiche_conn_stream_send(conn, 0, frame, len, 0, err) on the stopped thread `tid`.
        Scratch goes in unused stack below rsp; a non-canonical return address faults
        (SIGSEGV) so we regain control; `saved` is restored afterwards."""
        base = saved.rsp
        new_rsp = ((base - 0x1000) & ~0xF) | 8           # rsp%16==8 at entry
        frame_addr = (base - 0x800) & ~0xF               # above new_rsp, below rsp (unused)
        err_addr = frame_addr - 0x40
        _write_mem(tid, frame_addr, frame)
        _write_mem(tid, err_addr, b"\x00" * 8)
        _write_mem(tid, new_rsp, struct.pack("<Q", _MAGIC_RET))

        r = Regs.from_buffer_copy(saved)
        r.rip = self.addr_stream_send
        r.rsp = new_rsp
        r.rdi, r.rsi, r.rdx, r.rcx, r.r8, r.r9 = conn, 0, frame_addr, len(frame), 0, err_addr
        _setregs(tid, r)
        _ptrace(_PT["CONT"], tid)
        # Wait for OUR thread to fault on the sentinel return, pumping any other thread's
        # signal-stops through so GC etc. keeps working during the (sub-ms) call.
        out, status = None, 0
        deadline = time.time() + 5.0
        while time.time() < deadline:
            wpid, st = os.waitpid(-1, _WALL | os.WNOHANG)
            if wpid == 0:
                time.sleep(0.001)
                continue
            if wpid == tid and os.WIFSTOPPED(st):
                out, status = _getregs(tid), st
                break
            if os.WIFSTOPPED(st):
                s = os.WSTOPSIG(st)
                _ptrace(_PT["CONT"], wpid, 0, 0 if s in (5, 19, 21, 22) else s)
        _setregs(tid, saved)                             # restore -> back at conn_send entry
        if out is None:
            raise RuntimeError("timed out waiting for stream_send to return")
        ret = ctypes.c_longlong(out.rax).value
        returned = (os.WSTOPSIG(status) in (11, 5)
                    and (out.rip == _MAGIC_RET or out.rsp == new_rsp + 8))
        if not returned:
            raise RuntimeError("stream_send did not return cleanly "
                               f"(sig={os.WSTOPSIG(status)}, rip={out.rip:#x}, "
                               f"rsp={out.rsp:#x} (want {new_rsp + 8:#x}), rax={ret})")
        return ret

    def grab_conn(self, timeout: float = 20.0) -> int:
        """Catch a thread at conn_send and return the live conn (for --check). Resumes it."""
        self._install_bp()
        try:
            tid, saved, conn = self._wait_bp(timeout)
            _poke(tid, self.addr_conn_send, self._orig_word)      # remove int3 via this thread
            self._orig_word = None
            self._drain_stragglers()
            _setregs(tid, saved)
            _ptrace(_PT["CONT"], tid)
            return conn
        finally:
            self._remove_bp_if_set()

    def send_line(self, line: str, timeout: float = 20.0) -> int:
        """Inject one chat line. Returns quiche_conn_stream_send's return value."""
        frame = build_chat_frame(line)
        self._install_bp()
        try:
            tid, saved, conn = self._wait_bp(timeout)
            _poke(tid, self.addr_conn_send, self._orig_word)      # remove int3 (via caught thread)
            self._orig_word = None
            self._drain_stragglers()
            ret = self._call_stream_send(tid, saved, conn, frame)
            _ptrace(_PT["CONT"], tid)                    # resume -> real conn_send flushes it
            return ret
        finally:
            self._remove_bp_if_set()


# ------------------------------------------------------------------- root entry points

def run_check() -> int:
    _install_exit_signals()
    pid = _game_pid()
    if pid is None:
        print("HytaleClient not running", file=sys.stderr)
        return 1
    try:
        inj = Injector(pid)
    except Exception as e:                              # noqa: BLE001
        print(f"# setup failed: {e}", file=sys.stderr)
        return 2
    print(f"# seizing all threads of pid {pid} …", file=sys.stderr, flush=True)
    try:
        inj.attach()
        print(f"# seized {len(inj.seized)} threads. conn_send@{inj.addr_conn_send:#x} "
              f"stream_send@{inj.addr_stream_send:#x}. move in-game now…",
              file=sys.stderr, flush=True)
        conn = inj.grab_conn()
        print(f"# GOT LIVE conn = {conn:#x}  (software bp on conn_send worked)",
              file=sys.stderr, flush=True)
        print("# RESULT: VIABLE ✓  (ptrace attach + conn grab worked)", file=sys.stderr, flush=True)
        return 0
    except Exception as e:                              # noqa: BLE001
        print(f"# RESULT: FAILED ✗  {e}", file=sys.stderr, flush=True)
        return 3
    finally:
        inj.detach()


def run_injector() -> int:
    _install_exit_signals()
    pid = _game_pid()
    if pid is None:
        print("# HytaleClient not running", file=sys.stderr, flush=True)
        return 1
    try:
        inj = Injector(pid)                             # resolve addresses once
    except Exception as e:                              # noqa: BLE001
        print(f"# setup failed: {e}", file=sys.stderr, flush=True)
        return 2
    print("# inject-ready", file=sys.stderr, flush=True)
    # Attach ONLY for the brief moment of each send. The game is .NET (CoreCLR suspends
    # threads via signals for GC); staying seized-but-idle would trap those signals and
    # freeze the game. Between messages we are fully detached, so the game runs normally.
    # iter(readline) -- NOT `for line in sys.stdin` -- so a single piped line is processed
    # immediately (the file iterator reads ahead and would wedge until more data/EOF).
    for line in iter(sys.stdin.readline, ""):
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            inj.attach()
            try:
                ret = inj.send_line(line)
                print(f"# injected ({len(line)}B line, stream_send -> {ret})",
                      file=sys.stderr, flush=True)
            finally:
                inj.detach()
        except Exception as e:                          # noqa: BLE001
            print(f"# inject failed: {e}", file=sys.stderr, flush=True)
    return 0


# ------------------------------------------------------------------- user side

def elevate_cmd(mode_args: list[str]) -> list[str]:
    py = sys.executable or "python3"
    here = os.path.abspath(__file__)
    if os.geteuid() == 0:
        return [py, here, *mode_args]
    if shutil.which("pkexec"):
        return ["pkexec", py, here, *mode_args]
    if shutil.which("sudo"):
        return ["sudo", py, here, *mode_args]
    return [py, here, *mode_args]


def _pump_stderr(proc, ready_evt):
    for l in iter(proc.stderr.readline, ""):
        sys.stderr.write(l)
        sys.stderr.flush()
        if "inject-ready" in l:
            ready_evt.set()


def cli_send(lines: list[str]) -> int:
    import time
    proc = subprocess.Popen(elevate_cmd(["--inject"]),
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = threading.Event()
    threading.Thread(target=_pump_stderr, args=(proc, ready), daemon=True).start()
    if not ready.wait(timeout=30):
        print("# injector did not become ready", file=sys.stderr)
        proc.terminate()
        return 1
    for ln in lines:
        proc.stdin.write(ln + "\n")
        proc.stdin.flush()
        time.sleep(0.25)
    time.sleep(0.8)
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except Exception:                                   # noqa: BLE001
        proc.terminate()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="hytale_tunnel.inject",
        description="Direct chat send via ptrace injection (no chatbox typing).")
    ap.add_argument("--check", action="store_true",
                    help="SAFE viability test: attach + grab a live conn (no injection)")
    ap.add_argument("--send", action="append", default=[], metavar="LINE",
                    help="a full chat line to inject, e.g. --send '/p chat hi' (repeatable)")
    ap.add_argument("--inject", action="store_true",
                    help="(root/internal) read chat lines from stdin and inject them")
    ap.add_argument("--check-agent", action="store_true",
                    help="(root/internal) run the viability check")
    args = ap.parse_args()
    if args.inject:
        return run_injector()
    if args.check_agent:
        return run_check()
    if args.check:
        return subprocess.call(elevate_cmd(["--check-agent"]))
    if args.send:
        return cli_send(args.send)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
