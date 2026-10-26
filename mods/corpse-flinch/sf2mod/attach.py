"""Attach to the running SF2 client and drive it from outside the game.

Everything here is read-only until `Bridge.state()`; the only writes the mod ever
performs are (a) installing the release's own ProcessEvent trampoline (the exact
patch its bundled diagnostic tools install) and (b) asking the game to run
Engine.PrimitiveComponent.AddImpulse.

The runtime this mod reuses ships with the game:

    <game>\\runtime\\tools\\local_battle_bridge.py   bridge/Reader + hook installer
    <game>\\runtime\\tools\\local_route_bridge.py    NativeAPI: function/property lookup
    <game>\\runtime\\local_route_watch.py            main_thread(pid)

If the game is updated, those tools change first; keep this file thin so a new
build only needs the constants at the top reviewed.
"""
import ctypes
import ctypes.wintypes as w
import json
import os
import struct
import sys
import time
from pathlib import Path

HOOK = 0x6008D0
EXPECTED = bytes.fromhex('56 8b f1 8b 0d f4 b2 79 01')
STATE_OFFSET = 0x1000          # bridge_code() puts its command block here
TRAMPOLINE_OFFSET = 0x400

GAME_MARKER = ('runtime', 'tools', 'local_battle_bridge.py')


def game_dir(start=None):
    """The install root, found by walking up from this file.

    `SF2_GAME_DIR` overrides the search, which is handy when running the mod from a
    checkout instead of from <game>\\mods\\corpse-flinch.
    """
    override = os.environ.get('SF2_GAME_DIR')
    if override and Path(override, *GAME_MARKER).is_file():
        return Path(override).resolve()
    here = Path(start or __file__).resolve()
    for parent in here.parents:
        if parent.joinpath(*GAME_MARKER).is_file():
            return parent
    raise SystemExit('找不到游戏目录：请把 mods\\corpse-flinch 放在游戏安装目录下。')


def load_runtime(game):
    """Import the client's own runtime helpers (they are version pinned there)."""
    for path in (game / 'runtime', game / 'runtime' / 'tools',
                 game / 'runtime' / 'tools' / 'python-deps'):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    import local_battle_bridge as battle          # noqa: E402
    import local_route_bridge as route            # noqa: E402
    from local_route_watch import main_thread     # noqa: E402
    return battle, route, main_thread


# --------------------------------------------------------------------- process
def client_exe(game):
    """The client binary the runtime is pinned to (the launcher knows best)."""
    try:
        import start_local_client as launcher
        return Path(launcher.EXE).resolve()
    except Exception:                                # noqa: BLE001
        return (game / 'test-client' / 'Binaries' / 'Win32' / 'sf2.exe').resolve()


def find_pid(game, timeout=0.0):
    """PID of the isolated client, or 0. `timeout` seconds to wait for it."""
    wanted = client_exe(game)
    deadline = time.monotonic() + timeout
    while True:
        pid = _scan_pid(wanted)
        if pid:
            return pid
        if time.monotonic() >= deadline:
            return 0
        time.sleep(1.0)


def pid_alive(pid):
    """True while `pid` is a live process (QUERY_LIMITED, no other rights).

    Used by the worker to notice that the game closed: the bridge cannot tell,
    it just keeps answering from a dead process's memory.
    """
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
    """Match by full image path: several builds ship an exe called sf2.exe."""
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
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR,
                                             ctypes.POINTER(w.DWORD)]
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
            if entry.name.casefold() == wanted.name.casefold():
                handle = k.OpenProcess(0x1000, False, entry.pid)   # QUERY_LIMITED
                if handle:
                    try:
                        buffer = ctypes.create_unicode_buffer(32768)
                        size = w.DWORD(len(buffer))
                        if k.QueryFullProcessImageNameW(handle, 0, buffer,
                                                        ctypes.byref(size)):
                            if buffer.value.casefold() == target:
                                found = int(entry.pid)
                                break
                    finally:
                        k.CloseHandle(handle)
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return found


# ---------------------------------------------------------------------- bridge
class ForeignBridge(RuntimeError):
    """The native entry belongs to another bridge (the R6 AI worker)."""


