#!/usr/bin/env bash
# Toggle focus between the tunnel overlay and the Hytale game window.
# Bound to a key: if the overlay is focused, focus the game; otherwise focus the overlay.
cur=$(hyprctl activewindow -j 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('class',''))" 2>/dev/null)

if [ "$cur" = "hytale-tunnel" ]; then
    hyprctl dispatch focuswindow 'class:^(HytaleClient)$'
else
    hyprctl dispatch focuswindow 'class:^(hytale-tunnel)$'
fi
