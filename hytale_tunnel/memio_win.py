"""Windows memory-I/O backend: find the client and read its memory via Win32.

Mirrors memio_linux's surface (find_client_pid / open_reader) using
CreateToolhelp32Snapshot (process list), VirtualQueryEx (region walk) and
ReadProcessMemory (the actual reads). Only writable, private (heap/anon-equivalent)
committed regions are yielded, matching the Linux filter.

windll is touched only inside functions so this module still imports on non-Windows
(for syntax/CI checks); it is selected at runtime by memio.py only on win32.
"""

import ctypes
from ctypes import wintypes

CLIENT_NAMES = ("HytaleClient.exe", "HytaleClient")

# --- constants ---
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
# PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
WRITABLE_MASK = 0x04 | 0x08 | 0x40 | 0x80
STILL_ACTIVE = 259
MAX_ADDR = 0x7FFFFFFFFFFF          # top of x64 user space
READ_BUF = 8 * 1024 * 1024

# Fixed-width aliases so struct layout matches Win32 on ALL platforms (wintypes.DWORD
# is c_ulong, which is 8 bytes on 64-bit Linux but 4 on Windows -- don't use it in
# Structures). ULONG_PTR is pointer-sized (8 on the 64-bit Windows clients we target).
_DWORD = ctypes.c_uint32
ULONG_PTR = ctypes.c_size_t


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD),
        ("cntUsage", _DWORD),
        ("th32ProcessID", _DWORD),
        ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", _DWORD),
        ("cntThreads", _DWORD),
        ("th32ParentProcessID", _DWORD),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", _DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", _DWORD),
        ("__alignment1", _DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", _DWORD),
        ("Protect", _DWORD),
        ("Type", _DWORD),
        ("__alignment2", _DWORD),
    ]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    # Handles are pointer-sized; without explicit restype/argtypes ctypes defaults to
    # c_int (32-bit), which truncates/sign-extends a real kernel HANDLE on 64-bit
    # Windows. The snapshot handle happened to be small in one test, but a larger
    # value would silently corrupt the process walk -- declare them all.
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32First.restype = wintypes.BOOL
    k.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.Process32Next.restype = wintypes.BOOL
    k.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_ulonglong,
                                    ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_size_t)]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_ulonglong,
                                 ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    return k


def find_client_pid() -> int | None:
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return None
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    best = None
    try:
        ok = k.Process32First(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.decode("ascii", "ignore")
            low = name.lower()
            if "hytale" in low:
                if low in (n.lower() for n in CLIENT_NAMES):
                    return entry.th32ProcessID      # exact match wins immediately
                best = entry.th32ProcessID          # fuzzy fallback
            ok = k.Process32Next(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return best


class WinMemReader:
    def __init__(self, pid: int):
        self.pid = pid
        self.k = _kernel32()
        self.h = self.k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess failed for pid {pid} "
                          f"(error {ctypes.get_last_error()}; try running as admin)")
        self._buf = ctypes.create_string_buffer(READ_BUF)

    def regions(self, max_region: int | None = None):
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        while addr < MAX_ADDR:
            got = self.k.VirtualQueryEx(self.h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                break
            base, size = mbi.BaseAddress, mbi.RegionSize
            if size == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE
                    and (mbi.Protect & WRITABLE_MASK)
                    and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))):
                if not (max_region and size > max_region):
                    yield base, base + size
            addr = base + size

    def read(self, addr: int, size: int) -> bytes:
        nread = ctypes.c_size_t(0)
        buf = self._buf if size <= READ_BUF else ctypes.create_string_buffer(size)
        ok = self.k.ReadProcessMemory(self.h, addr, buf, size, ctypes.byref(nread))
        if not ok and nread.value == 0:
            return b""
        return buf.raw[:nread.value]

    def alive(self) -> bool:
        code = wintypes.DWORD(0)
        if self.k.GetExitCodeProcess(self.h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_reader(pid: int) -> WinMemReader:
    return WinMemReader(pid)
