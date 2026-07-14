"""User-side glue for the Frida injection sender on Windows.

On Linux this launched a root ptrace helper. On Windows, we just delegate to
our existing Frida hook running in receiver_quiche_win.
"""
import time
from . import crypto, receiver_quiche_win


def lines_for(mode: str, target: str | None, message: str) -> list[str]:
    """Produce the ready-to-send chat line(s) for a compose-box message (may chunk)."""
    if mode == "private":
        return [f"/msg {target} {t}" for t in crypto.encrypt_messages(target, message)]
    if mode in ("party", "party_private"):
        return [f"/party chat {t}" for t in crypto.encrypt_group_messages(target, message)]
    return crypto.split_public_lines(message)       # public: raw, but split to fit CHAT_LIMIT


def _build_chat_frame(text: str) -> bytes:
    import struct
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

    data = text.encode("utf-8")
    body = bytes([_FLAG]) + _leb128(len(data)) + data
    return struct.pack("<II", len(body), CHAT_MSG_TYPE) + body


class Injector:
    """Thread-safe handle to the Frida injector.
    
    On Windows, this just routes to receiver_quiche_win's shared script session.
    """

    def __init__(self, watch_pid: int = 0, gap: float = 0.25):
        self._gap = gap
        
    def send(self, mode: str, target: str | None, message: str) -> list[str]:
        lines = lines_for(mode, target, message)
        
        for i, line in enumerate(lines):
            frame = _build_chat_frame(line)
            success = receiver_quiche_win.inject_frame(frame)
            if not success:
                print(f"[inject_client] Failed to inject frame: {line}")
                
            if i < len(lines) - 1:
                time.sleep(self._gap)               # small gap between chunked lines
        return lines

    @property
    def proc(self):
        # Compatibility property for app.py
        class DummyProc:
            def terminate(self): pass
        return DummyProc()
