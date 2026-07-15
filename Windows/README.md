# Hytale Chat Windows Overlay v6.4

This project is a Windows overlay client that allows end-to-end encrypted messaging via Hytale's native chat. 
Because Hytale chat doesn't support encryption or rich media (like GIFs and Emojis) natively, this overlay floats above the game, intercepting and decrypting messages specifically meant for you.

## Features
- **E2E Encryption**: Messages are securely encrypted using shared keys.
- **Party & Direct Messaging**: Support for sending private whispers and group party chats.
- **Emoji Support**: Convert emoji shortcodes (like `:smile:`) directly into visual emojis via the system font.
- **GIF & Image Support**: Send direct `.gif` or `.webp` links in chat, and they will animate directly within your Hytale chat overlay.
- **Seamless HUD**: Integrated directly over the Hytale UI with an opened chat view and a fading HUD for a native experience.

## Recent Updates / Changelog

- **Improved Emoji Parsing**: Fixed an issue where the text body was being incorrectly formatted; emojis now cleanly render as glyphs on both clients.
- **Enhanced GIF & WebP Support**:
  - Automatically detects direct `.gif` and `.webp` links in messages without needing a manual prefix, and includes `\gif` command support.
  - Removed the legacy `HXG1` marker for full compatibility with newer Linux clients, eliminating the glitch where `HXG1` would appear as text in front of GIFs.
  - GIF headers now correctly format the recipient's name (e.g. `🔒 to Friend:`) instead of just showing `you`.
- **UI & Experience Upgrades**:
  - **Always-on HUD**: The passive fading chat (HUD) now remains visible and anchored directly below the pill icon even when the chat box is minimized (`Shift+Down`).
  - **Expanded View**: The chat window height was increased from 320px to 600px to give much more room for reading chat history.
  - **Styling Tweaks**: Updated overlay instruction text to crisp white for better readability and styled startup connection messages in a clean green color.
- **Bug Fixes**:
  - Fixed a double-send glitch in party chat by reverting the prefix injection from `/party chat` back to `/p chat`.
  - Fixed a scrolling bug where a large blank gap would appear above newly loaded GIFs; the chat view now perfectly snaps to the bottom once the GIF resolves.
- **Automated Friend Setup**:
  - Included a seamless key exchange system using `\friend add <name>` and `\friend accept <name>` that securely performs a Diffie-Hellman handshake over public whispers, eliminating the need to manually share passwords.

## Getting Started
To send encrypted messages, use the custom encryption popup. You can copy the generated tokens and paste them into Hytale. The overlay will automatically detect encrypted messages meant for you, decrypt them, and render them transparently over the game.
