# Hytale Chat Tunnel (v5.2) - Setup Guide

Welcome! This tool allows you to send end-to-end encrypted messages to your friends or your party in Hytale. No one (not even the server) can read your messages without the secret keys!

## 1. Initial Setup (Do this once)
1. Ensure you have **Python 3** installed on your computer (from python.org). Make sure to check the box that says "Add python.exe to PATH" during installation.
2. Double-click the `setup-windows.bat` file in this folder.
3. A terminal will open and automatically install the required libraries (PyQt6, cryptography, frida). Press any key to close it when it's done.

## 2. Setting Up Encryption Keys
To talk privately with someone, you both need to register the exact same secret key.

**How to generate a key:**
1. Open the `Windows_HyChat_V5.2` folder.
2. Click the address bar at the very top of the window, type `cmd`, and hit **Enter**.
3. In the black window that appears, type:
   `hytalecrypt genkey`
4. Copy the long random string it gives you and share it securely with your friend or party (e.g. over Discord).

**How to save a key:**
Once you have a key, everyone needs to register it. In that same `cmd` window:
- For a **Friend**, type: `hytalecrypt setkey THEIR_IN_GAME_NAME THE_KEY`
- For your **Party**, type: `hytalecrypt setkey party THE_KEY`
*(You should see a success message!)*

## 3. How to Use It
1. Start the overlay by double-clicking `hytale-tunnel.bat` (inside the `hytale_tunnel` folder).
2. The UI will appear as an overlay on your screen. 
3. Use the **dropdown menu** at the top right to select where your messages will go:
   - **Public**: Sends normal, unencrypted messages.
   - **Party**: Encrypts the message so only people with your "party" key can read it.
   - **[Friend Name]**: Encrypts the message so only that specific friend can read it.
4. Type your message and hit Enter!

## 💡 Cool Features
- **Global Hotkeys**: Control the chat without taking your hands off the keyboard!
  - `Shift + Up Arrow`: Instantly opens the chat box so you can start typing.
  - `Shift + Down Arrow`: Shrinks the chat box down to a tiny pill icon.
  - `Shift + Left Arrow`: Returns focus to the game, but keeps the chat fully expanded on your screen so you can read while you play!
- **Channel Memory**: The chat overlay automatically remembers the last channel you selected. If you close it on "Party", it will open back up on "Party"!
- **Custom Encrypt (🔐)**: Click the lock icon next to the dropdown to instantly encrypt a message and copy it to your clipboard. You can paste this on signs, in books, or anywhere in the world!
- **Manual Decrypt (👁️)**: See a weird `HX1...` code on a sign? Copy it, click the eye icon, paste it in, and the tunnel will decode the hidden message for you!
