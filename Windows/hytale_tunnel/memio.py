"""Platform dispatcher for memory I/O. Selects the /proc or Win32 backend.

The shared scanner (memscan.py) imports find_client_pid / open_reader from here
and never touches OS specifics directly.
"""

import sys

if sys.platform.startswith("win"):
    from .memio_win import CLIENT_NAMES, find_client_pid, open_reader
elif sys.platform.startswith("linux"):
    from .memio_linux import CLIENT_NAMES, find_client_pid, open_reader
else:                                                       # pragma: no cover
    CLIENT_NAMES = ("HytaleClient",)

    def find_client_pid():
        raise RuntimeError(f"memory scanning unsupported on platform: {sys.platform}")

    def open_reader(pid):
        raise RuntimeError(f"memory scanning unsupported on platform: {sys.platform}")
