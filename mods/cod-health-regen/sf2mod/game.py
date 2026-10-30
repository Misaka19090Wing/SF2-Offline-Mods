"""Where the local player and its health fields live.

Every object field is resolved from live reflection -- the health pair *and* the
two controller links -- so a rebuilt client keeps working. The constants left are
the same three the client's own runtime and corpse-flinch use (verified live by
`tools/probe.py` on this build), and `local_battle_bridge.Reader` refuses any
process whose sf2.exe hash differs, so a new build fails loudly instead of writing
into the wrong place.

The local player is identified by `APlayerController.Player != NULL`, not by class
name: that is the discriminator `local_battle_bridge.api()` itself uses, and it
needs no name table (which is what went stale in the bug recorded in docs/01 §6.1).
"""
import struct
import time

from sf2mod.regen import MAX_PLAUSIBLE_HEALTH

GWORLD = 0x179B2F4          # UWorld* of the current world
LEVEL = 0x50                # UWorld.PersistentLevel
ACTORS = 0x3C               # ULevel.Actors TArray
CLASS_INDEX = 13            # UObject.Class inside Reader.header()
PROPERTY_OFFSET = 0x64      # UProperty -> storage offset inside the owning object
UINT32 = 0x10000

HEALTH = 'Engine.Pawn.Health'
HEALTH_MAX = 'Engine.Pawn.HealthMax'
CONTROLLER_PAWN = 'Engine.Controller.Pawn'
CONTROLLER_PLAYER = 'Engine.PlayerController.Player'
PAWN_PRI = 'Engine.Pawn.PlayerReplicationInfo'
PAWN_CONTROLLER = 'Engine.Pawn.Controller'
CONTROLLER_PRI = 'Engine.Controller.PlayerReplicationInfo'
GAME = 'Engine.WorldInfo.Game'
PRI_TEAM = 'Engine.PlayerReplicationInfo.Team'
TEAM_INDEX = 'Engine.TeamInfo.TeamIndex'
OBJECT_SCAN_INTERVAL = 3.0  # a full GObjects sweep costs ~0.2 s; only when pawnless

INT_FIELDS = {HEALTH: 'health', HEALTH_MAX: 'health_max', TEAM_INDEX: 'team_index'}
OBJECT_FIELDS = {CONTROLLER_PAWN: 'controller_pawn', CONTROLLER_PLAYER: 'controller_player',
                 PAWN_PRI: 'pawn_pri', PRI_TEAM: 'pri_team',
                 PAWN_CONTROLLER: 'pawn_controller', CONTROLLER_PRI: 'controller_pri',
                 GAME: 'game'}
WANTED = {
    'Health': ('IntProperty', INT_FIELDS),
    'HealthMax': ('IntProperty', INT_FIELDS),
    'Pawn': ('ObjectProperty', OBJECT_FIELDS),
    'Player': ('ObjectProperty', OBJECT_FIELDS),
    'PlayerReplicationInfo': ('ObjectProperty', OBJECT_FIELDS),
    'Controller': ('ObjectProperty', OBJECT_FIELDS),
    'Game': ('ObjectProperty', OBJECT_FIELDS),
    'Team': ('ObjectProperty', OBJECT_FIELDS),
    'TeamIndex': ('IntProperty', INT_FIELDS),
}


def u32(bridge, address):
    return struct.unpack('<I', bridge.read(address, 4))[0]


