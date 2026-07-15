Here is a prompt you can give to an AI to upgrade your Linux version of Hytale_Chat with the V7.0 Friend and Party logic!

---

**PROMPT TO SEND TO YOUR AI:**

Hey! We need to upgrade this Linux version of Hytale Tunnel with the new automated Friend Setup and Party Invite systems we recently built on the Windows side. Here is exactly what we need you to implement across the files:

1. **Key Storage Migration (`crypto.py`)**:
   - Separate key storage into two directories: `~/.hytalecrypt/friends/` (for direct message keys) and `~/.hytalecrypt/groups/` (for party/group keys). 
   - Update `load_psk` and `set_psk` to use the `/friends` directory.
   - Create `load_group_psk` and `set_group_psk` to use the `/groups` directory.
   - Ensure the decryption loop checks *both* directories when building its list of loaded keys!

2. **Diffie-Hellman Friend Handshake (`crypto.py` & `app.py`)**:
   - Implement an automated key-exchange so users don't have to manually trade passwords.
   - **Step 1 (Add)**: When the user types `\friend add <name>`, generate a new ECDH keypair, save the private key locally as a pending outgoing request, and send the public key as an unencrypted `HXHS` (Hytale Chat Handshake) whisper to the friend.
   - **Step 2 (Receive Add)**: When the receiver parses an incoming `HXHS` token, if it's an "add", save their public key as a pending incoming request and alert the user.
   - **Step 3 (Accept)**: When the receiver types `\friend accept <name>`, generate our own ECDH keypair, derive the shared AES-256 secret using the friend's saved public key, save it to `/friends/<name>.key`, and send our public key back as an `HXHS` accept token.
   - **Step 4 (Receive Accept)**: When the original sender receives the `HXHS` accept token, derive the identical shared AES-256 secret using the receiver's public key, and save it to `/friends/<name>.key`.

3. **Party Creation and Invites (`app.py`)**:
   - Implement `\party create`: Generate 32 completely random bytes using `os.urandom(32)`, base64 encode it, and save it to `/groups/party.key`.
   - Implement `\party invite <friend>`: Load the current `party.key`, encrypt it using the friend's specific symmetric key (from `/friends/<friend>.key`), and send the encrypted payload as a standard private message. The internal payload text should be formatted as: `\party_invite <base64_party_key>`.
   - When decrypting incoming messages, if the decrypted payload text starts with `\party_invite `, intercept it! Do not display it to the user. Instead, parse the base64 key and instantly save it as their new `/groups/party.key`.

4. **UI Integration (`overlay.py`)**:
   - Add a `\help` command that prints out a list of these new commands in the chat window.
   - Ensure the UI dropdown menu dynamically rebuilds itself to list "Public", "Party" (if the party key exists), and the list of friends.
   - The UI should instantly refresh this list without requiring a restart whenever a friend is added/removed or a party is created/joined.
