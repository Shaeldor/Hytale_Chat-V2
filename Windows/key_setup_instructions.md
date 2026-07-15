# Hytale Tunnel - Setting up Keys

To send encrypted messages to friends or your party, you need to set up shared keys. Both you and your friend/party must have the exact same key registered.

## 1. Setting up a Party Key
The party channel allows you to encrypt messages so only your party members can read them.

**Step 1:** Generate a random secure key. Open a Command Prompt (cmd) and run the following commands to navigate to this folder and generate the key:
```cmd
cd "C:\path\to\Hytale_Chat\Windows"
hytalecrypt genkey
```
(This will output a long string of random characters, like "k+1234abcd...")

**Step 2:** Share that exact key with your party members securely (e.g. over Discord).

**Step 3:** Everyone in the party must register that key under the name "party" by running:
```cmd
cd "C:\path\to\Hytale_Chat\Windows"
hytalecrypt setkey party THE_KEY_YOU_SHARED
```

## 2. Setting up a Friend Key
To talk privately with a specific friend, do the same thing, but use their exact in-game name instead of "party".

**Step 1:** Generate a key:
```cmd
cd "C:\path\to\Hytale_Chat\Windows"
hytalecrypt genkey
```

**Step 2:** Share it with your friend.

**Step 3:** You run:
```cmd
cd "C:\path\to\Hytale_Chat\Windows"
hytalecrypt setkey FriendName THE_KEY_YOU_SHARED
```
(And your friend runs: `hytalecrypt setkey YourName THE_KEY_YOU_SHARED`)

## 3. Listing and Deleting Keys
- To see all your current keys: `hytalecrypt list`
- To delete a key: `hytalecrypt delkey party` or `hytalecrypt delkey FriendName`

## 4. Chat Hotkeys
Once you are in the game with the tunnel running, use these global hotkeys to control the chat overlay:
- **`Shift + Up Arrow`**: Open the chat box and start typing.
- **`Shift + Down Arrow`**: Shrink the chat box down to a tiny pill icon.
- **`Shift + Left Arrow`**: Return focus to the game (lets you play while keeping the chat fully expanded on screen).

*Tip: The chat also automatically remembers the last channel you were talking in when you restart it!*