def refresh_names(bridge):
    """Re-read the live name table and drop the per-address path cache.

    `Reader` reads the name table once, in `__init__`, and caches `header()`/
    `path()` by object address. Attach during boot and both are stale in a way
    that never heals: the table was shorter than it will be, and the paths cached
    for objects whose names had not loaded yet keep coming back, so a property is
    never recognised -- measured: a long-lived worker still failed at 300 s on a
    client where a *fresh* process resolved the offsets instantly. The shipped
    `NativeAPI.refresh()` clears the same two caches for the same reason.
    """
    from inspect_battle_runtime import NAMES
    pointer, count, capacity = struct.unpack('<3I', bridge.read(NAMES, 12))
    if not 0 < count <= capacity < 2000000:
        raise ValueError('Unexpected native name table')
    bridge.names = struct.unpack('<' + 'I' * count, bridge.read(pointer, count * 4))
    # All three are lru_cached by address/index, and a stale `name` entry is the
    # nastiest: an index looked up before it existed stays '<bad-name:N>' forever.
    bridge.name.cache_clear()
    bridge.header.cache_clear()
    bridge.path.cache_clear()
    return count


class Fields:
    """Storage offsets of the health pair and of the two controller links."""

    def __init__(self, bridge):
        self.bridge = bridge
        refresh_names(bridge)
        found = {}
        for address, name, cls in bridge.objects():
            wanted = WANTED.get(name)
            if not wanted or cls != wanted[0]:
                continue
            path = bridge.path(address)
            if path in wanted[1] and path not in found:
                found[path] = struct.unpack('<I', bridge.read(address + PROPERTY_OFFSET, 4))[0]
        missing = [path for path in list(INT_FIELDS) + list(OBJECT_FIELDS) if path not in found]
        if missing:
            raise RuntimeError('客户端缺少属性：' + ', '.join(missing))
        self.health = found[HEALTH]
        self.health_max = found[HEALTH_MAX]
        self.controller_pawn = found[CONTROLLER_PAWN]
        self.controller_player = found[CONTROLLER_PLAYER]
        self.pawn_pri = found[PAWN_PRI]
        self.pawn_controller = found[PAWN_CONTROLLER]
        self.controller_pri = found[CONTROLLER_PRI]
        self.game = found[GAME]
        self.pri_team = found[PRI_TEAM]
        self.team_index = found[TEAM_INDEX]

    def read(self, pawn):
        """(health, health_max) of one pawn."""
        health = struct.unpack('<i', self.bridge.read(pawn + self.health, 4))[0]
        maximum = struct.unpack('<i', self.bridge.read(pawn + self.health_max, 4))[0]
        return health, maximum

    def write(self, pawn, value):
        self.bridge.write(pawn + self.health, struct.pack('<i', int(value)))

    def pawn_of(self, controller):
        return u32(self.bridge, controller + self.controller_pawn)

    def player_of(self, controller):
        return u32(self.bridge, controller + self.controller_player)

    def world(self):
        """The current UWorld*, read fresh (Player.actors() is not always run first)."""
        value = u32(self.bridge, GWORLD)
        return value if value > UINT32 else 0

    def worldinfo(self, world):
        """The AWorldInfo actor of a level.

        `GWORLD` points at UWorld, which has no `Game` property: the pointer lives on
        the level's first actor, the same shortcut the client's own tools use.
        """
        if world <= UINT32:
            return 0
        level = u32(self.bridge, world + LEVEL)
        if level <= UINT32:
            return 0
        pointer, count, capacity = struct.unpack('<3I', self.bridge.read(level + ACTORS, 12))
        if not 0 < count <= capacity < 100000:
            return 0
        return u32(self.bridge, pointer)

    def game_of(self, world):
        """The game info object of a world (0 when there is none yet)."""
        info = self.worldinfo(world)
        return u32(self.bridge, info + self.game) if info else 0

    def team_of(self, pawn):
        """Team index of one pawn, or None when it has no replication info.

        None is not 0: a pawn without a PRI (a PVE monster, a spectator) must never
        be mistaken for a member of the player's team.
        """
        pri = u32(self.bridge, pawn + self.pawn_pri)
        if pri < UINT32:
            # AI pawns often carry the replication info on their controller only.
            controller = u32(self.bridge, pawn + self.pawn_controller)
            pri = u32(self.bridge, controller + self.controller_pri) if controller >= UINT32 else 0
        if pri < UINT32:
            return None
        team = u32(self.bridge, pri + self.pri_team)
        if team < UINT32:
            return None
        return struct.unpack('<i', self.bridge.read(team + self.team_index, 4))[0]

    def offsets(self):
        return {'health': hex(self.health), 'max': hex(self.health_max),
                'controller_pawn': hex(self.controller_pawn),
                'controller_player': hex(self.controller_player),
                'pawn_pri': hex(self.pawn_pri), 'pri_team': hex(self.pri_team),
                'team_index': hex(self.team_index), 'pawn_controller': hex(self.pawn_controller),
                'controller_pri': hex(self.controller_pri), 'game': hex(self.game)}

    @classmethod
    def wait(cls, bridge, timeout=3600.0, interval=2.0, on_wait=None):
        """Resolve the offsets, waiting out the client's boot.

        A freshly started client answers `Reader` (name table) long before it is
        useful: `objects()` raises `Unexpected object array` early, and the
        property objects stay invisible until the packages behind them load.
        None of that is a failure, so all of it gets the same generous window.
        A real version mismatch never resolves -- the log then says `waiting-boot`
        every minute and `tools/selftest.py` gives the definitive answer.
        """
        started = time.monotonic()
        while True:
            try:
                return cls(bridge)
            except (ValueError, OSError, RuntimeError) as failure:
                # `as failure` is cleared when the block ends (PEP 3110); keep a copy.
                error = failure
            if time.monotonic() - started >= timeout:
                raise error
            if on_wait:
                on_wait(error)
            time.sleep(interval)


