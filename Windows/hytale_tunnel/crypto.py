"""Shared crypto + keystore for HytaleCrypt / hytale-tunnel.

Wire format is identical to the original hytalecrypt CLI: RSA-2048, OAEP padding
with MGF1+SHA256. Ciphertext is always exactly 256 bytes, so its base64 form is
always exactly 344 chars ending in '==' -- the signature the memory scanner uses.
"""

import base64
import hashlib
import os
import threading
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONFIG_DIR = Path.home() / ".hytalecrypt"
MY_PRIV_PATH = CONFIG_DIR / "mykey.pem"
MY_PUB_PATH = CONFIG_DIR / "mykey.pub"
FRIENDS_DIR = CONFIG_DIR / "friends"
# Party/group keys live here. Unlike per-friend keys (one shared secret per pair), a
# group key is one secret shared by EVERY member of a party so a single '/p' ciphertext
# decrypts for all of them. Same 32-byte AES-256-GCM format as a friend key.
GROUPS_DIR = CONFIG_DIR / "groups"

# RSA-2048 ciphertext is exactly 256 bytes -> base64 is exactly 344 chars.
CIPHERTEXT_LEN = 256
BLOB_LEN = 344

_OAEP = padding.OAEP(
    mgf=padding.MGF1(algorithm=hashes.SHA256()),
    algorithm=hashes.SHA256(),
    label=None,
)


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FRIENDS_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)


def load_privkey(path: Path = MY_PRIV_PATH) -> RSAPrivateKey:
    if not path.exists():
        raise FileNotFoundError(f"No private key at {path}; run: hytalecrypt gen")
    return serialization.load_pem_private_key(
        path.read_bytes(), password=None, backend=default_backend()
    )


def load_pubkey(path: Path) -> RSAPublicKey:
    return serialization.load_pem_public_key(path.read_bytes(), backend=default_backend())


def friend_pubkey(name: str) -> RSAPublicKey:
    path = FRIENDS_DIR / f"{name}.pub"
    if not path.exists():
        known = ", ".join(list_friends()) or "(none)"
        raise KeyError(f"Unknown friend '{name}'. Known: {known}")
    return load_pubkey(path)


def list_friends() -> list[str]:
    if not FRIENDS_DIR.exists():
        return []
    return sorted(p.stem for p in FRIENDS_DIR.glob("*.pub"))


def fingerprint(pub: RSAPublicKey) -> str:
    """Short, key-distinguishing fingerprint: SHA256 over the full DER key.

    (The old scheme base64'd the first 16 DER bytes -- the ASN.1 header, which is
    identical for every RSA-2048 key, so it could not tell keys apart.)
    """
    der = pub.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()[:8]
    return ":".join(f"{b:02x}" for b in digest)


def pubkey_to_b64(pub: RSAPublicKey) -> str:
    pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(pem).decode()


def b64_to_pubkey(b64: str) -> RSAPublicKey:
    return serialization.load_pem_public_key(base64.b64decode(b64), backend=default_backend())


def encrypt_for(friend: str, message: str) -> str:
    """Encrypt `message` for `friend`; return base64 blob ready for /msg."""
    pub = friend_pubkey(friend)
    ciphertext = pub.encrypt(message.encode("utf-8"), _OAEP)
    return base64.b64encode(ciphertext).decode()


def decrypt_blob(b64_blob: str, priv: RSAPrivateKey | None = None) -> str:
    """Decrypt a blob with our private key. Raises on any failure."""
    if priv is None:
        priv = load_privkey()
    ciphertext = base64.b64decode(b64_blob)
    return priv.decrypt(ciphertext, _OAEP).decode("utf-8")


def try_decrypt(b64_blob: str | bytes, priv: RSAPrivateKey | None = None) -> str | None:
    """Best-effort decrypt for the scan loop. Returns plaintext or None on any failure.

    A successful OAEP decrypt is itself proof the blob was addressed to us, so this
    doubles as the relevance filter while scanning process memory.
    """
    if priv is None:
        priv = load_privkey()
    try:
        if isinstance(b64_blob, bytes):
            b64_blob = b64_blob.decode("ascii")
        ciphertext = base64.b64decode(b64_blob, validate=True)
        if len(ciphertext) != CIPHERTEXT_LEN:
            return None
        return priv.decrypt(ciphertext, _OAEP).decode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Symmetric mode (AES-256-GCM, pre-shared key per friend).
