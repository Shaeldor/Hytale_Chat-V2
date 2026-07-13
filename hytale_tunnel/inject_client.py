"""User-side glue for the ptrace injection sender.

Launches the elevated injector (inject.py, root) once and feeds it composed chat lines;
the injector frames each line and injects it onto the game's QUIC stream (no chatbox
typing). Crypto/chunking is reused unchanged -- only the DELIVERY differs from send_linux.
"""
import subprocess
import sys
import threading
import time

from . import crypto, inject


def lines_for(mode: str, target: str | None, message: str) -> list[str]:
    """Produce the ready-to-send chat line(s) for a compose-box message (may chunk)."""
    if mode == "private":
        return [f"/msg {target} {t}" for t in crypto.encrypt_messages(target, message)]
    if mode == "party":
        return [f"{crypto.PARTY_PREFIX}{t}" for t in crypto.encrypt_group_messages(target, message)]
    return crypto.split_public_lines(message)       # public: raw, but split to fit CHAT_LIMIT


class Injector:
    """Thread-safe handle to the elevated injector process.

    Waits for the injector to print `inject-ready` (which only happens after the pkexec
    prompt is approved and it has resolved the game's symbols) before writing the first
    line, and mirrors the injector's stderr so its `# ...` status/errors are visible.
    """

    def __init__(self, watch_pid: int, gap: float = 0.25):
        self._proc = subprocess.Popen(
            inject.elevate_cmd(["--inject"]),
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._lock = threading.Lock()
        self._gap = gap
        self._ready = threading.Event()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stderr(self) -> None:
        for line in iter(self._proc.stderr.readline, ""):
            if "inject-ready" in line:
                self._ready.set()
            sys.stderr.write("[inject] " + line)
            sys.stderr.flush()
        self._ready.set()                            # process died -> unblock any waiter

    def send(self, mode: str, target: str | None, message: str) -> list[str]:
        if not self._ready.wait(timeout=90):
            raise RuntimeError("injector never became ready (approve the pkexec prompt)")
        lines = lines_for(mode, target, message)
        for i, line in enumerate(lines):
            with self._lock:
                try:
                    if self._proc.stdin and not self._proc.stdin.closed:
                        self._proc.stdin.write(line + "\n")
                        self._proc.stdin.flush()
                    else:
                        raise RuntimeError("injector stdin closed")
                except (BrokenPipeError, ValueError, OSError, RuntimeError) as e:
                    raise RuntimeError(f"injector unavailable ({e}); was the pkexec prompt "
                                       "approved? (see the [inject] lines in the terminal)")
            if i < len(lines) - 1:
                time.sleep(self._gap)               # small gap between chunked lines
        return lines

    @property
    def proc(self) -> subprocess.Popen:
        return self._proc
