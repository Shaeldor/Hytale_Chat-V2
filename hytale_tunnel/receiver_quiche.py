"""Linux receive backend: flawless, via the quiche QUIC stream hook.

Spawns the root capture helper (eBPF on quiche_conn_stream_recv), which emits raw
HX1 tokens; we decrypt them with the user's keys and hand plaintext to the overlay.
Every incoming chat message is delivered here exactly once, the instant quiche
decrypts it -- no memory scanning, no GC race, no misses.

Privilege: the capture needs root, so we launch it via pkexec (GUI prompt) or sudo.
The overlay stays a normal user process; only the eBPF capture is elevated, and it
handles no keys (tokens are ciphertext that was already on the wire).
"""

import os
import shutil
import subprocess
import sys

from . import crypto

_CAPTURE = os.path.join(os.path.dirname(__file__), "quiche_capture.py")


def _elevate_cmd() -> list[str]:
    py = sys.executable or "python3"
    if os.geteuid() == 0:                     # already root
        return [py, _CAPTURE]
    if shutil.which("pkexec"):                # GUI password prompt (best for the overlay)
        return ["pkexec", py, _CAPTURE]
    if shutil.which("sudo"):
        return ["sudo", py, _CAPTURE]
    return [py, _CAPTURE]


def watch(on_message, stop=None, on_ready=None, proc_holder=None, seen=None):
    """Run the capture and call on_message(sender, plaintext) per incoming message.

    `seen` is a shared set of already-handled token strings ("HX1…"). The UI adds
    its own outgoing tokens there before sending, so the server's echo of our own
    whisper is skipped instead of shown as if the friend sent it.
    """
    if seen is None:
        seen = set()
    try:
        proc = subprocess.Popen(_elevate_cmd(), stdout=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as e:                     # noqa: BLE001
        if on_ready:
            on_ready()
        on_message("·", f"capture failed to start: {e}")
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
            tok = line.strip()
            if tok[:3] not in ("HX1", "HX2") or tok in seen:
                continue
            seen.add(tok)
            res = reasm.feed(tok, crypto.loaded_psks())   # handles single + chunked
            if res is not None:
                on_message(res[0], res[1])
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    watch(lambda s, t: print(f"[{s}] {t}"))
