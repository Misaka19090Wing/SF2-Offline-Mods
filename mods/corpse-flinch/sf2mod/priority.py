"""Give the native ProcessEvent entry back to the R6 AI worker while it needs it.

R6's bot AI drives plant/defuse and movement through its own bridge, installed on
the same 9-byte entry (`0x6008D0`) with a *batch* command block at `remote+0x1000`.
Only one bridge can own the entry, and the two block layouts are incompatible, so
the two mods must take turns: this mod keeps the entry in free-play modes and hands
it back the moment an AI-driven match starts.

Which matches the AI worker drives is not guessed here -- it is read from the
client's own launch record (`runtime\\evidence\\local-client-launch.json`) and the
same game-class test `local_route_watch.watch()` uses:

    sfwarriorgame*                                    hero mode (when hero AI is on)
    sfblastinggame_ sfseizuregame_ sfescapegame_ sfdeathgame_   objective modes
    sfaiteamdeathgame + a map in `route_maps`          route-assisted TDM

Handover is data-only: `Bridge.restore()` puts the original bytes back (no code
patch of our own), the AI worker's 1 Hz retry then installs its own hook.
"""
import json
import struct
import time
from pathlib import Path

UINT32 = 0x10000
GWORLD = 0x179B2F4          # UWorld*; same value the client runtime uses
LEVEL = 0x50                # UWorld.PersistentLevel
ACTORS = 0x3C               # ULevel.Actors
GAME_PROPERTY = 'Engine.WorldInfo.Game'
# A knife room is a room *rule*, not a game class: the match still uses the team
# deathmatch class, so the AI worker must be let in or nobody arms the bots
# (only local_route_worker.KnifeRule destroys the rifles; hero and PVE have none).
KNIFE_RULE = 5                      # ECUSTOMMODE_WEAPONTYPE_KNIFE, same as KnifeRule.read_rule
RULE_PROPERTIES = {
    'game': 'Engine.WorldInfo.Game',
    'gri': 'Engine.WorldInfo.GRI',
    'game_rule': 'SFGame.SFGameInfo.CustomModeType',
    'gri_rule': 'SFGame.SFGameReplicationInfo.CustomModeType',
}
LAUNCH_RECORD = ('runtime/evidence/local-client-launch.json',
                 'evidence/local-client-launch.json')
OBJECTIVE_PREFIXES = ('sfblastinggame_', 'sfseizuregame_', 'sfescapegame_', 'sfdeathgame_')
HERO_PREFIX = 'sfwarriorgame'
AI_TEAM_PREFIXES = ('sfaiteamdeathgame', 'sfteamdeathgame')
# Alien survival/defence (异形 PVE). The authoritative list lives in the client's
# own local_pve_allies.GAME_PREFIXES; this is only the fallback for a build where
# that module cannot be imported.
PVE_PREFIXES = ('sfsurvivalgame', 'sfdefencegame')

OWNED = 'owned'             # we may use the bridge
RELEASED = 'released'       # an AI match is running; the entry belongs to the AI
FOREIGN = 'foreign'         # someone else owns the entry and we could not take it


def u32(bridge, address):
    return struct.unpack('<I', bridge.read(address, 4))[0]


