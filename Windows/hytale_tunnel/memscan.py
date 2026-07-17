"""Scan the running HytaleClient's memory for messages addressed to us.

OS-independent scanning logic; the actual process lookup and memory reads come
from the memio backend (/proc on Linux, ReadProcessMemory on Windows).

The tunnel's wire token is "HX1" + base64(nonce||ciphertext+tag) (AES-256-GCM).
We scan writable memory for that marker and AEAD-decrypt each hit against our
shared keys -- a successful decrypt proves the message is genuine and tells us
which friend sent it. Legacy 344-char RSA blobs are also detected if we hold an
RSA key (kept for completeness; they exceed the 255-char chat limit).

Performance: the client has ~11 GB of writable memory, so a full sweep takes
~11 s (read-bound). A two-tier loop runs a periodic full sweep to locate the
chat buffer, then fast-polls only a few MB around the last hit.
"""

import hashlib
import re
import threading
import time

from . import crypto, memio


def token_hash(token: bytes) -> str:
    """Dedup key for a symmetric token (the base64 body, marker stripped)."""
    if isinstance(token, str):
        token = token.encode()
    return hashlib.sha256(b"s" + token).hexdigest()

# Re-exported so callers (app.py) keep using memscan.find_client_pid.
find_client_pid = memio.find_client_pid

CHUNK = 8 * 1024 * 1024           # 8 MiB read window
OVERLAP = 1024                    # carry-over so a token on a chunk edge isn't split
# Real message-bearing regions are small (<~80 MB each on Hytale); cap fast-poll to
# 256 MB/region so we never re-scan the giant GPU/old-gen arenas (sweeps cover those).
HOT_REGION_CAP = 256 * 1024 * 1024
MAX_HOT_REGIONS = 40                   # they're small, so keep plenty hot

PAD = b"=="                       # legacy-RSA anchor
_B64 = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")
# Match only the MARKER, then decrypt the following base64 run by trying lengths.
# (A greedy body regex merges back-to-back tokens because 'HX1' is itself base64,
#  swallowing the next token and dropping messages -- hence marker-only search.)
# Outgoing / command history is ASCII; messages RECEIVED render as UTF-16LE Java
# strings (each char + \x00), so we search for the marker in both encodings.
# "HX1" (single) or "HX2" (chunk) in UTF-16LE: H\x00 X\x00 [12]\x00
_MARK_ASCII = re.compile(re.escape(crypto.SYM_MARKER.encode()))
_MARK_UTF16 = re.compile(rb"H\x00X\x00[12]\x00")
_RUN_ASCII = re.compile(rb"[A-Za-z0-9+/=]+")
_RUN_UTF16 = re.compile(rb"(?:[A-Za-z0-9+/=]\x00)+")
_MIN_B64 = 40                     # nonce(12)+tag(16)=28 bytes -> 40 base64 chars
_MAX_B64 = 252                    # 255-char chat line cap


def _decrypt_run(run_b64: str, keyed):
    """A base64 run may contain several concatenated tokens. Try increasing prefix
    lengths (multiples of 4) until AES-GCM authenticates one. Returns
    (name, plaintext, prefix) for the first valid token, else None."""
    limit = min(len(run_b64), _MAX_B64)
    length = _MIN_B64
    while length <= limit:
        res = crypto.try_decrypt_sym(run_b64[:length], keyed)
        if res is not None:
            return res[0], res[1], run_b64[:length]
        length += 4
    return None

SEEN_PATH = crypto.CONFIG_DIR / "seen.log"


