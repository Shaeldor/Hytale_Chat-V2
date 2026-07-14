# Hytale Tunnel — encrypted in-game chat overlay

An end-to-end **encrypted chat channel that rides on top of Hytale's own chat**, plus a rich
always-on-top **overlay** that mirrors and beautifies the in-game chat. Two players who share a key
can whisper (or run a party group) with messages that travel through the game server as ordinary —
but AES-encrypted — chat text; nobody without the key (server included) can read them. The overlay
adds GIFs, emoji, a friends system, per-message colours, and a spam/noise filter.

This document describes the **final, current** behaviour and the **Linux (Hyprland) reference
implementation**, then explains how to reproduce every piece — with the **same behaviour** — on
**Windows**. It is written so another developer or AI can rebuild the whole thing. Throughout,
`file:function` pointers show exactly where each behaviour is solved in the Linux code.

> Nothing here modifies the game or its files. The tunnel only **reads** the chat the game already
> received and **writes** chat the way a player would. All crypto is client-side; the wire only ever
> carries opaque base64.

---

## 1. Architecture (the three real subsystems)

```
   ┌─────────────┐   encrypted token in normal chat   ┌─────────────┐
   │  YOUR game  │ ─────────────────────────────────► │ FRIEND game │
   └─────┬───────┘                                    └──────┬──────┘
     RECEIVE (read incoming chat, decrypt our tokens)        │
         ▼                                                   ▼
   ┌───────────────┐        Msg objects            ┌───────────────────┐
   │ receiver_     │ ────────────────────────────► │     overlay.py    │
   │ quiche (eBPF) │                               │  (PyQt6 overlay)  │
   └───────────────┘                               └─────────┬─────────┘
         ▲  SEND (encrypt + inject onto the game's QUIC stream)   │ you type a line
         └───────────────────────────────────────────────────────┘
```

The current Linux implementation is:

- **Receive** — an **eBPF/bpftrace probe** on `quiche_conn_stream_recv` captures every chat-log
  buffer at quiche's decrypt boundary, in the server's canonical order, with the real sender. Runs as
  root; the capture helper self-terminates with the app.
  → `receiver_quiche.py:watch`, launched helper `quiche_capture.py`.
- **Send** — by default the encrypted line is **injected straight onto the game's outbound QUIC
  stream via ptrace** (no chatbox typing) by an elevated helper; instant.
  → `inject_client.py:Injector` launches `inject.py` (root, via `pkexec`). Fallback: type/paste into
  the chatbox (`--send-method type` → `send.py` → `send_linux.py`).
- **Overlay + crypto** — pure cross-platform Python (`overlay.py`, `crypto.py`, `chatframe.py`,
  `chatfilter.py`, `gif_util.py`, `emoji_util.py`).

> **Obsolete / not part of this design:** an early memory-scanning receive path
> (`memscan.watch`) and the RSA identity CLI. The live tunnel does **not** read game memory and does
> **not** use RSA — it captures the quiche stream and decrypts with X25519-derived AES keys. Ignore
> those when porting. (`memscan` survives only for two utilities: finding the game PID and the
> `--mark-seen` maintenance command.)

`LINUX = sys.platform.startswith("linux")` in `app.py` is the platform switch; only receive/send/
hotkeys differ per OS.

---

## 2. Wire formats (parse these byte-for-byte)

### 2.1 Incoming: the chat-log record (`chatframe.py`)

Every rendered chat line is a sequence of **rich-text runs** (text + colour). On the wire a run is:

```
<4-byte LE textfield length> <8×0xFF anchor> <varint length><UTF-8 text> \x07 # rrggbb
                                              └──── textfield ────┘       └ colour tag ┘
```

- Find every `\x07#rrggbb` **colour tag** (the unambiguous anchor); the run text is the bytes between
  the nearest preceding `0xFF*8` run and the tag, cross-checked against the 4-byte length; strip the
  leading 7-bit varint. → `chatframe.py:parse_runs`.
- The displayed line is the concatenation of run texts (`"[42968] "+"Shaeldor"+": "+"hi"`).
- A chat-log frame is located by its **type signature `d2 00 00 00 01 00 40`**; a single buffer packs
  several, so split on every signature and parse each. → `chatframe.py:parse_all`, `SIG`.
