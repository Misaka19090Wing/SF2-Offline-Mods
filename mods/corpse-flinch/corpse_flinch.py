"""corpse-flinch: every bullet that really hits a corpse pushes its ragdoll.

Run it next to a running game:

    runtime\\python\\python.exe -X utf8 corpse_flinch.py [--dry] [--strength 700]

It attaches to the client, reads the weapon's own impact list after each shot, and
asks the engine to apply an impulse at the exact point the bullet landed. Nothing
in the game files is touched; the only memory write is the ProcessEvent trampoline
the client's own tooling installs at runtime.

Log is one JSON object per line (stdout and logs/*.jsonl).
"""
import argparse
import json
import math
import os
import re
import struct
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sf2mod                       # noqa: E402
from sf2mod import attach, game, punch as punch_mod, signals, priority  # noqa: E402
from sf2mod import projectiles as projectiles_mod, swing as swing_mod   # noqa: E402
from sf2mod import drag as drag_mod, keys as keys_mod   # noqa: E402

# Bones that look like a handle but have no body of their own: the impulse is
# dropped and the servo would push at full strength forever.
HELPER_BONE = re.compile(r'(^IK_|_IK\b|Twist|Finger|Thumb|Toe|Bottle|Weapon|^root$)', re.I)
# Pull the body by its centre of mass, not by whatever you happen to aim at:
# an impulse at the head or a hand is mostly torque, and the body tumbles.
CORE_BONE = re.compile(r'(Pelvis|Spine|Torso|Hips)', re.I)

# Bumped with every release. It shows up in `--version` and in the `ready` line,
# so a bug report always carries the build it came from.
VERSION = '1.11.2'

DEFAULTS = {
    'strength': 700.0,              # impulse, calibrated band 600..1600
    'max_impulse': 1800.0,
    'settle_seconds': 0.35,
    'poll_seconds': 0.004,
    'wait_for_game': 120.0,         # start before the game if a launcher does it
    'log_dir': 'logs',
    'melee': True,                  # the F swing
    'melee_reach': 130.0,           # cm from the eye to the nearest corpse bone
    'melee_strength': 700.0,
    'swing_delay': 0.30,            # wait this long to see whether ammo moved
    'projectile': True,             # arrows and throwing knives
    'projectile_radius': 70.0,      # how close the flight path must pass a bone
    'projectile_grace': 0.02,       # one sample: the round still in the hand
    'projectile_launch': 0.15,      # this early, the body must be ahead of you
    'projectile_lifetime': 2.0,     # seconds in the air: a hit dies at once, a miss lands
    'projectile_strength': 1080.0,
    'drag': True,                   # tap the use key (E) to grab / let go a body
    'drag_key': 0x45,               # VK_E, only used until the game flag answers
    'drag_reach': 300.0,            # cm from the eye to the nearest bone
    'drag_distance': 200.0,         # cm in front of the camera the body is held
    'drag_lift': 0.0,               # extra cm above the aim point
    'drag_hz': 60.0,                # control ticks per second (not the 250 Hz poll)
    'drag_kp': 20.0,                # spring: cm/s^2 per cm of error
    'drag_kd': 9.0,                 # damping: cm/s^2 per cm/s (2*sqrt(kp) = critical)
    'drag_max_speed': 700.0,        # cm/s, the fastest it closes in
    'drag_max_accel': 2500.0,       # cm/s^2 cap for the controller
    'drag_mass': 20.0,              # kg, learned on the fly (see drag._learn_mass)
    'drag_impulse': 900.0,          # cap for one control tick
    'drag_gravity': 1.0,            # 0 = the body sags, 1 = it hovers where you point
    'drag_break': 600.0,            # let go after being this far off for 0.3 s
    'ai_priority': True,            # hand the native entry to the R6 AI worker
    'ai_priority_poll_seconds': 1.0,  # how often the match is re-checked
    'ai_priority_settle_seconds': 2.0,  # wait this long before taking the entry back
}


