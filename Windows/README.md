# Hytale Chat Windows Overlay

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
  - Automatically detects direct `.gif` and `.webp` links in messages without needing a manual prefix.
  - Removed the legacy `HXG1` marker for full compatibility with newer Linux clients, eliminating the glitch where `HXG1` would appear as text in front of GIFs.
- **Optimized UI Resizing**:
  - Scaled down the maximum size of GIFs (160px for the opened chatbox, 120px for the passive HUD) so they fit comfortably in the compact UI.
  - Fixed a scrollbar bug that caused a large blank gap to appear above newly loaded GIFs; the chat view now perfectly snaps to the bottom once the GIF resolves.
- **Custom Encrypt Popup**: Improved the robustness of the custom encryption workflow used on Windows.

## Getting Started
To send encrypted messages, use the custom encryption popup. You can copy the generated tokens and paste them into Hytale. The overlay will automatically detect encrypted messages meant for you, decrypt them, and render them transparently over the game.
