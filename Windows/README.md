# Hytale Chat Windows Overlay

This project is a Windows overlay client that allows end-to-end encrypted messaging via Hytale's native chat. 
Because Hytale chat doesn't support encryption or rich media (like GIFs and Emojis) natively, this overlay floats above the game, intercepting and decrypting messages specifically meant for you.

## 🌟 Full Feature List

- **E2E Encryption**: All messages are securely encrypted using Quiche and shared keys. No one else can read your whispers.
- **Party & Direct Messaging**: Support for sending private whispers and group party chats.
- **Native HUD Integration**: A borderless, transparent overlay that perfectly aligns with Hytale's UI. It features a passive fading HUD for normal gameplay and an interactive chatbox when you open the game chat.
- **Emoji Support**: Convert emoji shortcodes (like `:smile:`) directly into visual emojis via the system font. Emojis natively render inline with text.
- **GIF & Image Support**: Send direct `.gif` or `.webp` links in chat, and they will animate directly within your Hytale chat overlay! The UI scales them down perfectly to fit within the chat window.
- **Memory Scanning**: Automatically reads the process memory (`memscan.py`, `memio.py`) to detect your player name and automatically intercept messages.
- **Global Hotkeys**: Uses `ctypes` and native Windows hooks (`hotkeys_win.py`) to listen for Hytale's chat toggle keys so the overlay perfectly mimics the game's chat state.

## 🛠️ Setup Instructions

Follow these steps to get the Windows client running:

1. **Install Python**: Ensure you have Python 3.8+ installed on your system. Make sure Python is added to your system PATH.
2. **Run Setup**: Double-click `setup-windows.bat`. This will automatically install all required Python packages (like `customtkinter`, `cryptography`, `pillow`, etc.) into a virtual environment.
3. **Configure Keys**: You will need to generate or share a cryptographic key with your friends. Read `key_setup_instructions.md` for detailed steps on placing your keys in the `keys/` directory.
4. **Launch Hytale**: Open Hytale and join a multiplayer server.
5. **Launch the Tunnel**: Double-click `hytale-tunnel.bat` to launch the overlay. 
6. **Focus Hytale**: The overlay will automatically attach to the Hytale game window.

## 📝 Usage

- **Sending Messages**: To send an encrypted message, use the custom encryption popup. You can copy the generated tokens and paste them into Hytale.
- **Receiving Messages**: The overlay will automatically detect encrypted messages meant for you, decrypt them, and render them transparently over the game.
- **Sending Media**: Just paste a direct `.gif` or `.webp` link into the chat, and the overlay will handle the rest!

## 🔄 Recent Updates (V6.1)

- **Improved Emoji Parsing**: Fixed an issue where the text body was being incorrectly formatted; emojis now cleanly render as glyphs on both clients.
- **Enhanced GIF & WebP Support**:
  - Automatically detects direct `.gif` and `.webp` links in messages without needing a manual prefix.
  - Removed the legacy `HXG1` marker for full compatibility with newer Linux clients, eliminating the glitch where `HXG1` would appear as text in front of GIFs.
- **Optimized UI Resizing**:
  - Scaled down the maximum size of GIFs (160px for the opened chatbox, 120px for the passive HUD) so they fit comfortably in the compact UI.
  - Fixed a scrollbar bug that caused a large blank gap to appear above newly loaded GIFs; the chat view now perfectly snaps to the bottom once the GIF resolves.
- **Custom Encrypt Popup**: Improved the robustness of the custom encryption workflow used on Windows.