class Player:
    """The local player's pawn.

    Discovery tries the persistent level's actor list first (cheap: one bulk read
    plus a few field reads) and only falls back to a full GObjects sweep when that
    list holds no controller with a `Player` link -- the shipped bridge finds
    controllers through GObjects, so neither route alone is trusted.
    """

    def __init__(self, bridge, fields, on_event=None):
        self.bridge = bridge
        self.fields = fields
        self.controller = 0
        self.pawn = 0
        self.via = ''                 # which route first found the controller
        self.world = 0
        self.level = 0
        self.actors_seen = 0
        self.humans_seen = 0
        self.candidates = []
        self.next_object_scan = 0.0
        self.last_world = 0
        self.names_count = len(bridge.names)
        self.sample_classes = []
        self.on_event = on_event or (lambda event, **fields: None)

    def actors(self):
        """Actors of the persistent level, or [] when there is no world yet."""
        world = u32(self.bridge, GWORLD)
        self.world = world if world > UINT32 else 0
        if not self.world:
            self.level = 0
            return []
        level = u32(self.bridge, self.world + LEVEL)
        self.level = level if level > UINT32 else 0
        if not self.level:
            return []
        pointer, count, capacity = struct.unpack('<3I', self.bridge.read(self.level + ACTORS, 12))
        if not 0 < count <= capacity < 100000:
            return []
        raw = self.bridge.read(pointer, 4 * count)
        self.actors_seen = count
        addresses = [struct.unpack_from('<I', raw, 4 * i)[0] for i in range(count)]
        self.sample_classes = [self._kind(address) for address in addresses[:8]]
        return addresses

    def _usable(self, pawn):
        """A pawn we may act on: its HealthMax must be sane.

        The menu's limbo pawn is not a player: `Health` and `HealthMax` are both 0,
        so accepting it would silently turn the whole mod into a no-op for as long
        as it exists. Health may be 0 here -- that is a legitimately dead player.
        """
        if pawn <= UINT32:
            return False
        try:
            _, maximum = self.fields.read(pawn)
        except OSError:
            return False
        return 0 < maximum <= MAX_PLAUSIBLE_HEALTH

    def kind(self, address):
        """Class name of one actor ('' when unreadable)."""
        return self._kind(address)

    def _kind(self, address):
        """Class name of one UObject, or '' when it cannot be read."""
        try:
            return self.bridge.object_name(self.bridge.header(address)[CLASS_INDEX])
        except (OSError, RecursionError):
            return ''

    def _refresh(self, controller, require_name=True):
        """Set controller/pawn when `controller` is the human one.

        The class check is not cosmetic: any other actor's bytes at the `Player`
        offset can be a non-zero pointer, and measured on this build that made 9 of
        26 lobby actors look like "human controllers". `require_name=False` skips it
        for the name-free fallback (see `find`).
        """
        try:
            if require_name and not self._kind(controller).endswith('PlayerController'):
                return False
            if self.fields.player_of(controller) <= UINT32:
                return False
            self.controller = controller
            self.pawn = self.fields.pawn_of(controller)
            return True
        except OSError:
            return False

    def find(self, now=None):
        """The local player's pawn, or 0. Cheap while a pawn is known."""
        now = time.monotonic() if now is None else now
        # Only a pawn counts as "found": a cached controller with no pawn (death,
        # respawn) must not stop the search, or a stale controller wedges the mod.
        if self.controller and self._refresh(self.controller) and self._usable(self.pawn):
            return self.pawn
        actors = self.actors()
        humans = 0
        for address in actors:
            if address > UINT32 and self._refresh(address):
                humans += 1
                if self._usable(self.pawn):
                    self.via = self.via or 'level'
                    self.humans_seen = humans
                    return self.pawn
        self.humans_seen = humans
        # Fallback that needs no class names at all: a non-null `Player` link whose
        # `Pawn` is one of this level's actors and has a sane HealthMax. Used when
        # class names cannot be trusted (a map load interns new names).
        known = set(actors)
        for address in actors:
            if address <= UINT32 or not self._refresh(address, require_name=False):
                continue
            if self.pawn in known and self._usable(self.pawn):
                self.via = self.via or 'level-no-name'
                return self.pawn
        if self.world and self.world != self.last_world:
            self.last_world = self.world
            self.next_object_scan = 0.0    # a new world: rescan immediately
        # No pawn: the controller may sit outside the persistent level's actor list
        # (the shipped bridge finds controllers through GObjects), so sweep as well.
        if now >= self.next_object_scan:
            self.next_object_scan = now + OBJECT_SCAN_INTERVAL
            # Loading a map interns new names; `Reader` keeps the table it read at
            # attach, so without this every class-name check fails in a match --
            # measured: 0 controllers found in a 244-actor map, then 1 in the menu.
            try:
                count = refresh_names(self.bridge)
                if count and count != self.names_count:
                    self.on_event('names', before=self.names_count, after=count)
                    self.names_count = count
            except (OSError, ValueError) as error:
                self.on_event('worker-error', where='refresh_names', error=str(error))
            self.candidates = []           # evidence for the no-pawn diagnostic
            for address, name, cls in self.bridge.objects():
                if name.startswith('Default__') or not cls.endswith('PlayerController'):
                    continue
                try:
                    player = self.fields.player_of(address)
                    pawn = self.fields.pawn_of(address)
                except OSError:
                    continue
                if len(self.candidates) < 5:
                    self.candidates.append({'address': hex(address), 'class': cls,
                                            'player': hex(player), 'pawn': hex(pawn),
                                            'usable': self._usable(pawn)})
                if player > UINT32:
                    self.controller = address
                    self.pawn = pawn
                    if self._usable(pawn):
                        self.via = self.via or 'objects'
                        return pawn
        return 0

    def summary(self):
        return {'world': hex(self.world), 'level': hex(self.level),
                'actors': self.actors_seen, 'human_controllers': self.humans_seen,
                'controller': hex(self.controller), 'pawn_pending': bool(self.controller),
                'names': self.names_count, 'via': self.via,
                'sample_classes': self.sample_classes, 'candidates': self.candidates}
