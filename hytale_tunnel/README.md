# hytale-tunnel

A private encrypted-chat tunnel layered over Hytale's public `/msg` whisper.
You and a friend exchange RSA-encrypted blobs through normal `/msg`; staff who
read the chat see only base64. This overlay reads incoming blobs out of the game
client's memory, decrypts them, and shows the plaintext in an always-on-top
window — and lets you type a reply that gets encrypted and auto-typed back in.

It reuses your existing `hytalecrypt` keys (`~/.hytalecrypt/`) and the exact same
RSA-2048 / OAEP-SHA256 wire format, so it interoperates with the manual CLI flow.

## Crypto: AES-256-GCM with a shared key (not RSA)

Hytale caps a chat message at **255 chars**, but an RSA-2048 blob is always 344
chars — it does not fit, full stop. So the tunnel uses **AES-256-GCM with a
per-friend pre-shared key** (exchanged out of band, like the RSA keys were). A
short message becomes a ~80–180 char token, well within the limit, and the
auth tag makes a successful decrypt proof the message is genuine and for you.
Per-friend keys also let the scanner report **who** a message is from.

Wire token: `HX1` + base64(`nonce(12) || ciphertext+tag`). Set up once with
`hytalecrypt genkey` (share the output) + `hytalecrypt setkey <name> <key>` on
both sides. (The legacy RSA `encrypt`/`decrypt` commands still exist but their
blobs are too long for this server.)

## How it works

- **Receive:** the scanner reads the `HytaleClient` process memory, finds `HX1…`
  tokens, and AEAD-decrypts each against your shared keys. A successful decrypt
  identifies the message *and* the sender. No chat log (Hytale has none), no
  client mod (Hytale has no client modding API — server-side Java plugins only).
- **Two-tier scan:** the client has ~11 GB of writable memory (a full sweep is
  ~11 s). A periodic full sweep locates the chat buffer; between sweeps only a
  few MB around the last hit are re-scanned, so replies appear sub-second.
- **Send:** your text is encrypted to the recipient, then `wtype` opens chat and
  types `/msg <friend> <token>` into the focused client window.

## Run

```
hytale-tunnel                      # recipient = first imported friend
hytale-tunnel -r PlayerB           # pick recipient
hytale-tunnel --open-key Return    # key that opens in-game chat (tune per keybind)
hytale-tunnel --sweep 15           # how often to re-discover new chat regions
```

### What it shows (incoming vs outgoing)

Messages **received** from other players are stored as UTF-16 strings; your own
outgoing `/msg` lines are stored as ASCII (command history). The scanner only
surfaces the **UTF-16 (incoming)** ones — your own sends are echoed by the overlay
itself (and pre-suppressed in the ledger), so they never double-show. This is what
makes private whispers *and* public chat both appear, while the reloaded command
history is ignored.

`--sweep` controls how often a full sweep re-runs to discover a chat region the
scanner hasn't mapped yet (~11 s read). Once a region is known it stays "hot" and
is re-scanned every `--interval` (0.25 s), so messages there surface sub-second.

### The seen ledger (why messages don't repeat)

Every shown message's hash is written to `~/.hytalecrypt/seen.log` so it stays
suppressed across restarts. A genuinely new message has a fresh nonce → fresh hash,
so it still shows. Delete the file for a clean slate (resident chat will show once).

Optionally bake the current in-memory messages into the ledger (so they won't show
at next launch):

```
hytale-tunnel --mark-seen     # records all in-memory messages as seen, then exits
```

Delete `~/.hytalecrypt/seen.log` to reset and see everything again.

Hyprland integration (pin/float/position the overlay, add a focus hotkey):

```
# ~/.config/hypr/hyprland.conf
source = ~/.local/lib/hytale_tunnel/hyprland.conf
```

After editing the file run `hyprctl reload` to apply it (and to drop any old
`size`/`nofocus` rules from earlier versions).

**`Esc` collapses the overlay to a small always-on-top pill** (it shows a `● N`
badge for unread messages); click the pill to expand. `Esc` only fires while the
overlay itself is focused, though — to collapse/expand **while the game is
focused**, use the global keybind in `hyprland.conf`
(`SUPER+SHIFT+J` by default), which signals the running tunnel. Pick a combo that
doesn't clash with your setup — JaKooLit binds a lot of `SUPER` keys; check with
`hyprctl binds | grep -iE 'SUPER'`. On Windows there's no global hotkey; just click
the pill.

## Tuning the send key

`--open-key` is the in-game key that opens the chat/command box. Common values:
`Return` (default), `t`, or `slash` (for a `/`-prefixed command box — if you use
this, the leading `/` of `/msg` may be doubled; adjust to taste). Find Hytale's
chat keybind and match it.

## Set up a shared key