- **Trailing UNCOLOURED field (must-have):** a Discord-relay line `[Discord] <name>: <message>` stores
  the final message with **no colour tag** — just `<0xFF run><varint length><text>` to end of frame.
  Recover it after the last colour tag; guard so it only augments lines that already have colour runs,
  and reject binary (≤2 U+FFFD, ≥90% printable). Skipping this makes Discord messages render blank.
  → `chatframe.py:parse_runs` (the `if runs:` tail block), `_read_len_prefixed`.
- Per-run colours are kept end-to-end (`Msg.name_runs`, `Msg.body_runs`) so names/bodies render with
  the game's real **multi-colour** runs; `""` colour = default text colour.
- `chatframe.py:classify` labels the line `public / party / whisper_in / whisper_out / emote /
  system`. Only the first four + emote are "player" kinds; everything else is "system".

### 2.2 Outgoing: the ptrace-inject frame (`inject.py`)

The client writes a chat line onto QUIC stream 0 as
`[u32 LE body_len][u32 LE 0x000000d3][0x01][LEB128 text_len][UTF-8 text]` (note `0xd3` outbound vs
`0xd2` for the incoming chat-log). The injector reproduces that frame and ptrace-writes it into the
game process. Windows has no ptrace equivalent → use typed/pasted input instead (§8.2).

---

## 3. Cryptography (`crypto.py`)

Secrets live in `~/.hytalecrypt/` (git-ignored). A token must fit the game's **255-char** chat limit;
longer messages chunk and reassemble automatically.

- **Add a friend = X25519 handshake over chat.** Each side has an X25519 key (`x25519.key`). The
  friends panel sends an `HXK1` add-request token (carrying your public key); the other side's panel
  shows it and can send an `HXK2` accept token. Both sides derive the same AES-256 friend key via
  X25519 + HKDF (context `hytale-tunnel friend v1`) and save it as a per-friend PSK
  (`friends/<name>.key`). → `crypto.py:hs_add_token / hs_accept_token / derive_friend_key /
  save_derived_friend_key`.
- **Party** = one shared AES-256 group key (`groups/<name>.key`), set once for everyone.
- **Message tokens:** `HX1`+base64(`nonce(12) || AES-256-GCM ct`) for a single message; `HX2`+
  base64(`nonce || AESGCM(header||chunk)`) for a chunk (header = `msgid(4)+part(2)+total(2)` hex,
  reassembled per `(sender, msgid)`). Private = `/msg <friend> HX1…`; party = `/p chat HX1…`
  (`PARTY_PREFIX`). → `crypto.py:encrypt_sym / encrypt_messages / try_decrypt_sym / all_decrypt_keys`.
- The receive path only ever tries **symmetric** decryption of `HX1`/`HX2` tokens against your friend
  + party keys. → `receiver_quiche.py:_build_msg`.
- The `hytalecrypt` CLI can set keys by hand (`genkey/setkey`, `gengroupkey/setgroupkey`,
  `senc/sdec`); the panel automates the common case. (The CLI also has an older RSA identity path —
  not used by the live tunnel.)

**Windows:** copy `crypto.py` verbatim (pure `cryptography`); `Path.home()` already targets
`%USERPROFILE%\.hytalecrypt`.

---

## 4. The overlay window — states & exact behaviour (`overlay.py`)

The overlay is a frameless, translucent, always-on-top **tool window** titled `hytale-tunnel`
(`Overlay.__init__`). It has three states; transitions are the heart of the UX.

### 4.1 The three states

- **PASSIVE** (expanded, but the game has keyboard focus) — the default while you play. No chrome
  (no header/buttons): just the most recent lines floating over one shared translucent panel, each
  line lingering 8 s then fading out over 0.9 s on its own timer, so old chat disappears by itself.
  → `Overlay._render_passive`, `_hud_add_content`, `_FadingWrap` (`_LINGER_MS`/`_FADE_MS`).
- **OPENED** (you focused it to type) — shows the header buttons, the full scrollable transcript, and
  the compose box, **with keyboard focus already in the compose box** so you can type immediately.
  → `Overlay.set_opened(True)`.