#
# RSA-2048 blobs are 344 chars and don't fit Hytale's 255-char chat limit, so the
# tunnel uses a shared key instead. Wire token = "HX1" + base64(nonce || ct||tag).
# A per-friend key also lets the scanner attribute who a message is from. The key
# is exchanged out of band (Discord), same as RSA public keys.
# ---------------------------------------------------------------------------

SYM_MARKER = "HX1"            # single message: "HX1" + base64(nonce||ct)
CHUNK_MARKER = "HX2"         # part of a multi-message: payload = 8-char header + chunk
NONCE_LEN = 12
KEY_LEN = 32                  # AES-256
_TAG_LEN = 16
CHAT_LIMIT = 255             # Hytale max chars for a single chat message
_CHUNK_HEADER = 8            # msgid(4 hex) + part(2 hex) + total(2 hex)


def split_public_lines(line: str, limit: int = CHAT_LIMIT) -> list[str]:
    """Split an UNENCRYPTED chat line so each piece fits Hytale's CHAT_LIMIT.

    Sending a longer-than-limit line gets you kicked (the server rejects the oversized
    chat frame), so plain/public messages must be split just like encrypted ones are
    chunked. Splits on word boundaries (hard-splitting a single over-long word), and
    preserves a leading '/msg <name> ' command prefix on every piece so a long unencrypted
    whisper stays a whisper across all its parts (rather than leaking the tail to public)."""
    if len(line) <= limit:
        return [line]
    prefix, body = "", line
    if line.startswith("/msg "):
        sp = line.find(" ", len("/msg "))
        if sp != -1:
            prefix, body = line[:sp + 1], line[sp + 1:]   # "/msg <name> " + rest
    budget = max(1, limit - len(prefix))
    return [prefix + piece for piece in _wrap_words(body, budget)]


def _wrap_words(text: str, budget: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for word in text.split(" "):
        while len(word) > budget:                    # a single word longer than a whole line
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:budget])
            word = word[budget:]
        candidate = word if not cur else cur + " " + word
        if len(candidate) <= budget:
            cur = candidate
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out or [""]


def _psk_path(name: str) -> Path:
    return FRIENDS_DIR / f"{name}.key"


def gen_psk() -> str:
    """Return a fresh random shared key as base64 (share with your one friend)."""
    return base64.b64encode(os.urandom(KEY_LEN)).decode()


def set_psk(name: str, b64key: str) -> Path:
    """Store a shared key for `name`. Raises ValueError if it isn't 32 bytes."""
    raw = base64.b64decode(b64key.strip())
    if len(raw) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes ({KEY_LEN*8}-bit), got {len(raw)}")
    ensure_dirs()
    p = _psk_path(name)
    p.write_text(base64.b64encode(raw).decode())
    p.chmod(0o600)
    return p


def load_psk(name: str) -> bytes | None:
    p = _psk_path(name)
    if not p.exists():
        return None
    try:
        raw = base64.b64decode(p.read_text().strip())
        return raw if len(raw) == KEY_LEN else None
    except Exception:
        return None


def list_psk_friends() -> list[str]:
    if not FRIENDS_DIR.exists():
        return []
    return sorted(p.stem for p in FRIENDS_DIR.glob("*.key"))


def loaded_psks() -> list[tuple[str, bytes]]:
    """All (name, key) pairs we hold shared keys for."""
    out = []
    for name in list_psk_friends():
        k = load_psk(name)
        if k is not None:
            out.append((name, k))
    return out


def key_fingerprint(raw: bytes) -> str:
    digest = hashlib.sha256(raw).digest()[:8]
    return ":".join(f"{b:02x}" for b in digest)


# --------------------------------------------------------------------------- /friend
# X25519 key-agreement handshake, carried over the public /msg channel.
#
# Each user has ONE long-term X25519 keypair. To befriend someone we exchange public
# keys (via /msg); both sides then compute the SAME shared secret with X25519 ECDH --
# an eavesdropper who logs both /msg lines sees only two public keys and CANNOT derive
# the secret. The shared secret is HKDF-stretched into the 32-byte AES-256 key the
# normal HX1/HX2 message crypto already uses, so no other code path changes.
#
# Trust-on-first-use: there is no PKI, so an ACTIVE man-in-the-middle who swaps the
# keys mid-handshake is not detected automatically. Both sides are shown the resulting
# key fingerprint so they can compare it out-of-band (voice/Discord) if they want that
# guarantee. Passive eavesdropping (the stated threat) is fully prevented.
#
# Wire tokens (plaintext, fit easily in one /msg):
#   "HXK1 <b64(my 32-byte X25519 pubkey)>"  -- /friend add   (request)
#   "HXK2 <b64(my 32-byte X25519 pubkey)>"  -- /friend accept (reply)
X25519_PRIV_PATH = CONFIG_DIR / "x25519.key"
PENDING_DIR = CONFIG_DIR / "pending"       # inbound requests + outbound-add markers
HS_ADD = "HXK1"                            # request token marker
HS_ACCEPT = "HXK2"                         # reply token marker
_HS_INFO = b"hytale-tunnel friend v1"      # HKDF context (identical on both sides)


