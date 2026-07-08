Here is a prompt you can give to an AI to upgrade your Linux version of Hytale_Chat with the latest UI and Party features!

---

**PROMPT TO SEND TO YOUR AI:**

Hey! We need to upgrade this Linux version of Hytale Tunnel with some new UI features and Party Chat integration that we recently built on the Windows side. Here is exactly what we need you to implement across the files:

1. **Party Chat Parsing (`chatframe.py`)**:
   - Add a regex to parse incoming party messages: `_RE_PARTY = re.compile(r"^\[!\]\s+\[Party\]\s+([A-Za-z0-9_]{2,20}):\s(.*)$", re.DOTALL)`
   - Add it to the `classify()` function so it returns `ChatLine("party", ...)`
   
2. **Whitelist Party in Quiche (`receiver_quiche.py`)**:
   - Add `"party"` to the `_PLAYER_KINDS` set so party messages are not filtered out.

3. **Routing via Dropdown (`overlay.py` & `app.py`)**:
   - In `overlay.py`, modify the `self.recipient_box` initialization to explicitly add `"Public"` and `"Party"` to the top of the dropdown list, followed by the registered friends. Default the selection to `"Public"`.
   - Update the placeholder text in the input box to say `"Type a message to the selected channel..."`
   - In `app.py`, rewrite `_parse_command` so it takes a `channel` argument. Completely remove all the old slash command parsing (`/msg`, `/pe`, `/r`). Make it strictly use the `channel` variable (which comes from the dropdown) to return either `("public", None, text)`, `("party_private", "party", text)`, or `("private", channel, text)`. Update `on_submit` to pass `ui.recipient` into `_parse_command`.

4. **Sending Party Messages (`send_linux.py` & `send.py`)**:
   - Add a `send_party_message()` function to `send_linux.py` (which encrypts the message for the "party" key and types `/party chat {token}` in game). 
   - Ensure `send_public()` and `send_party_message()` are properly exported in `send.py` so `app.py` can call them.

5. **Color Overhauls (`overlay.py`)**:
   - In `Overlay.__init__`, store the list of friends as `self._friends_list`.
   - In `add_message`, completely override the game's default name colors. If `msg.is_self`, make the name green. If `msg.sender in self._friends_list`, make the name cyan blue. Otherwise (strangers), make it white.
   - For `msg.kind == "party"`, format the line so the `[Party] ` tag is always orange, but the player's name still follows the Green/Cyan/White rule above.