def load_config(path=None):
    config = dict(DEFAULTS)
    path = Path(path) if path else HERE / 'config.json'
    if path.is_file():
        config.update(json.loads(path.read_text(encoding='utf-8')))
    return config


def make_logger(directory):
    directory = Path(directory)
    if not directory.is_absolute():
        directory = HERE / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime('corpse-flinch-%Y%m%d.jsonl')
    handle = path.open('a', encoding='utf-8')

    def emit(**fields):
        line = json.dumps(fields, ensure_ascii=False)
        if sys.stdout is not None:              # pythonw: no console at all
            print(line, flush=True)
        handle.write(line + '\n')
        handle.flush()

    emit.file = path                           # type: ignore[attr-defined]
    return emit


class Worker:
    def __init__(self, bridge, api, config, emit, dry=False):
        self.bridge = bridge
        self.api = api
        self.pid = int(getattr(bridge, 'pid', 0) or 0)   # the *game* process
        self.config = config
        self.emit = emit
        self.pawns = game.Pawns(bridge)
        self.player = game.Player(bridge, self.pawns)
        self.gate = priority.Gate(bridge, getattr(bridge, 'main_thread', None),
                                  attach.game_dir(HERE), config, emit)
        self.punch = punch_mod.Punch(bridge, api,
                                     strength=config['strength'],
                                     max_impulse=config['max_impulse'],
                                     dry=dry, settle=config['settle_seconds'],
                                     emit=emit, allowed=self.gate.allowed)
        self.weapon = 0
        self.ammo = None
        self.signature = None
        self.shots = 0
        self.dropped = 0
        self.world = 0
        self.last = {}                          # corpse actor -> last impulse key
        self.swing = swing_mod.Swing(bridge)
        self.projectiles = projectiles_mod.Projectiles(bridge, self.pawns)
        self.keys = keys_mod.Keys(bridge, config)
        self.drag = drag_mod.Drag(bridge, api, config, emit)
        self.melee_hits = 0
        self.projectile_hits = 0
        self.bone_cache = None
        self.bone_stamp = 0.0
        self.ammo_drop_at = 0.0                 # when the magazine last shrank
        self.pending_swing = None               # burst edge waiting for the ammo
        self.last_strike = 0.0                  # one push per swing

    # ------------------------------------------------------------------ shots
    def read_impacts(self):
        """Impacts of the newest shot, waiting briefly for the list to change."""
        deadline = time.monotonic() + 0.06
        while True:
            found = signals.read(self.bridge, self.weapon)
            current = signals.signature(found)
            if found and current != self.signature:
                self.signature = current
                return found
            if time.monotonic() > deadline:
                if found:
                    self.signature = current
                return found
            time.sleep(0.002)

    def name_of(self, address):
        if address <= 0x10000:
            return None
        try:
            return self.bridge.object_name(address)
        except (OSError, RecursionError):
            return None

    @staticmethod
    def plausible(found):
        """Whether the impact list looks like a real bullet trace.

        Only `SFWeap_Bullet` keeps this list; with a bow, a knife or a grenade
        in hand the very same offset reads unrelated memory, which used to fill
        the log with coordinates like 2.5e+21 and phantom actors.
        """
        hits = [entry for entry in found if entry['list'] == 'impact']
        if not hits:
            return False
        for entry in hits:
            if not entry['actor']:
                return False
            if not all(abs(value) < 100000.0 for value in entry['loc']):
                return False
        return True

    def shot(self):
        self.shots += 1
        found = self.read_impacts()
        if not self.plausible(found):
            self.emit(event='shot-miss', shots=self.shots, ammo=self.ammo,
                      rounds=self.dropped, impacts='no-bullet-trace')
            return
        # Only entry points count: the penetration list holds where the round
        # left the body again, and would double every hit.
        hits = [entry for entry in found if entry['list'] == 'impact']
        corpses = [(entry, self.pawns.is_corpse(entry['actor'])) for entry in hits]
        corpses = [(entry, dead) for entry, dead in corpses if dead]
        record = {'event': 'shot', 'shots': self.shots, 'ammo': self.ammo,
                  'rounds': self.dropped,
                  'impacts': signals.describe(found, self.name_of)}
        if not corpses:
            record['event'] = 'shot-miss'
            self.emit(**record)
            return
        record['corpses'] = [self.name_of(entry['actor']) for entry, _ in corpses]
        applied = []
        seen = set()
        for entry, _ in corpses:
            actor = entry['actor']
            if actor in seen or entry['list'] == 'penetration':
                continue                            # exit points are not new hits
            seen.add(actor)
            mesh = game.u32(self.bridge, actor + game.MESH)
            key = (tuple(round(v, 1) for v in entry['loc']),
                   tuple(round(v, 1) for v in entry['dir']))
            if self.last.get(actor) == key:
                continue                            # the list did not change: same shot
            self.last[actor] = key
            try:
                applied.append(self.punch.apply(mesh, entry['loc'], entry['dir']))
                applied[-1]['corpse'] = self.name_of(actor)
            except Exception as exc:                # noqa: BLE001
                applied.append({'corpse': self.name_of(actor),
                                'error': f'{type(exc).__name__}: {exc}'})
        record['event'] = 'corpse-hit' if not self.punch.dry else 'corpse-hit-dry'
        record['applied'] = applied
        self.emit(**record)

    # ------------------------------------------------------------ melee / bow
    BONE_TTL = 0.25              # seconds a corpse pose is reused

    def corpse_meshes(self, force=False):
        """[(actor, mesh, [bone world positions])] for every ragdoll on the map.

        Reading 70 bones per corpse costs 70 round trips, and the poll loop used
        to do it for every projectile sample -- which is why the loop ran at a
        few Hz and the flight path was coarse.  Cache the poses briefly instead:
        250 ms is far below the time a body needs to move and buys a ~10x faster
        sample rate.
        """
        now = time.monotonic()
        if not force and self.bone_cache is not None and now - self.bone_stamp < self.BONE_TTL:
            return self.bone_cache
        out = []
        for address in list(self.pawns.live):
            if not self.pawns.is_corpse(address):
                continue
            mesh = game.u32(self.bridge, address + game.MESH)
            points = game.bones(self.bridge, mesh)
            if points:
                out.append((address, mesh, points))
        self.bone_cache = out
        self.bone_stamp = now
        return out

    @staticmethod
    def nearest_bone(point, corpses):
        """(distance, actor, mesh, bone index, bone position) of the closest bone."""
        best = None
        for address, mesh, points in corpses:
            for index, bone in enumerate(points):
                distance = math.dist(point, bone)
                if best is None or distance < best[0]:
                    best = (distance, address, mesh, index, bone)
        return best

    @staticmethod
    def path_hit(path, corpses, radius):
        """Closest approach between the flight path and any corpse bone."""
        best = None
        for start, end in zip(path, path[1:]):
            span = tuple(b - a for a, b in zip(start, end))
            length2 = sum(v * v for v in span) or 1.0
            for address, mesh, points in corpses:
                for index, bone in enumerate(points):
                    along = sum((b - a) * (p - a) for a, b, p in
                                zip(start, end, bone)) / length2
                    along = max(0.0, min(1.0, along))
                    point = tuple(a + v * along for a, v in zip(start, span))
                    distance = math.dist(point, bone)
                    if distance <= radius and (best is None or distance < best[0]):
                        best = (distance, address, mesh, index, point, span, bone)
        return best

    def direction(self, fallback=None):
        _, forward = game.camera(self.bridge, self.player.find())
        return forward or fallback or (0.0, 0.0, -1.0)

    def melee(self):
        try:
            triggers = self.swing.poll(self.player.pawn(), self.player.weapon())
        except OSError:
            return                              # pawn swapped mid read
        if not triggers:
            return
        if not self.config.get('melee', True):
            return
        location, forward = game.camera(self.bridge, self.player.find())
        if location is None:
            return
        if 'exigency' in triggers:
            self.strike(location, forward, 'f-key')
            return
        # A burst edge is a rifle shot, a throw or a swing alike; only the
        # magazine tells them apart, and it needs a moment to tick down.
        self.pending_swing = {'at': time.monotonic(), 'location': location,
                              'forward': forward, 'drop_before': self.ammo_drop_at}

    def resolve_swing(self):
        """Decide a burst edge once the magazine had time to move."""
        pending = self.pending_swing
        if not pending:
            return
        if time.monotonic() - pending['at'] < self.config['swing_delay']:
            return
        self.pending_swing = None
        if self.ammo_drop_at > pending['drop_before']:
            return                              # ammo left the gun: not a swing
        self.strike(pending['location'], pending['forward'], 'weapon-swing')

    def strike(self, location, forward, source):
        """Push the nearest corpse bone if the swing could have reached it."""
        if time.monotonic() - self.last_strike < 0.45:
            return                              # both flags fired for one swing
        self.emit(event='swing', source=source, n=self.swing.count)
        corpses = self.corpse_meshes()
        best = self.nearest_bone(location, corpses)
        if best is None or best[0] > self.config['melee_reach']:
            self.emit(event='melee-miss', source=source,
                      near=None if best is None else round(best[0], 1))
            return
        self.last_strike = time.monotonic()
        distance, actor, mesh, index, bone = best
        scale = self.config['melee_strength'] / (self.config['strength'] or 1.0)
        record = {'event': 'melee-hit', 'source': source,
                  'corpse': self.name_of(actor),
                  'bone': index, 'distance': round(distance, 1)}
        try:
            record['applied'] = self.punch.apply(
                mesh, bone, self.slide(forward) or forward or (0.0, 0.0, -1.0), scale)
            self.melee_hits += 1
        except Exception as exc:                # noqa: BLE001
            record['error'] = f'{type(exc).__name__}: {exc}'
        self.emit(**record)

    def pawn_location(self):
        pawn = self.player.pawn()
        if not pawn:
            return None
        return game.vector(self.bridge, pawn + game.LOCATION)

    def grabbable(self, point, corpses):
        """(distance, actor, mesh, index, bone, name) for each bone, nearest first."""
        rows = []
        for address, mesh, points in corpses:
            for index, bone in enumerate(points):
                gap = math.dist(point, bone)
                if gap <= self.config['drag_reach']:
                    rows.append((gap, address, mesh, index, bone))
        rows.sort(key=lambda row: row[0])
        out = []
        for gap, address, mesh, index, bone in rows[:12]:
            label, _ = self.drag.name_of(mesh, index)
            out.append((gap, address, mesh, index, bone, label))
        return out

    def toggle_drag(self, source):
        """Use key: take hold of the nearest body, press again to let go.

        The body is not teleported -- sf2mod/drag.py pulls the grabbed bone with
        one impulse per poll, so it swings, catches on walls and follows the
        player instead of gliding through the level.
        """
        if not self.config.get('drag', True):
            return
        if self.drag.active:
            self.emit(event='drag-end', **self.drag.release('toggled'))
            return
        location, _ = game.camera(self.bridge, self.player.find())
        if location is None:
            return
        corpses = self.corpse_meshes(force=True)
        best = self.nearest_bone(location, corpses)
        record = {'event': 'drag-start', 'source': source, 'key': self.keys.count,
                  'keys_word': None if self.keys.word is None else hex(self.keys.word)}
        if best is None or best[0] > self.config['drag_reach']:
            record['event'] = 'drag-none'
            record['near'] = None if best is None else round(best[0], 1)
            record['reach'] = self.config['drag_reach']
            self.emit(**record)
            return
        aim = best[0]                   # how far the crosshair bone was
        distance, actor, mesh, index, bone = best
        # Grab the corpse by its pelvis, not by the atom under the crosshair:
        # an impulse at a hand or a head is almost all torque, and the body
        # tumbles out of the level instead of following you.  The pelvis sits
        # at the centre of mass, so the same push is a pure translation.
        points = next((p for a, _, p in corpses if a == actor), None)
        if points:
            core = self.drag.center_index(mesh, points)
            if core is not None:
                index = core
                bone = points[index]
                distance = math.dist(location, bone)
        try:
            record.update(self.drag.grab(actor, mesh, index, bone,
                                         self.pawn_location()))
        except (OSError, struct.error) as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            self.emit(**record)
            return
        record.update({'corpse': self.name_of(actor), 'gap': round(aim, 1),
                       'held_at': round(distance, 1)})
        self.emit(**record)

    @staticmethod
    def slide(direction):
        """Corpses lie on the floor: a straight-down push only grinds them in.

        Keep the horizontal heading of the shot and add a tiny lift so the body
        skates across the ground instead of being pinned into it.
        """
        x, y = (direction or (0.0, 0.0))[0], (direction or (0.0, 0.0))[1]
        length = math.hypot(x, y)
        if length < 1e-3:
            return None
        return (x / length, y / length, 0.08)

    def arrows(self):
        events = self.projectiles.poll()
        if not self.config.get('projectile', True):
            return
        corpses = None
        for kind, entry in events:
            if kind == 'spawn':
                # Without this line a throw that never got tracked and a throw
                # that missed both look like "nothing happened".
                self.emit(event='projectile-spawn', klass=entry['klass'],
                          address=hex(entry['address']),
                          from_=[round(v, 1) for v in entry['path'][0]])
                continue
            if kind not in ('move', 'gone') or entry.get('scored'):
                continue
            if len(entry['path']) < 2:
                if kind == 'gone':
                    self.emit(event='projectile-lost', klass=entry['klass'],
                              age=round(time.monotonic() - entry['t'], 3),
                              points=len(entry['path']),
                              why='sampled once')
                continue
            if kind == 'move':
                # Judge every new segment as it is flown, so the push lands the
                # moment the round crosses the body -- waiting for the actor to
                # be freed cost a full second of delay.
                segment = entry['path'][-2:]
                age = time.monotonic() - entry['t']
                # One sample only: that is the round still in the hand.  A time
                # based grace used to swallow the first 0.12 s -- at 24 m/s that
                # is nearly 3 m of flight, i.e. the whole contact of a close
                # throw, and the push then arrived a second later when the axe
                # came to rest next to the body.
                if age < self.config['projectile_grace']:
                    continue
            else:
                age = time.monotonic() - entry['t']
                if age > self.config['projectile_lifetime']:
                    continue                    # landed and stayed: never a hit
                segment = entry['path']         # fallback: whole flight
            if corpses is None:
                corpses = self.corpse_meshes()
            best = self.path_hit(segment, corpses, self.config['projectile_radius'])
            if best is None:
                if kind == 'gone':
                    near = self.path_hit(entry['path'], corpses, 1e9)
                    self.emit(event='projectile-miss', klass=entry['klass'],
                              age=round(time.monotonic() - entry['t'], 2),
                              near=None if near is None else round(near[0], 1))
                continue
            distance, actor, mesh, index, point, span, bone = best
            if kind == 'move' and age < self.config['projectile_launch']:
                # Standing on a corpse: its bones are right under the hand and
                # the launch sample is genuinely close to them.  In the first
                # fraction of a second only count a body that is *ahead* -- and
                # only horizontally, because a round thrown downwards would
                # otherwise "look ahead" of the body under your feet.
                origin = entry['path'][0]
                if sum((b - o) * s for b, o, s in zip(bone[:2], origin[:2], span[:2])) <= 0.0:
                    continue
            entry['scored'] = True
            direction = entry.get('velocity') or span
            if not any(direction):
                direction = self.direction(span)
            direction = self.slide(direction) or direction
            scale = self.config['projectile_strength'] / (self.config['strength'] or 1.0)
            record = {'event': 'projectile-hit', 'corpse': self.name_of(actor),
                      'bone': index, 'distance': round(distance, 1),
                      'klass': entry['klass'],
                      'age': round(time.monotonic() - entry['t'], 3),
                      'path': len(entry['path'])}
            try:
                record['applied'] = self.punch.apply(mesh, point, direction, scale)
                self.projectile_hits += 1
            except Exception as exc:            # noqa: BLE001
                record['error'] = f'{type(exc).__name__}: {exc}'
            self.emit(**record)

    # -------------------------------------------------------------------- run
    def target_lost(self):
        return not self.player.find()

    def run(self, seconds=0.0):
        end = time.monotonic() + seconds if seconds else None
        status = time.monotonic() + 20
        idle_since = time.monotonic()
        probe = time.monotonic() + 1.0
        while end is None or time.monotonic() < end:
            lap = time.monotonic()
            # The bridge happily answers from a dead process's memory, so a
            # closed game looked exactly like an empty map: same status line,
            # loop_ms 0.0, forever.  Notice the exit instead of idling until the
            # one-hour timeout, and leave worker.pid behind for nobody.
            if time.monotonic() >= probe:
                probe = time.monotonic() + 1.0
                if self.pid and not attach.pid_alive(self.pid):
                    self.emit(event='game-exit', pid=self.pid)
                    return
                # R6's bot AI needs this same native entry. Yield it while an
                # AI-driven match runs, take it back once that match ends.
                try:
                    self.gate.refresh()
                except Exception as exc:            # a policy bug must not end the run
                    self.emit(event='worker-error', where='ai-priority',
                              error=f'{type(exc).__name__}: {exc}')
            try:
                self.pawns.poll()
            except OSError:
                time.sleep(0.05)
                continue
            if self.pawns.world != self.world:
                self.world = self.pawns.world
                self.weapon = 0
                self.ammo = None
                self.signature = None
                self.pending_swing = None
                self.last.clear()
                if self.drag.active:
                    self.drag.release('world')
                self.emit(event='world', world=hex(self.world or 0),
                          tracked=len(self.pawns.live),
                          corpses=len(self.pawns.corpses()))
            # Each detector runs on its own: a stale pawn used to raise OSError
            # in the melee check and silently skip the projectile check too.
            for step in (self.melee, self.arrows):
                try:
                    step()
                except OSError:
                    pass                        # actor disappeared mid read
                except Exception as exc:        # a bug here must not end the run
                    self.emit(event='worker-error', where=step.__name__,
                              error=f'{type(exc).__name__}: {exc}')
            try:
                for source in self.keys.poll(self.player.find()):
                    self.toggle_drag(source)
                if self.drag.active:
                    eye, forward = game.camera(self.bridge, self.player.find())
                    ended = self.drag.step(eye, forward, self.pawn_location())
                    if ended:
                        self.emit(event='drag-end', **ended)
            except OSError:
                pass                            # controller or mesh vanished
            except Exception as exc:            # a bug here must not end the run
                self.emit(event='worker-error', where='drag',
                          error=f'{type(exc).__name__}: {exc}')
                if self.drag.active:
                    self.drag.release('invoke-failed')
            weapon = self.player.weapon()
            if weapon != self.weapon:
                self.weapon = weapon
                self.ammo = None
                self.signature = None
                self.last.clear()
                if weapon:
                    self.emit(event='weapon', weapon=hex(weapon),
                              name=self.name_of(weapon))
                else:
                    idle_since = time.monotonic()
            if weapon:
                ammo = self.player.ammo(weapon)
                previous, self.ammo = self.ammo, ammo
                # Any drop means at least one round left the barrel; a fast
                # burst can drop by 2+ between two polls, and the impact list
                # only ever holds the newest shot, so count it once.
                if previous is not None and ammo < previous:
                    self.dropped = previous - ammo
                    self.ammo_drop_at = time.monotonic()
                    try:
                        self.shot()
                    except OSError as exc:
                        self.emit(event='shot-failed', error=str(exc))
                if self.punch.pending:
                    self.punch.settle()
            try:
                self.resolve_swing()
            except OSError:
                pass                            # pawn vanished mid decision
            if time.monotonic() >= status:
                self.emit(event='status', tracked=len(self.pawns.live),
                          corpses=len(self.pawns.corpses()), shots=self.shots,
                          hits=self.punch.hits, melee=self.melee_hits,
                          projectiles=self.projectile_hits, ammo=self.ammo,
                          drag=self.drag.active,
                          loop_ms=round((time.monotonic() - lap) * 1000.0, 1))
                status = time.monotonic() + 20
            if not weapon and time.monotonic() - idle_since > 3600:
                self.emit(event='idle-exit')
                return
            time.sleep(self.config['poll_seconds'])


