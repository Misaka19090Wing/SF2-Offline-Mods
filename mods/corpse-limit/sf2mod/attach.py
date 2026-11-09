"""Attach to the running SF2 client and read/write its memory. Installs nothing.

Unlike corpse-flinch this mod does not need the ProcessEvent bridge: it never asks
the game to run a function, it only reads the local player's Health/HealthMax and
writes Health back. So there is no code patch, no remote allocation and no main
thread requirement -- which is also why it can coexist with an AI worker that has
already installed the shipped bridge.

The memory reader is built in (`sf2mod.reader`), NOT imported from the client: R10
seals the client runtime (its `local_battle_bridge.py` is a one-line stub) and its
bundled python refuses any script outside the signed manifest. Only two static
offsets and the `sf2.exe` hash are version specific; both are in `reader.py`.

Both layouts are supported: the mod ships into `mods\\<name>` of either a portable
game root or the developer workspace.
"""
import ctypes
import ctypes.wintypes as w
import os
import sys
import time
from pathlib import Path

from sf2mod.reader import Reader

MARKERS = (('runtime', 'tools', 'local_battle_bridge.py'),
           ('tools', 'runtime', 'local_battle_bridge.py'))
UINT32 = 0x10000


def game_dir(start=None):
    """The install root (or developer workspace) this mod belongs to."""
    override = os.environ.get('SF2_GAME_DIR')
    if override and any(Path(override, *m).is_file() for m in MARKERS):
        return Path(override).resolve()
    here = Path(start or __file__).resolve()
    for parent in here.parents:
        if any(parent.joinpath(*m).is_file() for m in MARKERS):
            return parent
    raise SystemExit('找不到游戏目录：请把 mods\\corpse-limit 放在游戏目录下，或设置 SF2_GAME_DIR。')


def layout(game):
    return 'portable' if (Path(game) / 'runtime/tools/local_battle_bridge.py').is_file() else 'developer'


def client_exe(game):
    """The binary this install is pinned to; the launcher knows best.

    `start_local_client` must come from *this* install: if it is resolved from a
    neighbouring tree, `find_pid` would look for another install's sf2.exe and the
    launcher would happily start a second game.
    """
    game = Path(game).resolve()
    try:
        # R10's start_local_client.py is a sealed stub that raises SystemExit.
        import start_local_client as launcher
        exe = Path(launcher.EXE).resolve()
        if game in exe.parents:
            return exe
    except BaseException:                            # noqa: BLE001 - fallback only
        pass
    root = game / 'runtime' if layout(game) == 'portable' else game
    return (root / 'test-client/Binaries/Win32/sf2.exe').resolve()


def pid_alive(pid):
    """True while `pid` is a live process (QUERY_LIMITED, no other rights)."""
    try:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = k.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = w.DWORD(0)
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                   # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    except Exception:                                # noqa: BLE001
        return False


def _scan_pid(wanted):
    """Match by full image path: several builds ship a process called sf2.exe."""
    class Entry(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('default_heap', ctypes.c_void_p), ('module', w.DWORD),
                    ('threads', w.DWORD), ('parent', w.DWORD), ('prio', ctypes.c_long),
                    ('flags', w.DWORD), ('name', w.WCHAR * 260)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == w.HANDLE(-1).value:
        return 0
    entry = Entry()
    entry.size = ctypes.sizeof(Entry)
    target = str(wanted).casefold()
    found = 0
    try:
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.name.casefold() == Path(wanted).name.casefold():
                handle = k.OpenProcess(0x1000, False, entry.pid)
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = w.DWORD(len(buffer))
                        if k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            if buffer.value.casefold() == target:
                                found = int(entry.pid)
                                break
                    finally:
                        k.CloseHandle(handle)
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return found


def find_pid(game, timeout=0.0):
    """PID of the isolated client, or 0 after waiting `timeout` seconds."""
    wanted = client_exe(game)
    deadline = time.monotonic() + timeout
    while True:
        pid = _scan_pid(wanted)
        if pid or time.monotonic() >= deadline:
            return pid
        time.sleep(0.5)


def connect(game, pid):
    """Read/write handle on the client. Raises while the client is still booting."""
    return Reader(pid, client_exe(game))