- **COLLAPSED** — a tiny always-on-top “🔒 ▸” pill. It is never unmapped (so it's always reachable
  without a hotkey). While collapsed it shows an **unread badge** (“🔒 ● N”) counting messages that
  arrived. Clicking the pill (or SUPER+SHIFT+J) expands it. → `Overlay.set_collapsed`,
  `mousePressEvent`, `_note_activity`.

**Behaviour when a message arrives** (`Overlay.add_message`): stored in history (capped at
`_ENTRIES_MAX=2000`); if collapsed → just bump the unread badge; if opened and it passes the filters →
append a widget; if passive and it passes → add a fading HUD line. Messages you sent never raise the
unread badge.

### 4.2 Opening must feel instant

When you open (`set_opened(True)`): **focus the compose box first**, then render only the last ~30
messages synchronously (~7 ms), then stream older history in above over 0 ms timer ticks (batches of
50), cancellable if you close again. So typing is available immediately and history fills in behind
it. → `set_opened`, `_rebuild`, `_fill_older`, `_IMMEDIATE`/`_FILL_BATCH`/`_OPENED_MAX`. History is
also capped so open/close never slows down on a spammy server.

### 4.3 The transcript is a widget list (not a QTextEdit)

`self.view` is a `QScrollArea` holding a vertical stack of per-message widgets. This is what lets a
GIF be a real animated widget that sizes correctly and never overlaps text. One shared translucent
panel sits behind all messages — its background is set **directly on the transcript widget** (so
`WA_StyledBackground` actually paints; a background put in the scroll-area's own stylesheet targeting
a child does **not** paint — a real gotcha). Text-only messages are a single bare `QLabel` for speed.
→ `_build_entry`, `_message_block`, `_text_label`, `_apply_font` (the `#hx_transcript` bg).

### 4.4 Scrolling

New messages **follow to the bottom only if you're already at the bottom**; if you scrolled up to read
history, your position is kept and the new message fills in below without yanking you. Implemented via
the scrollbar's `rangeChanged` signal (a deferred `setValue(max)` lands short). → `_append_widget`,
`_follow_range`, `_at_bottom`.

### 4.5 Colours

Player names and bodies render with the game's **own per-character colours** (a two-colour name shows
two colours); system/server lines keep their real colours too, not flat grey. Colours are brightened
to a minimum lightness so dark game colours stay legible over the translucent panel. Your own messages
are green; decrypted tunnel lines get a 🔒 prefix; whisper/emote keep fixed semantic colours.
→ `_format_message`, `_runs_html`, `_brighten`.

---

## 5. The compose box & commands (`overlay._ComposeEdit`, `app.on_submit`)

- Type + **Enter** submits. Empty Enter = "dismiss": hands focus back to the game.
  → `Overlay._on_submit`, `dismissed`.
- **Up / Down** walk your sent-line history (in-game style); past the newest, Down restores the draft
  you were typing. → `_ComposeEdit._browse`.
- **On unfocus** (you leave via J / P / ESC) the box is **wiped** so an old unsent scramble isn't left
  sitting there; sent-line history is kept. → `_ComposeEdit.discard`, called from `set_opened(False)`
  / `set_collapsed(True)`.
- Routing (`app._parse_command`): plain text → **public**; `/msg <friend> <text>` → **private**
  (encrypted if you share a key, else a plain `/msg`); `/r <text>` → reply to your last private
  contact; `/p <text>` → **party** (encrypted). `/font <name>` / `/font list` change the font live;
  `/friend add|accept|remove <name>` manage friends.

---

## 6. Header buttons & popups — what each does

The header is visible only in the OPENED state (`_sync_visibility`). Left→right:

- **Filter button** (text label: `all` / `party+dm` / `encrypted`). Click **cycles the view filter** —
  which message *types* the transcript shows. This is a separate axis from the noise filter.
  → `_cycle_filter`, `_passes`, `_FILTERS`.
- **😀 Emoji picker.** Click opens a popup grid; if the optional `emoji` lib is installed a search box
  filters the full set, else a curated set. Click a glyph → its `:shortcode:` is inserted at the
  compose cursor and focus returns to the box. `:shortcodes:` and emoticons (`<3`, `:)`) are expanded
  to glyphs at display time only (the wire keeps the shortcode). → `EmojiPicker`, `_insert_shortcode`,
  `emoji_util`.