class AiModes:
    """The same per-match test the client's AI worker applies."""

    def __init__(self, bridge, game):
        self.bridge = bridge
        self.game_root = Path(game)
        self.record = self._launch_record()
        self.route_maps = {str(name).casefold()
                           for name in (self.record.get('route_maps') or [])}
        self.hero_enabled = bool(self.record.get('hero_mode_ai', True))
        self.worker_pid = int(self.record.get('route_worker_pid') or 0)
        self.pve_prefixes = self._pve_prefixes()
        self.game_offset = None
        self.rule_offsets = None

    @staticmethod
    def _pve_prefixes():
        """Read the client's own PVE prefix list so it cannot drift from the AI worker."""
        try:
            from local_pve_allies import GAME_PREFIXES
            prefixes = tuple(str(p).casefold() for p in GAME_PREFIXES)
            return prefixes or PVE_PREFIXES
        except Exception:                          # noqa: BLE001
            return PVE_PREFIXES

    def _launch_record(self):
        for relative in LAUNCH_RECORD:
            path = self.game_root / relative
            try:
                if path.is_file():
                    return json.loads(path.read_text('utf-8'))
            except (OSError, ValueError):
                continue
        return {}

    # ------------------------------------------------------------------ probes
    def world(self):
        """AWorldInfo* of the current world (mirrors local_route_watch.current_world)."""
        world = u32(self.bridge, GWORLD)
        if world < UINT32:
            return 0
        level = u32(self.bridge, world + LEVEL)
        if level < UINT32:
            return 0
        actors = u32(self.bridge, level + ACTORS)
        if not actors:
            return 0
        return u32(self.bridge, actors)

    def _rules(self):
        """Property offsets for the match's CustomModeType (knife rooms)."""
        if self.rule_offsets is None:
            self.rule_offsets = {}
            wanted = {path: key for key, path in RULE_PROPERTIES.items()}
            try:
                for address, name, cls in self.bridge.objects():
                    key = wanted.get(self.bridge.path(address))
                    # CustomModeType is an IntProperty, not an object link.
                    if key and cls.endswith('Property'):
                        self.rule_offsets[key] = struct.unpack(
                            '<I', self.bridge.read(address + 0x64, 4))[0]
            except (OSError, RecursionError):
                pass
        return self.rule_offsets

    def custom_mode(self):
        """The match's CustomModeType, 0 when unknown (reads the game, then the GRI)."""
        try:
            offsets = self._rules()
            world = self.world()
            if not world:
                return 0
            for owner_key, rule_key in (('game', 'game_rule'), ('gri', 'gri_rule')):
                owner_offset = offsets.get(owner_key)
                rule_offset = offsets.get(rule_key)
                if not owner_offset or not rule_offset:
                    continue
                owner = u32(self.bridge, world + owner_offset)
                if not owner:
                    continue
                rule = struct.unpack('<i', self.bridge.read(owner + rule_offset, 4))[0]
                if rule:
                    return rule
        except (OSError, RecursionError, struct.error):
            pass
        return 0

    def _offset(self):
        if self.game_offset is None:
            for address, name, cls in self.bridge.objects():
                if cls == 'ObjectProperty' and name == 'Game' \
                        and self.bridge.path(address) == GAME_PROPERTY:
                    self.game_offset = struct.unpack(
                        '<I', self.bridge.read(address + 0x64, 4))[0]
                    break
            else:
                self.game_offset = 0
        return self.game_offset

    def match(self):
        """(is_ai_match, reason) for the world that is loaded right now."""
        try:
            world = self.world()
            if not world:
                return False, 'no-world'
            offset = self._offset()
            if not offset:
                return False, 'no-game-property'
            game = u32(self.bridge, world + offset)
            if not game:
                return False, 'no-game'
            cls = self.bridge.object_name(game).casefold()
            if cls.startswith(HERO_PREFIX) and self.hero_enabled:
                return True, 'hero:' + cls
            if cls.startswith(OBJECTIVE_PREFIXES):
                return True, 'objective:' + cls
            if cls.startswith(self.pve_prefixes):
                return True, 'pve:' + cls
            if self.custom_mode() == KNIFE_RULE:
                return True, 'knife:' + cls
            map_name = self.bridge.path(world).split('.')[0].casefold()
            if map_name in self.route_maps and cls.startswith(AI_TEAM_PREFIXES):
                return True, 'route:' + map_name
            return False, cls
        except (OSError, RecursionError):
            return False, 'unreadable'

    def worker_alive(self):
        """Is the R6 AI worker process running at all?"""
        if not self.worker_pid:
            return False
        try:
            import ctypes
            import ctypes.wintypes as w
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            handle = k.OpenProcess(0x1000, False, self.worker_pid)
            if not handle:
                return False
            try:
                code = w.DWORD(0)
                if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                k.CloseHandle(handle)
        except Exception:                          # noqa: BLE001
            return False


class Gate:
    """Decides whether this mod owns the native entry, and moves it between owners."""

    def __init__(self, bridge, main_thread, game, config, emit):
        self.bridge = bridge
        self.main_thread = main_thread
        self.modes = AiModes(bridge, game)
        self.config = config
        self.emit = emit
        self.state = OWNED if getattr(bridge, 'owned', False) else FOREIGN
        self.next_poll = 0.0
        self.release_since = 0.0
        self.last_foreign = 0.0
        self.last_reason = ''

    def allowed(self):
        return self.state == OWNED

    # ------------------------------------------------------------------ policy
    def refresh(self, now=None):
        if not self.config.get('ai_priority', True):
            return self.allowed()
        now = time.monotonic() if now is None else now
        if now < self.next_poll:
            return self.allowed()
        self.next_poll = now + max(0.2, float(self.config.get('ai_priority_poll_seconds', 1.0)))
        if not self.modes.worker_alive():
            # No AI worker in this session: nothing to yield to.
            if not self.allowed():
                self.acquire(now, 'no-ai-worker')
            return self.allowed()
        is_ai, reason = self.modes.match()
        if is_ai:
            if self.state == OWNED:
                self.release(reason)
            elif self.last_reason != reason:
                self.last_reason = reason
                self.emit(event='ai-priority', state=RELEASED, reason=reason,
                          note='AI worker owns the native entry in this match')
            return self.allowed()
        if not self.allowed():
            self.acquire(now, 'free-play')
        return self.allowed()

    def release(self, reason):
        """Put the original entry bytes back so the AI worker can install its own."""
        try:
            self.bridge.restore()
        except Exception as error:                 # noqa: BLE001
            self.emit(event='bridge-release-failed', error=str(error))
            return
        self.state = RELEASED
        self.release_since = time.monotonic()
        self.last_reason = reason
        self.emit(event='ai-priority', state=RELEASED, reason=reason,
                  note='bridge handed back to the AI worker')

    def acquire(self, now, reason):
        """Try to take the entry back; only if it is free and the match has settled."""
        settle = float(self.config.get('ai_priority_settle_seconds', 2.0))
        if self.release_since and now - self.release_since < settle:
            return False
        # `Bridge.state()` is the authority: it installs when the entry is free and
        # adopts only this session's own record, so a leftover install from an
        # earlier worker run is reclaimed instead of being mistaken for the AI's.
        try:
            self.bridge.state(self.main_thread)
        except Exception as error:                 # noqa: BLE001
            self.state = FOREIGN
            if now - self.last_foreign >= 60.0:
                self.last_foreign = now
                self.emit(event='bridge-foreign', note=(
                    'native entry belongs to another bridge (AI worker); staying passive'),
                    error=str(error))
            return False
        self.state = OWNED
        self.release_since = 0.0
        self.emit(event='ai-priority', state=OWNED, reason=reason,
                  note='bridge taken back')
        return True
