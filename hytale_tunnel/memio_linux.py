"""Linux memory-I/O backend: find the client and read its memory via /proc.

Exposes the small surface the shared scanner (memscan.py) needs:
  find_client_pid() -> int | None
  open_reader(pid)  -> MemReader   (context manager with .regions/.read/.alive)
"""

import glob
import os

CLIENT_NAMES = ("HytaleClient",)


def _is_zombie(pid: str | int) -> bool:
    # A leftover "<defunct>" client keeps its name/comm but is state Z with no maps;
    # reading its memory yields nothing, so never return it as the live client.
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] == b"Z"
    except (OSError, IndexError):
        return True


def find_client_pid() -> int | None:
    for comm_path in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm_path) as f:
                if f.read().strip() in CLIENT_NAMES:
                    pid = int(comm_path.split("/")[2])
                    if not _is_zombie(pid):
                        return pid
        except (OSError, ValueError):
            continue
    # comm is truncated to 15 chars; fall back to argv[0].
    for cmd_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmd_path, "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0]
            if any(argv0.endswith(b"/" + n.encode()) for n in CLIENT_NAMES):
                pid = int(cmd_path.split("/")[2])
                if not _is_zombie(pid):
                    return pid
        except (OSError, ValueError):
            continue
    return None


class LinuxMemReader:
    def __init__(self, pid: int):
        self.pid = pid
        self._mem = open(f"/proc/{pid}/mem", "rb", 0)   # FileNotFoundError if dead

    def regions(self, max_region: int | None = None):
        """Yield (start, end) for readable+writable, anonymous regions."""
        with open(f"/proc/{self.pid}/maps") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 5:
                    continue
                perms = parts[1]
                if "r" not in perms or "w" not in perms:
                    continue
                path = parts[5] if len(parts) >= 6 else ""
                if path and not path.startswith("["):     # skip file-backed (libs/assets)
                    continue
                start_s, end_s = parts[0].split("-")
                start, end = int(start_s, 16), int(end_s, 16)
                if max_region and (end - start) > max_region:
                    continue
                yield start, end

    def read(self, addr: int, size: int) -> bytes:
        try:
            self._mem.seek(addr)
            return self._mem.read(size)
        except OSError:
            return b""

    def alive(self) -> bool:
        return os.path.exists(f"/proc/{self.pid}")

    def close(self):
        try:
            self._mem.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_reader(pid: int) -> LinuxMemReader:
    return LinuxMemReader(pid)
