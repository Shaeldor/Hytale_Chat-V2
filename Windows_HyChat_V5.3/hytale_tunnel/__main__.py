"""Enables `python -m hytale_tunnel` (cross-platform launch)."""

import sys

from .app import main

sys.exit(main())