def load_or_create_x25519() -> X25519PrivateKey:
    """Our long-term X25519 private key (generated once, then reused)."""
    ensure_dirs()
    if X25519_PRIV_PATH.exists():
        return serialization.load_pem_private_key(
            X25519_PRIV_PATH.read_bytes(), password=None, backend=default_backend())
    priv = X25519PrivateKey.generate()
    X25519_PRIV_PATH.write_bytes(priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))
    X25519_PRIV_PATH.chmod(0o600)
    return priv


def my_hs_pub_b64() -> str:
    """Our X25519 public key, base64 — the payload of an HXK1/HXK2 token."""
    raw = load_or_create_x25519().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def hs_add_token() -> str:
    return f"{HS_ADD} {my_hs_pub_b64()}"


def hs_accept_token() -> str:
    return f"{HS_ACCEPT} {my_hs_pub_b64()}"


def parse_hs_token(body: str) -> tuple[str, str] | None:
    """If `body` is a handshake line, return (marker, their_pub_b64), else None."""
    parts = body.strip().split()
    if len(parts) == 2 and parts[0] in (HS_ADD, HS_ACCEPT):
        try:
            if len(base64.b64decode(parts[1], validate=True)) == 32:
                return parts[0], parts[1]
        except Exception:                        # noqa: BLE001
            return None
    return None


def derive_friend_key(their_pub_b64: str) -> bytes:
    """The shared AES-256 key from our private key and their X25519 public key.
    Symmetric: both friends compute the identical key."""
    their_pub = X25519PublicKey.from_public_bytes(base64.b64decode(their_pub_b64))
    shared = load_or_create_x25519().exchange(their_pub)
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN,
                salt=None, info=_HS_INFO, backend=default_backend()).derive(shared)


def save_derived_friend_key(name: str, their_pub_b64: str) -> bytes:
    """Derive and persist the shared key for `name`; returns the raw key."""
    key = derive_friend_key(their_pub_b64)
    set_psk(name, base64.b64encode(key).decode())
    return key


def _pending_in_path(name: str) -> Path:
    return PENDING_DIR / f"{name}.in"


def _pending_out_path(name: str) -> Path:
    return PENDING_DIR / f"{name}.out"


def record_incoming_request(name: str, their_pub_b64: str) -> None:
    """Stash an inbound /friend add's pubkey until we run /friend accept <name>."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _pending_in_path(name).write_text(their_pub_b64)


def take_incoming_request(name: str) -> str | None:
    """Pop the stored inbound pubkey for `name` (consumed on accept)."""
    p = _pending_in_path(name)
    if not p.exists():
        return None
    b64 = p.read_text().strip()
    p.unlink()
    return b64


def peek_incoming_request(name: str) -> str | None:
    p = _pending_in_path(name)
    return p.read_text().strip() if p.exists() else None


def list_incoming_requests() -> list[str]:
    if not PENDING_DIR.exists():
        return []
    return sorted(p.stem for p in PENDING_DIR.glob("*.in"))


def record_outgoing_request(name: str) -> None:
    """Mark that WE sent an add to `name`, so their HXK2 reply is expected (not spoofed)."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _pending_out_path(name).write_text("1")


def has_outgoing_request(name: str) -> bool:
    return _pending_out_path(name).exists()


def clear_outgoing_request(name: str) -> None:
    p = _pending_out_path(name)
    if p.exists():
        p.unlink()


def remove_friend(name: str) -> bool:
    """Delete every trace of a friend (shared key, RSA pubkey, pending markers)."""
    removed = False
    for p in (_psk_path(name), FRIENDS_DIR / f"{name}.pub",
              _pending_in_path(name), _pending_out_path(name)):
        if p.exists():
            p.unlink()
            removed = True
    return removed


