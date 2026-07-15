# Hytale Chat Windows Overlay v7.0

This project is a Windows overlay client that allows end-to-end encrypted messaging via Hytale's native chat. 
Because Hytale chat doesn't support encryption or rich media (like GIFs and Emojis) natively, this overlay floats above the game, intercepting and decrypting messages specifically meant for you.

---

## 🚀 Quick Start (Easiest Way to Run)

For Windows users, we have pre-compiled the overlay into a self-contained executable. You do not need to install Python or run any setup scripts.

1. Download the **`Compiled_HyChat`** folder.
2. Inside it, double-click **`hytale-tunnel.exe`** to start the overlay!
3. The overlay will launch as a overlay on top of your screen. 
   *(Note: Make sure your game is set to **Borderless Windowed Fullscreen** in its video settings so the overlay draws on top smoothly without any black screen flashes!)*

---

## 🎮 In-Game Commands

Once the overlay is running, type these commands directly into your standard Hytale chat box to interact with the system:

### Friend Management (Handshake)
- **`\friend add <player>`**: Sends a secure friend request to a player. This will automatically execute a Diffie-Hellman cryptographic key handshake over public whispers.
- **`\friend accept <player>`**: Accepts an incoming friend request and completes the secure key exchange.
- **`\friend remove <player>`**: Removes a friend and deletes their shared secret key.

### Group & Party Chats
- **`\party create`**: Generates a brand new, unique 32-byte secret AES key for a secure party chat room.
- **`\party invite <friend>`**: Encrypts and securely sends your active party key to your friend. Once they receive it, they are instantly added to your party chat!

### Extras & Utilities
- **`\gif <url>`**: Sends an encrypted GIF link. The recipient client will decrypt and animate it directly in their overlay.
- **`\help`**: Prints this list of commands in crisp white text in your chat history.
- **`\exit`**: Gracefully terminates the overlay, releases hotkeys, and stops all background packet capture tasks.

---

## 🎹 Global Hotkeys (Controls)

Use these keyboard shortcuts to navigate the overlay while in-game:
*   **`Shift + Up`**: Expands the chat window so you can read history and type messages.
*   **`Shift + Down`**: Minimizes/collapses the chat overlay back to a clean, passive HUD.
*   **`Shift + Left`**: Unfocuses the chat overlay and returns keyboard focus back to your game window.

---

## 🛠️ Developer Setup (For building from source)

If you wish to modify the code or run it directly using a local Python interpreter:

1. Double-click `setup-windows.bat` to install the required Python dependencies (`PyQt6`, `cryptography`, `frida`, `emoji`).
2. Run the batch launcher: `hytale_tunnel\hytale-tunnel.bat -r <friend>`
3. To re-compile the `.exe` folder yourself, run the `build_compiled.bat` script.
