# Hytale Chat Windows Overlay v7.0

This project is a Windows overlay client that allows end-to-end encrypted messaging via Hytale's native chat. 
Because Hytale chat doesn't support encryption or rich media (like GIFs and Emojis) natively, this overlay floats above the game, intercepting and decrypting messages specifically meant for you.

## Features
- **E2E Encryption**: Messages are securely encrypted using shared keys.
- **Party & Direct Messaging**: Support for sending private whispers and group party chats.
- **Emoji Support**: Convert emoji shortcodes (like `:smile:`) directly into visual emojis via the system font.
- **GIF & Image Support**: Send direct `.gif` or `.webp` links in chat, and they will animate directly within your Hytale chat overlay.
- **Seamless HUD**: Integrated directly over the Hytale UI with an opened chat view and a fading HUD for a native experience.

## Recent Updates / Changelog

- **Major Restructuring (V7.0)**:
  - Migrated key storage! Friend keys remain in `/friends`, while party/group keys are now distinctly stored in `/groups`.
  - Removed the `[name]` argument for parties, locking down the UI and logic to support a single, unified "party" chat across the system.
- **New Commands**:
  - `\party create` instantly spawns a new 32-byte party key.
  - `\party invite <friend>` securely sends the active party key to a friend.
  - `\help` prints a crisp white list of all supported overlay commands.
- **UI & Experience Upgrades**:
  - The minimized HUD now strictly filters out all standard unencrypted Hytale chat messages, so it only ever pops up for encrypted incoming DMs, party chat, or system alerts.
  - System message UI was overhauled to use a crisp light blue (`#7ec8ff`) font and an alert `❗` emoji instead of standard text, massively improving readability.
  - Fixes to the dropdown menu ensuring it stays instantly synced when friends or parties are added/removed.
  - Added seamless background saving! Whenever you select a recipient channel from the dropdown (like Party, Public, or a specific friend), it silently saves that choice to a file so it completely persists when you reboot the overlay.
- **Improved Emoji Parsing**: Fixed an issue where the text body was being incorrectly formatted; emojis now cleanly render as glyphs on both clients.
- **Enhanced GIF & WebP Support**:
  - Automatically detects direct `.gif` and `.webp` links in messages without needing a manual prefix, and includes `\gif` command support.
  - Removed the legacy `HXG1` marker for full compatibility with newer Linux clients, eliminating the glitch where `HXG1` would appear as text in front of GIFs.
  - GIF headers now correctly format the recipient's name (e.g. `🔒 to Friend:`) instead of just showing `you`.
- **Legacy Changes**:
  - **Always-on HUD**: The passive fading chat (HUD) now remains visible and anchored directly below the pill icon even when the chat box is minimized (`Shift+Down`).
  - **Expanded View**: The chat window height was increased from 320px to 600px to give much more room for reading chat history.
  - Fixed a scrolling bug where a large blank gap would appear above newly loaded GIFs; the chat view now perfectly snaps to the bottom once the GIF resolves.
- **Automated Friend Setup**:
  - Included a seamless key exchange system using `\friend add <name>` and `\friend accept <name>` that securely performs a Diffie-Hellman handshake over public whispers, eliminating the need to manually share passwords.

## Getting Started
To send encrypted messages, use the custom encryption popup. You can copy the generated tokens and paste them into Hytale. The overlay will automatically detect encrypted messages meant for you, decrypt them, and render them transparently over the game.