class SeenStore(set):
    """A `seen` set that persists hashes to disk so a message is never shown twice,
    even across restarts or if it pages into memory after the startup sweep.

    Keyed on ciphertext hash: a genuinely new message has a fresh nonce -> fresh
    hash, so it still surfaces; old/already-shown blobs stay suppressed forever.
    Delete the file to reset the baseline.
    """

    def __init__(self, path=None):
        super().__init__()
        self._path = path or SEEN_PATH
        self._fh = None
        self._lock = threading.Lock()
        try:
            with open(self._path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        super().add(line)
        except FileNotFoundError:
            pass

    def add(self, h):
        # Thread-safe: called from the scanner thread and (to pre-suppress our own
        # outgoing sends) from the UI thread.
        with self._lock:
            if h in self:
                return
            super().add(h)
            try:
                if self._fh is None:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = open(self._path, "a")
                self._fh.write(h + "\n")
                self._fh.flush()
            except OSError:
                pass


# ----------------------------------------------------------------------- search

def _find_rsa_blobs(buf: bytes):
    """Yield (offset, blob_bytes) for every legacy 344-char RSA blob (ends in '==')."""
    pos = buf.find(PAD)
    while pos != -1:
        if pos >= 342:
            body = buf[pos - 342:pos]
            if _B64.issuperset(body):
                yield pos - 342, buf[pos - 342:pos + 2]
        pos = buf.find(PAD, pos + 1)


def _scan_range(reader, start: int, end: int, ctx: dict, seen: set, emit) -> list[int]:
    """Read the whole region [start,end), then decrypt every received (UTF-16) token.

    Reading the region as one buffer avoids chunk-boundary bugs. Only UTF-16 tokens
    are scanned -- those are messages rendered in chat (incoming). Our own outgoing
    sends are ledgered before they render (app pre_send), so they're skipped here.
    Returns [start] if the region held any new message (used for hot-region tracking).
    """
    keyed = ctx.get("keyed") or []
    if not keyed:
        return []
    reasm = ctx.get("reasm")
    # Read the region whole (skip unreadable pages so a hole can't truncate it).
    data = bytearray()
    addr = start
    while addr < end:
        buf = reader.read(addr, min(CHUNK, end - addr))
        if not buf:
            addr += 4096
            continue
        data += buf
        addr += len(buf)
    if not data:
        return []
    found = False
    for m in _MARK_UTF16.finditer(data):
        run = _RUN_UTF16.match(data, m.end())
        if not run:
            continue
        marker = "HX" + chr(data[m.start() + 4])      # 'HX1' single or 'HX2' chunk
        body = run.group()[::2].decode("ascii", "ignore")
        res = _decrypt_run(body, keyed)
        if res is None:
            continue
        h = token_hash(res[2])
        if h in seen:
            continue
        seen.add(h)
        found = True
        # add_decrypted handles single (emit now) and chunk (emit when complete)
        out = reasm.add_decrypted(res[0], marker, res[1]) if reasm else (res[0], res[1])
        if out is not None:
            emit(out[0], out[1], True)
    return [start] if found else []


# ------------------------------------------------------------------- public API

def _build_ctx() -> dict:
    """Load whatever keys we have, plus a reassembler for multi-part messages."""
    ctx = {"keyed": crypto.loaded_psks(), "priv": None, "reasm": crypto.Reassembler()}
    try:
        ctx["priv"] = crypto.load_privkey()
    except Exception:
        pass
    return ctx


def _sweep(pid: int, ctx: dict, seen, emit, max_region, workers: int) -> list:
    """Full sweep of all writable regions, returning regions that held messages.

    Reads regions in parallel (each worker gets its own memory reader). The read
    syscalls release the GIL, so this gives a real speedup over a serial sweep and
    cuts discovery latency -- the main cause of missed/delayed messages.
    """
    with memio.open_reader(pid) as r0:
        regions = list(r0.regions(max_region))
    if workers <= 1 or len(regions) <= 1:
        fresh = []
        with memio.open_reader(pid) as r:
            for start, end in regions:
                if _scan_range(r, start, end, ctx, seen, emit):
                    fresh.append((start, end))
        return fresh
    # Balance workers by total bytes (largest-region-first bin packing).
    buckets = [[] for _ in range(workers)]
    loads = [0] * workers
    for start, end in sorted(regions, key=lambda se: se[1] - se[0], reverse=True):
        i = loads.index(min(loads))
        buckets[i].append((start, end))
        loads[i] += end - start
    fresh: list = []
    lock = threading.Lock()

    def work(group):
        local = []
        try:
            with memio.open_reader(pid) as r:
                for start, end in group:
                    if _scan_range(r, start, end, ctx, seen, emit):
                        local.append((start, end))
        except OSError:
            pass
        with lock:
            fresh.extend(local)

    threads = [threading.Thread(target=work, args=(g,), daemon=True)
               for g in buckets if g]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return fresh


def scan_once(pid: int, ctx: dict | None = None) -> list[tuple[str, str]]:
    """One full sweep (used by the CLI/tests). Return [(sender, plaintext), ...]."""
    if ctx is None:
        ctx = _build_ctx()
    out: list[tuple[str, str]] = []
    seen: set = set()
    with memio.open_reader(pid) as reader:
        for start, end in reader.regions():
            _scan_range(reader, start, end, ctx, seen,
                        lambda sender, pt, incoming, o=out: o.append((sender or "?", pt)))
    return out


def mark_all_seen(pid: int | None = None) -> int:
    """Record every message currently in the client's memory into the seen ledger
    without showing it. Run once (with the in-game chat open so history is resident)
    to bake the existing backlog into the baseline. Returns how many were newly added.
    """
    if pid is None:
        pid = find_client_pid()
    if pid is None:
        return 0
    store = SeenStore()
    ctx = _build_ctx()
    added = 0

    def sink(_sender, _text, _incoming):
        nonlocal added
        added += 1

    before = len(store)
    with memio.open_reader(pid) as reader:
        for start, end in reader.regions():
            _scan_range(reader, start, end, ctx, store, sink)
    return len(store) - before


def watch(on_message, interval=0.2, sweep_interval=3.0, max_region=None,
          on_ready=None, on_disconnect=None, seen=None, workers=4, pid_getter=find_client_pid, stop=None):
    """Two-tier poll loop. Calls on_message(sender, plaintext) for each new *received*
    message (deduped forever by the persistent ledger).

    A full sweep every `sweep_interval` s locates which memory regions hold messages;
    between sweeps, those whole regions are re-scanned every `interval` s. Scanning the
    entire region (not a fixed window) tracks the JVM relocating incoming chat strings,
    so received messages surface in well under a second once their region is known. Hot
    regions accumulate across sweeps so the chat buffer stays hot.

    Only INCOMING (UTF-16) messages are surfaced; ASCII tokens are your own outgoing
    messages / reloaded command history and are recorded-but-not-shown (the overlay
    echoes your sends itself). `seen` is a shared SeenStore so the UI can pre-suppress
    your own sends. `on_ready()` fires after the first sweep completes.
    """
    ctx = _build_ctx()
    if seen is None:
        seen = SeenStore()
    hot: list[tuple[int, int]] = []      # (start, end) regions that have held messages
    pid = None
    last_sweep = 0.0
    ready_sent = False

    def emit(sender, pt, incoming):
        if incoming:                      # show received messages only
            on_message(sender, pt)

    while stop is None or not stop.is_set():
        if pid is None:
            if ready_sent and on_disconnect:
                on_disconnect()
                ready_sent = False
            pid, hot = pid_getter(), []
            if pid is None:
                time.sleep(interval)
                continue
        try:
            now = time.monotonic()
            if not hot or (now - last_sweep) >= sweep_interval:
                fresh = _sweep(pid, ctx, seen, emit, max_region, workers)
                # Accumulate hot regions (keep the chat buffer hot across sweeps).
                hot = list(dict.fromkeys(hot + fresh))[-MAX_HOT_REGIONS:]
                last_sweep = now
                if not ready_sent and on_ready:
                    on_ready()
                    ready_sent = True
            else:
                with memio.open_reader(pid) as reader:
                    for start, end in hot:
                        if (end - start) <= HOT_REGION_CAP:
                            _scan_range(reader, start, end, ctx, seen, emit)
                    if not reader.alive():
                        pid = None
        except OSError:
            pid = None           # process died or access lost; re-resolve next loop
        time.sleep(interval)


if __name__ == "__main__":
    import sys

    p = find_client_pid()
    if p is None:
        sys.exit("HytaleClient not running")
    print(f"Scanning HytaleClient pid={p} ...", file=sys.stderr)
    hits = scan_once(p)
    if not hits:
        print("(no decryptable messages found in memory)")
    for sender, pt in hits:
        print(f"[{sender}] {pt}")