def parse(argv=None):
    parser = argparse.ArgumentParser(description='让被子弹真正命中的尸体产生布娃娃反应')
    parser.add_argument('--version', action='version', version=f'corpse-flinch {VERSION}')
    parser.add_argument('--config', default=None, help='配置文件（默认 config.json）')
    parser.add_argument('--pid', type=int, default=0, help='客户端 PID（默认自动查找）')
    parser.add_argument('--strength', type=float, default=None, help='冲量强度（默认 700）')
    parser.add_argument('--max-impulse', type=float, default=None, help='冲量上限（默认 1800）')
    parser.add_argument('--dry', action='store_true', help='只记录命中，不施加冲量')
    parser.add_argument('--wait', type=float, default=None, help='等待游戏启动的秒数')
    parser.add_argument('--seconds', type=float, default=0.0, help='运行多少秒（0=直到退出）')
    parser.add_argument('--log-dir', default=None, help='日志目录')
    return parser.parse_args(argv)


BOOT_GRACE = 3600.0       # seconds we keep waiting for the client / first match
BOOT_LOG_EVERY = 60.0     # how often a repeating wait is re-logged


def connect_when_booted(game, battle, route, main_thread, pid, wait, log, emit,
                        grace=BOOT_GRACE):
    """Attach, but tolerate a client that is still loading or still in the menus.

    Two startup gates showed up once the launcher started the game itself:

    * right after sf2.exe appears the UObject array is still empty, so
      `Reader.objects()` raises `ValueError: Unexpected object array`;
    * the bundled bridge can only install its trampoline once a match exists
      (`RuntimeError: Battle API incomplete: N functions, 0 controllers`), so
      attaching from the main menu fails no matter how long we wait.

    Both used to be fatal: the worker logged `{"event": "fatal", ...}` and quit
    a second after launch, which is what "installed 1.10.1, still nothing
    happens" was.  Now the worker waits for the first match instead of dying
    (logging `waiting-boot`, throttled to once a minute, and `booted` when it
    finally gets in).
    """
    deadline = time.monotonic() + grace
    started = time.monotonic()
    attempt = 0
    announced = set()
    last_error = None
    next_log = 0.0

    def dedupe(text):
        """connect() logs `process` on every attempt; keep the log readable."""
        try:
            event = json.loads(text).get('event')
        except (TypeError, ValueError):
            event = None
        if event in ('process', 'attached') and event in announced:
            return
        announced.add(event)
        log(text)

    while True:
        try:
            bridge, api = attach.connect(game, battle, route, main_thread,
                                         pid=pid, wait=wait, log=dedupe)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:                    # noqa: BLE001
            attempt += 1
            now = time.monotonic()
            if now >= deadline:
                emit(event='boot-timeout', attempts=attempt,
                     error=f'{type(exc).__name__}: {exc}')
                raise
            error = f'{type(exc).__name__}: {exc}'
            pause = min(1.0 + attempt, 5.0)
            if error != last_error or now >= next_log:
                emit(event='waiting-boot', attempt=attempt, error=error,
                     waited=round(now - started, 1), retry_in=round(pause, 1))
                last_error = error
                next_log = now + BOOT_LOG_EVERY
            time.sleep(pause)
        else:
            if attempt:
                emit(event='booted', attempts=attempt,
                     waited=round(time.monotonic() - started, 1))
            return bridge, api


