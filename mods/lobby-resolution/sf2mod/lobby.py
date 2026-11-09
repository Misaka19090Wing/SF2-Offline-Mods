"""Tell whether the client is showing the front end (the lobby) right now.

Only two static addresses are version specific; both are pinned by the same
`sf2.exe` hash the reader checks. `is_lobby` keeps the class-name rule pure so the
unit test does not need a game.
"""
import struct

from sf2mod.reader import UINT32

GWORLD = 0x179B2F4
LEVEL = 0x50
ACTORS = 0x3C
PROPERTY_OFFSET = 0x64
LOBBY_PREFIXES = ('sfentrygame',)


def is_lobby(game_class):
    """The front end's game class, e.g. `SFEntryGame_0`."""
    return bool(game_class) and game_class.casefold().startswith(LOBBY_PREFIXES)


def u32(reader, address):
    return struct.unpack('<I', reader.read(address, 4))[0]


class Lobby:
    """Live lobby detector: UWorld -> persistent level -> WorldInfo -> Game class."""

    def __init__(self, reader):
        self.reader = reader
        self.game_offset = self._offset()

    def _offset(self):
        for address, _name, cls in self.reader.objects():
            if cls == 'ObjectProperty' and self.reader.path(address) == 'Engine.WorldInfo.Game':
                return struct.unpack('<I', self.reader.read(address + PROPERTY_OFFSET, 4))[0]
        raise RuntimeError('找不到 Engine.WorldInfo.Game 属性')

    def world(self):
        value = u32(self.reader, GWORLD)
        return value if value > UINT32 else 0

    def worldinfo(self, world):
        if world <= UINT32:
            return 0
        level = u32(self.reader, world + LEVEL)
        if level <= UINT32:
            return 0
        pointer, count, capacity = struct.unpack('<3I', self.reader.read(level + ACTORS, 12))
        if not 0 < count <= capacity < 100000:
            return 0
        return u32(self.reader, pointer)

    def game_class(self):
        world = self.world()
        info = self.worldinfo(world) if world else 0
        if not info:
            return ''
        game = u32(self.reader, info + self.game_offset)
        if game <= UINT32:
            return ''
        return self.reader.object_name(self.reader.header(game)[13])

    def active(self):
        return is_lobby(self.game_class())