```
hytalecrypt genkey                 # prints a key; send it to your friend on Discord
hytalecrypt setkey Revenir <key>   # BOTH of you store the SAME key under each other's name
```
A `self` key is handy for solo testing: `hytalecrypt setkey self $(hytalecrypt genkey | sed -n 2p)`.

## Windows (same overlay, different backend)

The tunnel is cross-platform. `crypto`, `overlay`, and `app` are shared; only the
OS plumbing differs and is selected automatically by `sys.platform`:

| concern        | Linux                                   | Windows                          |
|----------------|-----------------------------------------|----------------------------------|
| receive        | **quiche eBPF hook** — flawless, instant (`receiver_quiche`) | **memory scan** — best-effort, may miss fast/transient messages (`memscan` + `memio_win`) |
| send input     | `wtype` (type) / `ydotool` (paste)      | `SendInput` (`send_win`)         |
| always-on-top  | Hyprland window rules                   | native Qt `WindowStaysOnTop`     |

The Linux receive is exact (hooks quiche's decrypt). There is no eBPF on Windows,
so Windows falls back to scanning the client's memory for messages — it works but
can occasionally miss a message that flickers through memory. A flawless Windows
receive would hook `quiche_conn_stream_recv` in `libquiche.dll` (e.g. via
MinHook/Detours) — not built yet.

### Setup

1. Install Python 3 from **python.org** (tick **Add python.exe to PATH** — this also
   installs the `py` launcher the tools prefer). Avoid the Microsoft Store "python":
   it leaves a stub `python.exe` that just opens the Store. If you already have it,
   the bundled launchers look past the stub and find a real Python automatically.
2. Copy the whole share folder (the `hytale_tunnel` package, the `hytalecrypt`
   script, and `setup-windows.bat`) somewhere, e.g. `C:\hytale\`.
3. Double-click **`setup-windows.bat`** once — it installs `pyqt6` + `cryptography`.
4. Exchange a shared key over Discord and both run
   `py C:\hytale\hytalecrypt setkey <theirname> <key>` (or `python …`).
5. Start the overlay by double-clicking **`hytale_tunnel\hytale-tunnel.bat`**
   (pass `-r <friend>` to pick the recipient), or from a terminal:
   `cd C:\hytale && py -m hytale_tunnel -r <friend>`.

> **Borderless/windowed only:** the always-on-top overlay can't draw over a game in
> *exclusive* fullscreen. Set Hytale to **borderless windowed** so the pill stays
> visible (same as any Discord/Steam overlay).

**Sending**: `SendInput` is real input on Windows, so `--paste-method ctrl-v`
(default) should paste instantly. If it doesn't land, use `--paste-method type`.
**Reading memory**: if `OpenProcess` fails, run the terminal **as Administrator**.
Anti-cheat is more likely to notice memory reads/synthetic input on Windows.

> Status: the Windows backend has now been run end-to-end on a real Windows 11 +
> Hytale client — process discovery, the writable-region walk, `ReadProcessMemory`,
> the UTF-16 token scan + AES-GCM decrypt, window focus, and `SendInput` typing/paste
> were all verified live (a `/msg self` round-trip was decrypted back out of memory).

Windows first-run self-test (no second player): `python hytalecrypt setkey self <key>`,
`python hytalecrypt senc self "memtest123"`, paste the printed `/msg self HX1…` line
in-game, then `python -m hytale_tunnel -r self` → `self: memtest123` should appear.
If `find_game_window` misses, check the client's window title contains "Hytale".

## Verify end-to-end (these steps need your live game)

Component tests (memory read, HX1 find+decrypt, scan loop, sender attribution,
crypto roundtrip) all pass headlessly. The following need you, in-game:

1. **Capture (solo):** `hytalecrypt senc self "memtest123"` → paste the printed
   `/msg self <token>` line in-game (it now fits 255). Start `hytale-tunnel -r self`
   → within ~10 s the overlay shows `self: memtest123`.
2. **Send:** focus the game, `SUPER+M`, type `hello`, Enter → confirm
   `/msg <friend> <token>` is typed and sent.
3. **Live:** with Revenir (who holds the same shared key), exchange messages →
   both directions appear within ~1 s; staff see only `HX1…` tokens.

## Limitations

- **Latency:** first message in a new memory region can take up to one sweep
  (~10 s); subsequent ones are sub-second. Lower `--sweep` to trade CPU for speed.
- **Length:** a message must keep the whole `/msg <name> <token>` line ≤255
  chars (~150 chars of text for a 7-char name); `senc` enforces this and errors
  if you exceed it. Longer messages would need splitting (not yet implemented).
- **Shared-key trust:** both sides hold the same symmetric key; anyone with it
  can read and forge messages, so guard it and compare key fingerprints out of
  band. Exchange a fresh key with each friend.
- **Brittleness / anti-cheat:** external memory reads + synthetic keystrokes can
  break on game updates and carry inherent risk.
```
