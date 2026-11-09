"""Read/write the isolated client's memory without the client's own runtime.

R10 seals the game's runtime: every `<game>\\runtime\\tools\\*.py` is a one-line stub
and the bundled `python.exe` refuses any script that is not in the signed manifest
(`sf2guard: 此程序只能运行游戏自带的脚本`). The mod therefore carries its own small
reader, pinned to the same `sf2.exe` hash the client's Reader used.

Nothing here patches memory; `write` exists because the regen mod writes
`Pawn.Health` and the corpse mod writes `Actor.LifeSpan`.
"""
import ctypes as c
import ctypes.wintypes as w
import hashlib
import struct
from functools import lru_cache
from pathlib import Path

OBJECTS = 0x17882D0        # UObject array (data pointer, count, capacity)
NAMES = 0x1775EF0          # UE name table
EXE_HASH = '6bb5ac173b1a4dee1ffd0644456849c559d2eaec464036e36cf8a6e30dd2b194'
ACCESS = 0x438             # QUERY_INFORMATION | VM_OPERATION | VM_READ | VM_WRITE
UINT32 = 0x10000


def exe_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Reader:
    """Read/write handle on one process, locked to the expected sf2.exe."""

    def __init__(self, pid, exe):
        exe = Path(exe)
        if not exe.is_file():
            raise ValueError('找不到客户端：' + str(exe))
        if exe_hash(exe) != EXE_HASH:
            raise ValueError('客户端版本不匹配（sf2.exe 哈希变了）：' + str(exe))
        self.pid = int(pid)
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.k.OpenProcess.restype = w.HANDLE
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        self.k.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
        self.k.WriteProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
        self.handle = self.k.OpenProcess(ACCESS, False, self.pid)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        buffer = c.create_unicode_buffer(32768)
        length = w.DWORD(len(buffer))
        if not self.k.QueryFullProcessImageNameW(self.handle, 0, buffer, c.byref(length)) \
                or Path(buffer.value).resolve() != exe.resolve():
            self.close()
            raise ValueError('Refusing a process outside the isolated client')
        pointer, count, capacity = struct.unpack('<III', self.read(NAMES, 12))
        if not 1000 < count <= capacity < 1000000:
            self.close()
            raise ValueError('Unexpected name array')
        self.names = struct.unpack('<' + 'I' * count, self.read(pointer, count * 4))

    def close(self):
        if getattr(self, 'handle', None):
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __getattr__(self, name):
        raise AttributeError(name)

    def read(self, address, size):
        buffer = c.create_string_buffer(size)
        count = c.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, address, buffer, size, c.byref(count)) \
                or count.value != size:
            raise OSError(f'Cannot read {address:#x} ({size} bytes)')
        return buffer.raw

    def write(self, address, value):
        buffer = c.create_string_buffer(bytes(value))
        count = c.c_size_t()
        if not self.k.WriteProcessMemory(self.handle, address, buffer, len(value), c.byref(count)) \
                or count.value != len(value):
            raise OSError(f'Cannot write {address:#x} ({len(value)} bytes)')

    @lru_cache(maxsize=1000000)
    def name(self, index):
        if not 0 <= index < len(self.names):
            return f'<bad-name:{index}>'
        if not self.names[index]:
            return f'<empty-name:{index}>'
        address = self.names[index]
        try:
            data = self.read(address, 128)
        except OSError:
            # UE name entries are variable length; the final one can cross into an
            # unmapped page.
            data = self.read(address, min(128, 0x1000 - (address & 0xfff)))
        flags = struct.unpack_from('<I', data, 8)[0]
        if flags >> 1 != index:
            return f'<bad-entry:{index}>'
        raw = data[16:]
        if flags & 1:
            return raw.decode('utf-16le', errors='replace').split('\0', 1)[0]
        return raw.split(b'\0', 1)[0].decode('latin1')

    @lru_cache(maxsize=1000000)
    def header(self, address):
        return struct.unpack('<15I', self.read(address, 60))

    def object_name(self, address):
        fields = self.header(address)
        name = self.name(fields[11])
        return name + (f'_{fields[12] - 1}' if fields[12] else '')

    @lru_cache(maxsize=1000000)
    def path(self, address):
        if not address:
            return ''
        fields = self.header(address)
        prefix = self.path(fields[10]) if fields[10] else ''
        return (prefix + '.' if prefix else '') + self.object_name(address)

    def objects(self):
        pointer, count, capacity = struct.unpack('<III', self.read(OBJECTS, 12))
        if not 1000 < count <= capacity < 1000000:
            raise ValueError('Unexpected object array')
        pointers = struct.unpack('<' + 'I' * count, self.read(pointer, count * 4))
        for index, address in enumerate(pointers):
            if not address:
                continue
            try:
                header = self.header(address)
                if header[1] != index:
                    continue
                yield address, self.object_name(address), self.object_name(header[13])
            except OSError:
                continue
