<div align="center">
  <h1>🛡️ Hytale Chat Tunnel (Windows V5.3)</h1>
  <p><b>End-to-End Encrypted Chat Overlay for Hytale</b></p>
</div>

Welcome to the **Hytale Chat Tunnel**! This tool provides a sleek, non-intrusive Windows overlay that allows you to send end-to-end encrypted messages to your friends or party in Hytale. No one—not even the server—can read your messages without the secret keys!

---

## ✨ Features
- **Unbreakable Privacy:** Military-grade encryption (AES-256-GCM) masks your chat traffic as meaningless `HX1...` codes.
- **Sleek Overlay UI:** A gorgeous, transparent overlay that sits on top of your game without interfering with gameplay.
- **Global Hotkeys:** Never take your hands off your keyboard.
  - `Shift + Up Arrow`: Instantly opens the chat box so you can start typing.
  - `Shift + Down Arrow`: Shrinks the chat box down to a tiny, unobtrusive pill icon.
  - `Shift + Left Arrow`: Returns focus to the game but keeps the chat fully expanded on your screen so you can read while you play!
- **Channel Memory:** The overlay remembers the last channel you were talking in across restarts.
- **Hardware Anti-Leak:** Features a foolproof, hardware-level auto-abort system. If you accidentally hit a key while the macro is injecting a message, it immediately aborts to prevent raw encrypted strings from leaking into global chat.
- **Custom Encrypt/Decrypt (🔐/👁️):** Want to leave a secret message on a wooden sign or in a book? Use the manual encrypt/decrypt tools built right into the UI!

---

## 🛠️ 1. Initial Setup (One-Time)
1. Ensure you have **Python 3** installed on your computer (from [python.org](https://python.org)). 
   > **Note:** Make sure to check the box that says `"Add python.exe to PATH"` during installation!
2. Open the `Windows_HyChat_V5.3` folder.
3. Double-click the `setup-windows.bat` file.
4. A terminal will open and automatically install the required libraries (`PyQt6`, `cryptography`, `frida`). Press any key to close it when it's done.

---

## 🔑 2. Setting Up Encryption Keys
To talk privately with someone, you both need to register the exact same secret key on your computers.

### Generating a New Key
1. Inside the `Windows_HyChat_V5.3` folder, click the address bar at the very top of the window, type `cmd`, and hit **Enter**.
2. In the black terminal window that appears, type:
   ```cmd
   hytalecrypt genkey
   ```
3. Copy the long random string it gives you and share it securely with your friend or party (e.g., over Discord).

### Saving a Key
Once you have a key, everyone who wants to communicate needs to register it. In that same `cmd` window:
- For a **Friend**, type: `hytalecrypt setkey THEIR_IN_GAME_NAME THE_KEY`
- For your **Party**, type: `hytalecrypt setkey party THE_KEY`

*(You should see a success message!)*

---

## 🎮 3. How to Use It
1. Start the overlay by double-clicking `hytale-tunnel.bat` (inside the `Windows_HyChat_V5.3/hytale_tunnel` folder).
2. The UI will appear as an overlay on your screen. 
3. Use the **dropdown menu** at the top right to select where your messages will go:
   - **Public**: Sends normal, unencrypted messages.
   - **Party**: Encrypts the message so only people with your "party" key can read it.
   - **[Friend Name]**: Encrypts the message so only that specific friend can read it.
4. Type your message and hit Enter! Happy tunneling! 🚀