def stream_ok(stream):
    """A wrapper can exist and still be broken.

    pythonw.exe started from another console-less process inherits OS handles
    that look valid to Python but every write fails with OSError 22.  If we
    keep such a stream, the first `emit` kills the worker a second after it
    starts, with no visible error -- that is exactly the "installed it, nothing
    happens" report.  Probe once instead of trusting `is not None`.
    """
    if stream is None:
        return False
    try:
        stream.write('')
        stream.flush()
        return True
    except Exception:                                  # noqa: BLE001
        return False


def ensure_streams():
    """pythonw.exe (used by the launcher) has no console: keep the output anyway."""
    if stream_ok(sys.stdout) and stream_ok(sys.stderr):
        return
    stream = None
    try:
        directory = HERE / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        stream = (directory / 'worker-console.log').open('a', encoding='utf-8', buffering=1)
    except OSError:
        try:
            stream = open(os.devnull, 'w', encoding='utf-8')
        except OSError:
            return
    if not stream_ok(sys.stdout):
        sys.stdout = stream
    if not stream_ok(sys.stderr):
        sys.stderr = stream


def main(argv=None):
    ensure_streams()
    args = parse(argv)
    config = load_config(args.config)
    if args.strength is not None:
        config['strength'] = args.strength
    if args.max_impulse is not None:
        config['max_impulse'] = args.max_impulse
    if args.wait is not None:
        config['wait_for_game'] = args.wait
    if args.log_dir:
        config['log_dir'] = args.log_dir

    emit = make_logger(config['log_dir'])
    # The launcher reads this to avoid starting a second worker, and uninstall
    # uses it to stop the one that is holding the old files open.
    pidfile = Path(emit.file).parent / 'worker.pid'
    try:
        pidfile.write_text(str(os.getpid()), encoding='ascii')
    except OSError:
        pidfile = None

    def log_line(text):
        """attach.py talks in JSON strings; fold them into the same log."""
        try:
            emit(**json.loads(text))
        except (TypeError, ValueError):
            emit(event='attach', message=text)

    try:
        root = attach.game_dir()
        battle, route, main_thread = attach.load_runtime(root)
        bridge, api = connect_when_booted(root, battle, route, main_thread,
                                          pid=args.pid or None,
                                          wait=config['wait_for_game'],
                                          log=log_line, emit=emit)
    except SystemExit as exc:
        emit(event='fatal', reason=str(exc))
        raise
    except Exception as exc:                    # noqa: BLE001
        emit(event='fatal', reason=f'{type(exc).__name__}: {exc}')
        raise SystemExit(1)

    worker = Worker(bridge, api, config, emit, dry=args.dry)
    emit(event='ready', version=VERSION, pid=bridge.pid, dry=args.dry,
         strength=config['strength'], max_impulse=config['max_impulse'],
         add_impulse=hex(worker.punch.add_impulse),
         melee=config.get('melee', True), projectile=config.get('projectile', True),
         log=str(emit.file))
    try:
        worker.run(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        emit(event='stopped', shots=worker.shots, hits=worker.punch.hits,
             melee=worker.melee_hits, projectiles=worker.projectile_hits,
             log=str(emit.file))
        if pidfile is not None:
            try:
                pidfile.unlink()
            except OSError:
                pass
        bridge.close()


def crash_log(text):
    """Last resort: a traceback that reaches no console still reaches a file."""
    for directory in (HERE / 'logs',
                      Path(os.environ.get('TEMP', '.')) / 'corpse-flinch'):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / 'crash.log').open('a', encoding='utf-8') as handle:
                handle.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {text}\n')
            return
        except OSError:
            continue


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except BaseException:                              # noqa: BLE001
        crash_log(traceback.format_exc())
        raise