- **🎬 GIF picker.** Click opens your **favorites + recents** as animated thumbnails, plus a box to
  add a GIF by pasting a direct `.gif`/`.webp` URL. Per tile: **★ / ☆** toggles favorite, **✕**
  deletes it from favorites *and* recents. Clicking a thumbnail **drops a compact `[GIF]` token into
  the compose box** (it does not send immediately) so you can put it in a `/msg`, `/p`, or public
  line; on send the token expands to the URL. → `GifPicker`, `Overlay.insert_gif`,
  `_ComposeEdit.insert_gif_token / expanded_text`, `gif_util`.
- **🧹 Noise filter.** Opens the filter panel (§7). A dot on the broom (🧹●) means at least one rule is
  actively hiding messages. → `_open_noise_panel`, `_update_noise_btn`, `chatfilter.any_active`.
- **👥 Friends.** Shows the current recipient; click for a panel to pick the whisper recipient, see
  pending inbound requests (badge count), and **add / accept / remove** friends. Add/accept run the
  X25519 handshake by sending `HXK1`/`HXK2` tokens over public chat. → `FriendsPanel`,
  `_on_friend_event`, `friend_action`.

### GIF rendering behaviour

An inline `.gif`/`.webp` URL in any message body renders as an **animated `QMovie` widget** on its own
row (so a tall GIF never overlaps text). Until the download lands it shows a "loading…" note, then
swaps to the animation; animations pause when the view is hidden and resume when shown. In the passive
HUD the GIF animates too. Downloads are cached (`~/.hytalecrypt/gifs/<sha256>.gif`); favorites/recents
live in `~/.hytalecrypt/gifs.json`. `gif_util.fetch` is the project's **only** outbound request and is
deliberately defensive: http/https only, 15 MB cap, `image/*` content-type only, bytes only ever fed
to `QMovie`. → `_GifLabel`, `_message_block`, `_ensure_gif`, `gif_util.fetch`.

---

## 7. Noise filter — behaviour (`chatfilter.py` + `overlay.NoiseFilterPanel`)

**Model:** everything shows by default; you opt in to *hiding* categories of spammy server chat. The
🧹 panel has category checkboxes plus a custom-rule list. Ticking a category, toggling/deleting a
custom rule, or adding one **re-renders the transcript immediately** and updates the broom badge.
→ `NoiseFilterPanel`, `_on_noise_changed`, `_refresh_view`.

**How a line is judged** (`chatfilter.should_hide(text, runs, is_player)` from `overlay._passes`):

- A **category** holds patterns. Each pattern is: a bare `"substring"` (contains), or
  `("text","startswith"|"endswith"|"contains")`, or with a 3rd colour arg
  `("[!]","startswith","orange")` (also require that colour), or colour-only `("","contains","red")`,
  or the special `("","number")` / `("","number","cyan")` = the whole message is **just a number**
  (`-298.0`, `52.6`, `100%`), optionally of a colour — for hiding stray HUD/combat numbers that leak
  onto the chat stream. → `_pattern_hit`, `_matches`, `_NUM_RE`.
- **Colour matching is region-aware:** a colour is checked in the run region where the text matched
  (e.g. the `!` inside `[!]`), *not* anywhere in the line — a yellow tip line also has red command
  runs, so "anywhere" would false-match. Colours are names (mapped by hue bucket) or exact `#rrggbb`.
  → `_match_region`, `_region_has_color`, `_color_matches`, `_color_name`.
- **Player-chat safety:** categories (except those in `PLAYER_CATEGORIES`, e.g. `welcome`) and **all**
  custom rules only ever hide **system** lines. A player who types "vote now!" or "52.6" is never
  hidden; your own and encrypted messages are never filtered. `overlay._passes` passes
  `is_player = (msg.kind != "system")`.

Rules persist in `~/.hytalecrypt/chatfilter.json`; the filter is display-only. Category patterns are
server-specific — the reference set targets one server (its `[!]` marker colours: `#ff5555` = voting,
`#ffff55` = tips/rules, `#ffaa00` = chat games). Tune them for the target server.

**Custom-rule row layout note:** the rule text word-wraps so a long rule can't push the ✕ delete button
off the fixed-width panel. → `NoiseFilterPanel._custom_row`.

---

## 8. Hotkeys & focus behaviour