# --------------------------------------------------------------------------- groups
# A party/group key is the same 32-byte AES key as a friend key, but stored in
# GROUPS_DIR and shared by EVERY member of the party (so one '/p' ciphertext decrypts
# for all of them). Attribution of who sent a group message comes from the chat frame
# (the sender's name), not from the key -- everyone in the party holds the same key.

def _group_path(name: str) -> Path:
    return GROUPS_DIR / f"{name}.key"


def set_group_psk(name: str, b64key: str) -> Path:
    """Store a party group key. Raises ValueError if it isn't 32 bytes."""
    raw = base64.b64decode(b64key.strip())
    if len(raw) != KEY_LEN:
        raise ValueError(f"key must be {KEY_LEN} bytes ({KEY_LEN*8}-bit), got {len(raw)}")
    ensure_dirs()
    p = _group_path(name)
    p.write_text(base64.b64encode(raw).decode())
    p.chmod(0o600)
    return p


def load_group_psk(name: str) -> bytes | None:
    p = _group_path(name)
    if not p.exists():
        return None
    try:
        raw = base64.b64decode(p.read_text().strip())
        return raw if len(raw) == KEY_LEN else None
    except Exception:
        return None


def list_groups() -> list[str]:
    if not GROUPS_DIR.exists():
        return []
    return sorted(p.stem for p in GROUPS_DIR.glob("*.key"))


def loaded_group_psks() -> list[tuple[str, bytes]]:
    """All (group_name, key) pairs we hold party keys for."""
    out = []
    for name in list_groups():
        k = load_group_psk(name)
        if k is not None:
            out.append((name, k))
    return out


def all_decrypt_keys() -> list[tuple[str, bytes]]:
    """Every shared key we can decrypt an incoming token with: per-friend keys first,
    then party group keys. AES-GCM authentication means only the right key validates,
    so trying all of them can't mis-attribute (a wrong key just fails)."""
    return loaded_psks() + loaded_group_psks()


PARTY_PREFIX = "/p chat "     # the in-game party-chat command we type before a token


def _budget_for_prefix(prefix_len: int) -> int:
    """Largest plaintext (bytes) whose '<prefix><token>' line fits CHAT_LIMIT."""
    token_budget = CHAT_LIMIT - prefix_len
    b64_budget = token_budget - len(SYM_MARKER)
    payload_bytes = (b64_budget // 4) * 3            # base64 expands 3->4
    return max(0, payload_bytes - NONCE_LEN - _TAG_LEN)


def max_sym_plaintext(name: str) -> int:
    """Largest message (bytes) whose '/msg <name> <token>' line fits CHAT_LIMIT."""
    return _budget_for_prefix(len(f"/msg {name} "))


def max_party_plaintext() -> int:
    """Largest message (bytes) whose '/p <token>' party line fits CHAT_LIMIT."""
    return _budget_for_prefix(len(PARTY_PREFIX))


def encrypt_sym(name: str, message: str) -> str:
    """Encrypt `message` to friend `name`'s shared key; return the 'HX1...' token."""
    key = load_psk(name)
    if key is None:
        raise KeyError(f"No shared key for '{name}'. Run: hytalecrypt setkey {name} <key>")
    data = message.encode("utf-8")
    limit = max_sym_plaintext(name)
    if len(data) > limit:
        raise ValueError(f"message too long: {len(data)} bytes > {limit} "
                         f"(255-char chat limit for /msg {name})")
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, data, None)
    return SYM_MARKER + base64.b64encode(nonce + ct).decode()