class Bridge:
    """A local_battle_bridge.Bridge that can find (or create) its own command block.

    The bundled bridge keeps that address in runtime/evidence/local-battle-bridge.json
    and refuses to run when the record does not match the process. A community
    install may have no record at all (the game was started by some other tool) or
    a stale one from an earlier session, so the address is recovered from the jump
    we can read at HOOK instead: E9 rel32 -> remote, and the command block always
    lives at remote + 0x1000 because that is where Bridge.install() puts it.
    """

    HOOK = HOOK
    EXPECTED = EXPECTED

    def __init__(self, battle, pid):
        self.impl = battle.Bridge(pid)
        self.pid = pid
        self._state = 0
        self.owned = False                 # this session installed the hook
        self.main_thread = None            # callable, set by connect()

    # ---- plain reads
    def read(self, address, size):
        return self.impl.read(address, size)

    def write(self, address, value):
        return self.impl.write(address, value)

    @property
    def names(self):
        return self.impl.names

    @names.setter                                  # NativeAPI.refresh() assigns it
    def names(self, value):
        self.impl.names = value

    def __getattr__(self, name):                   # k, handle, header, path, objects, close
        return getattr(self.impl, name)

    # ---- command block
    def state(self, main_thread=None):
        if self._state:
            return self._state
        hook = self.read(HOOK, len(EXPECTED))
        if hook == EXPECTED:
            if main_thread is None:
                raise RuntimeError('桥未安装，需要主线程 id')
            record = self.impl.install(main_thread())
            self._state = int(record['state'], 16)
            self.owned = True
            return self._state
        if hook[:1] != b'\xe9':
            raise RuntimeError(f'ProcessEvent 入口不是跳转也不是原指令：{hook.hex()}')
        # Never adopt a block we did not create: R6's AI worker uses a batch layout
        # on this very address, and single-call writes would corrupt its batches.
        record = self.record()
        if record is None:
            raise ForeignBridge('native entry belongs to another bridge')
        # E9 rel32 is a 32-bit displacement: when VirtualAllocEx lands high the
        # signed value is negative, and adding it unmasked produced a negative
        # address ("Cannot read -0x..."). Mask like the CPU does.
        remote = (HOOK + 5 + struct.unpack('<i', hook[1:5])[0]) & 0xFFFFFFFF
        state = remote + STATE_OFFSET
        status = struct.unpack('<I', self.impl.read(state, 4))[0]
        if status > 4:
            raise RuntimeError(f'桥命令块状态异常：{status}')
        self._state = state
        self.owned = True
        return state

    def record(self):
        """This session's own bridge record, or None when the entry is not ours."""
        try:
            record = self.impl.active()
        except Exception:                              # noqa: BLE001
            return None
        return record

    def restore(self):
        """Put the original entry bytes back; False when we never owned it."""
        if not self.owned:
            return False
        record = self.record()
        if record is None:
            return False
        self.impl.patch_entry(record['main_thread'], bytes.fromhex(record['jump']), EXPECTED)
        self.owned = False
        self._state = 0
        return True

    def active(self):
        return {'pid': self.pid, 'state': hex(self.state())}

    def invoke(self, object_address, function_address, parameters, timeout=10.0):
        if not self.owned:
            raise ForeignBridge('bridge is passive: the AI worker owns the entry')
        state = self.state()
        status = struct.unpack('<I', self.read(state, 4))[0]
        if status not in (0, 3):
            raise RuntimeError(f'上一条请求还没完成（status={status}）')
        parameter_address = state + 0x100
        self.write(parameter_address, parameters)
        self.write(state + 4, struct.pack('<III', object_address, function_address,
                                          parameter_address))
        self.write(state, struct.pack('<I', 1))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = struct.unpack('<I', self.read(state, 4))[0]
            if status == 3:
                return self.read(parameter_address, len(parameters))
            if status == 4:
                raise RuntimeError('游戏拒绝了这条调用（native batch rejected）')
            time.sleep(0.002)
        raise TimeoutError(f'游戏主线程没有响应（status={status}）：游戏是否被暂停/最小化？')


def connect(game, battle, route, main_thread, pid=None, wait=0.0, log=print):
    """Wait for the client, attach, make sure the bridge answers, build the API."""
    pid = pid or find_pid(game, timeout=wait)
    if not pid:
        raise SystemExit('游戏没有运行：先启动游戏，再运行本程序。')
    log(json.dumps({'event': 'process', 'pid': pid}))
    bridge = Bridge(battle, pid)
    try:
        # main_thread takes the pid; connect hands Bridge.state a ready callable so
        # the installer can find a thread of *this* process (the zero-argument call
        # was a latent TypeError that only showed up when we had to install).
        bridge.main_thread = lambda: main_thread(pid)
        try:
            bridge.state(bridge.main_thread)
        except ForeignBridge:
            # The AI worker owns the entry (an AI match is already running). Stay
            # passive instead of exiting: sf2mod.priority takes it back later.
            log(json.dumps({'event': 'bridge-foreign',
                            'note': 'native entry owned by the AI worker; passive'}))
        api = route.NativeAPI(bridge)              # constructor already refreshes
    except Exception:
        bridge.close()
        raise
    log(json.dumps({'event': 'attached', 'pid': pid,
                    'functions': len(api.functions),
                    'properties': len(api.properties)}))
    return bridge, api