On Linux, Hyprland `bind`s send POSIX signals to the app (PID in `~/.hytalecrypt/tunnel.pid`), drained
on the Qt thread (`app.drain`):

| Bind | Signal | Behaviour |
|------|--------|-----------|
| `SUPER+SHIFT+J` | `SIGUSR1` | collapse ↔ expand the pill |
| `SUPER+SHIFT+P` | `SIGRTMIN+5` | toggle focus overlay ↔ game |
| `Enter` (dynamic) | `SIGRTMIN+3` | open + focus the compose box — **only from the passive HUD** |
| `\` | `SIGRTMIN+4` | open + focus, pre-filled with `/` |
| `SUPER+SHIFT+±` | `SIGRTMIN+1/2` | grow / shrink the chat font |

Two subtle behaviours a port must copy:

- **Dynamic Enter.** The Enter→open bind is armed only while the game is focused and the overlay is
  passive, so Enter still works normally in-game and only "opens the chat" when there's nothing else
  it should do. → `app._update_enter_bind`, `_set_enter_bind`.
- **Focus-intent vs click-bounce.** When the passive overlay merely gets *clicked* it should bounce
  focus back to the game (don't open). But a *hotkey* open must not be mistaken for a click even if the
  cursor happens to be over it. So every deliberate focus sets a short "intent" window that suppresses
  the bounce; `SUPER+SHIFT+P` is routed **through the app** (RTMIN+5) rather than a bare window-focus
  so it also sets the intent. → `app._focus_overlay` (`focus_intent`), `_on_activation`, the RTMIN+5
  handler.

> **Caution when changing signals:** an unhandled real-time signal default-terminates the process, so
> restart the tunnel (to load new handlers) **before** reloading the Hyprland config that sends them.

---

## 9. Fonts, display defaults

- Font family/size change live via `/font`, `Ctrl+±`, and the global `SUPER+SHIFT+±`; the choice
  persists and re-renders the current view. → `Overlay.bump_font / set_font_family / _apply_font`.
- System/server lines are shown **by default** (so the noise filter can hide the junk you pick);
  `--hide-system` drops them at the source, `--tunnel-only` shows only encrypted messages.

---

## 10. Module map

| File | Role |
|------|------|
| `app.py` | Entry point: args, wiring, receive/send dispatch, hotkey signals, compose→send. |
| `overlay.py` | The entire PyQt6 UI and all behaviour above. |
| `chatframe.py` | Wire parsing (runs, colours, uncoloured tail), `classify`, `ChatLine`/`Msg`. Pure. |
| `crypto.py` | X25519 handshake, AES-GCM tokens, chunking, key storage. |
| `chatfilter.py` | Noise filter (categories, custom rules, colour/number matching, player-exemption). |
| `gif_util.py` | GIF favorites/recents/cache + defensive `fetch()`. |
| `emoji_util.py` | Shortcode/emoticon expansion + picker data. |
| `playername.py` | Detect your own in-game name (for `is_self`). |
| `receiver_quiche.py` + `quiche_capture.py` | **Linux receive**: eBPF hook on `quiche_conn_stream_recv`. |
| `inject_client.py` + `inject.py` | **Linux send**: ptrace injection onto the QUIC stream (elevated). |
| `send.py` / `send_linux.py` / `send_win.py` | Typing/paste send backends (the fallback / Windows path). |
| `hyprland.conf` | Hyprland window rules + keybinds. |
| `hytalecrypt` | Standalone crypto CLI. |

*(Not part of the current design: `memscan.py`/`memio*.py` memory-scan receive, `quiche_send_probe.py`
recon, `receiver_quiche_win.py`/`hotkeys_win.py` Windows scaffolding, `diag_modules.py` diagnostics.)*

---

## 11. Linux setup

Install PyQt6 + `cryptography` (optional `emoji`); `bpftrace` for capture; `pkexec`/`sudo` for the
injector; `wtype`/`ydotool` only if you use `--send-method type`. Run `hytale-tunnel` (`--help`; e.g.
`--me <name>`, `--party <group>`). Source `hytale_tunnel/hyprland.conf` in your Hyprland config. The
capture and injector each prompt once via `pkexec` on first use.

---

## 12. Porting to Windows — reproduce the same behaviour

**Reuse verbatim (pure Python):** `overlay.py`, `chatframe.py`, `crypto.py`, `chatfilter.py`,
`gif_util.py`, `emoji_util.py`, `playername.py`, `hytalecrypt`. Every §4–§9 behaviour above then comes
for free. Only three layers are OS-specific.

### 12.1 Receive — hook the QUIC layer (mirror the eBPF probe)

Match the Linux full-mirror behaviour: capture the same chat-log buffers and feed them to
**`chatframe.parse_all(raw)`**, emitting `Msg` objects exactly like `receiver_quiche.watch`. On Windows
that means **hooking the game's QUIC/`quiche` receive** (e.g. `quiche_conn_stream_recv` in the client's
networking DLL) with MinHook/Detours or a Frida script, and forwarding each buffer.
`receiver_quiche_win.py` is the placeholder for this. **Do not** reintroduce memory-scanning (it only
saw tunnel tokens, out of order, no sender) — the whole point of the current design is the full,
ordered mirror. Whatever you hook, **you must reproduce §2.1 exactly**, including the uncoloured Discord
tail, or those messages render blank.

### 12.2 Send — type/paste (no ptrace)

Windows has no ptrace injection; send by driving the chatbox: focus the game window
(`SetForegroundWindow`, un-minimize only), open chat, **type via `SendInput` + `KEYEVENTF_UNICODE`**
per-character (small delay so chars aren't dropped) or paste (`Ctrl+V`/`Shift+Insert` after `clip`),
then Enter; pace multi-chunk messages. Because there's no server echo of your own typed line, keep the
**optimistic local echo + token-hash dedup** the code already does on the non-Linux path.
→ `send_win.py` (`send_message`/`send_party`/`send_public`), `app.on_submit` (the `if not LINUX` echo).

### 12.3 Hotkeys, window & focus

Replace the Hyprland binds with a global-hotkey listener (`hotkeys_win.py`; the `keyboard` lib or a
`RegisterHotKey` message loop) that calls the same overlay methods (`toggle_collapsed`, focus
overlay/game, `prefill("/")`, `bump_font`). **Reproduce the two subtle behaviours from §8**: the
dynamic-Enter (only opens from passive) and the focus-intent (a hotkey-open isn't a click). The PyQt6
frameless `Tool` + `WindowStaysOnTopHint` + `WA_TranslucentBackground` window works on Windows as-is;
keep the transcript background **on the transcript widget** (§4.3). Swap `pkexec`/PID-file signalling
for Windows equivalents; find the game by window/process name (`send_win.find_game_window`).

### 12.4 Behaviour-fidelity checklist

- [ ] Full-mirror receive via a QUIC hook; §2.1 parsing incl. packed frames + uncoloured Discord tail
- [ ] Multi-colour names/bodies + coloured system lines (§4.5)
- [ ] Three window states + transitions; instant open; passive fading HUD; unread pill badge (§4.1–4.2)
- [ ] Scroll-stick; history caps (§4.4)
- [ ] Compose: Enter/empty-Enter, Up/Down recall, wipe-on-unfocus; routing incl. `/r` (§5)
- [ ] Header buttons: filter cycle, emoji, GIF picker→`[GIF]` token, noise, friends (§6)
- [ ] GIF inline animation (opened + HUD), picker, defensive fetch/cache (§6)
- [ ] X25519 friend handshake over chat tokens; chunking; 255-char fit (§3)
- [ ] Noise filter: categories, custom rules, region-aware colour, `number` mode, player-exemption (§7)
- [ ] Hotkeys incl. dynamic-Enter + focus-intent (§8); type/paste send + own-echo dedup (§12.2)
- [ ] Fonts live-resize; view-filter cycle; system-shown-by-default (§9)

---

## Security notes

- Secrets never leave `~/.hytalecrypt/` (git-ignored: `.hytalecrypt/`, `*.key`, `*.pem`, `seen.log`,
  `*.pid`). The wire carries only base64 AEAD tokens; friend requests carry only public keys.
- The overlay filter is display-only and never hides your own or encrypted messages.
- `gif_util.fetch` is the only outbound request and is sandboxed to image bytes handed to `QMovie`.
- This reads chat the game already received and writes chat as a player would; it does not patch the
  game, inspect anything beyond chat, or automate gameplay.