def _decrypt_sym_one(token_b64: str, key: bytes) -> str | None:
    try:
        raw = base64.b64decode(token_b64, validate=True)
        if len(raw) < NONCE_LEN + _TAG_LEN:
            return None
        nonce, ct = raw[:NONCE_LEN], raw[NONCE_LEN:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception:
        return None


def try_decrypt_sym(token_b64: str | bytes,
                    keyed: list[tuple[str, bytes]] | None = None) -> tuple[str, str] | None:
    """Try every shared key against a candidate token (marker already stripped).

    Returns (friend_name, plaintext) on the first AEAD success, else None. The
    auth tag makes false positives effectively impossible, so success identifies
    both that it's for us and which friend's key it was.
    """
    if isinstance(token_b64, bytes):
        try:
            token_b64 = token_b64.decode("ascii")
        except UnicodeDecodeError:
            return None
    if keyed is None:
        keyed = all_decrypt_keys()
    for name, key in keyed:
        pt = _decrypt_sym_one(token_b64, key)
        if pt is not None:
            return name, pt
    return None


# --------------------------------------------------------------------------- chunking
# A message longer than one /msg can hold is split into N parts, each its own token
# ("HX2" + base64(nonce || AESGCM(header||chunk))). The header is 8 hex chars:
# msgid(4) | part(2) | total(2). The receiver reassembles by msgid -> one message.

def _split_utf8(text: str, max_bytes: int) -> list[str]:
    """Split `text` into pieces each <= max_bytes when UTF-8 encoded (no split chars)."""
    out, cur, cur_b = [], "", 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur and cur_b + b > max_bytes:
            out.append(cur)
            cur, cur_b = "", 0
        cur += ch
        cur_b += b
    if cur:
        out.append(cur)
    return out


def _encrypt_messages_core(key: bytes, budget: int, message: str) -> list[str]:
    """Encrypt `message` under `key` into one or more tokens that each fit `budget`.

    A message that fits is a single 'HX1...' token; a longer one is split into 'HX2...'
    chunk tokens the receiver reassembles. `budget` is the max plaintext bytes per token
    (depends on the send prefix: '/msg <name> ' for whispers, '/p ' for party).
    """
    data = message.encode("utf-8")
    if len(data) <= budget:
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(key).encrypt(nonce, data, None)
        return [SYM_MARKER + base64.b64encode(nonce + ct).decode()]
    chunk_max = max(1, budget - _CHUNK_HEADER)
    chunks = _split_utf8(message, chunk_max)
    if len(chunks) > 255:
        raise ValueError("message too long even when chunked")
    msgid = int.from_bytes(os.urandom(2), "big")
    total = len(chunks)
    tokens = []
    for i, chunk in enumerate(chunks):
        payload = f"{msgid:04x}{i:02x}{total:02x}{chunk}".encode("utf-8")
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(key).encrypt(nonce, payload, None)
        tokens.append(CHUNK_MARKER + base64.b64encode(nonce + ct).decode())
    return tokens


def encrypt_messages(name: str, message: str) -> list[str]:
    """Return one or more ready-to-send '/msg <name>' tokens for `message`.

    A short message is a single 'HX1...' token; a long one is split into 'HX2...'
    chunk tokens that the receiver reassembles into one message.
    """
    key = load_psk(name)
    if key is None:
        raise KeyError(f"No shared key for '{name}'. Run: hytalecrypt setkey {name} <key>")
    return _encrypt_messages_core(key, max_sym_plaintext(name), message)


def encrypt_group_messages(group: str, message: str) -> list[str]:
    """Return one or more ready-to-send '/p' party tokens for `message`, encrypted with
    the shared party group key (so every member of the party can decrypt it)."""
    key = load_group_psk(group)
    if key is None:
        raise KeyError(f"No party key for '{group}'. "
                       f"Run: hytalecrypt setgroupkey {group} <key>")
    return _encrypt_messages_core(key, max_party_plaintext(), message)


class Reassembler:
    """Decrypts incoming tokens and reassembles multi-part messages.

    feed(token, keys) returns (sender, full_message) when a complete message is
    ready (immediately for single 'HX1' tokens), else None.
    """

    def __init__(self):
        self._parts: dict = {}     # (sender, msgid) -> {int part: chunk, 'total': n}
        self._lock = threading.Lock()

    def feed(self, token: str, keyed=None):
        """Decrypt a raw token and reassemble. Returns (sender, message) or None."""
        if token[:3] in (SYM_MARKER, CHUNK_MARKER):
            marker, body = token[:3], token[3:]
        else:
            marker, body = SYM_MARKER, token
        res = try_decrypt_sym(body, keyed)
        if res is None:
            return None
        return self.add_decrypted(res[0], marker, res[1])

    def add_decrypted(self, sender: str, marker: str, payload: str):
        """Feed an already-decrypted (marker, payload). Returns (sender, message) or None."""
        if marker != CHUNK_MARKER:
            return sender, payload
        if len(payload) < _CHUNK_HEADER:
            return None
        try:
            msgid = payload[:4]
            part = int(payload[4:6], 16)
            total = int(payload[6:8], 16)
        except ValueError:
            return None
        with self._lock:
            slot = self._parts.setdefault((sender, msgid), {})
            slot[part] = payload[_CHUNK_HEADER:]
            slot["total"] = total
            if sum(1 for k in slot if isinstance(k, int)) >= total:
                full = "".join(slot.get(i, "") for i in range(total))
                del self._parts[(sender, msgid)]
                return sender, full
        return None
