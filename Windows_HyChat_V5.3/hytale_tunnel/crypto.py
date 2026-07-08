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
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CONFIG_DIR = Path.home() / ".hytalecrypt"
MY_PRIV_PATH = CONFIG_DIR / "mykey.pem"
MY_PUB_PATH = CONFIG_DIR / "mykey.pub"
FRIENDS_DIR = CONFIG_DIR / "friends"

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


def max_sym_plaintext(name: str) -> int:
    """Largest message (bytes) whose '/msg <name> <token>' line fits CHAT_LIMIT."""
    prefix = len(f"/msg {name} ")
    token_budget = CHAT_LIMIT - prefix
    b64_budget = token_budget - len(SYM_MARKER)
    payload_bytes = (b64_budget // 4) * 3            # base64 expands 3->4
    return max(0, payload_bytes - NONCE_LEN - _TAG_LEN)


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
        keyed = loaded_psks()
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


def encrypt_messages(name: str, message: str) -> list[str]:
    """Return one or more ready-to-send tokens for `message`.

    A short message is a single 'HX1...' token (unchanged). A long one is split
    into 'HX2...' chunk tokens that the receiver reassembles into one message.
    """
    if len(message.encode("utf-8")) <= max_sym_plaintext(name):
        return [encrypt_sym(name, message)]
    key = load_psk(name)
    if key is None:
        raise KeyError(f"No shared key for '{name}'. Run: hytalecrypt setkey {name} <key>")
    chunk_max = max(1, max_sym_plaintext(name) - _CHUNK_HEADER)
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
