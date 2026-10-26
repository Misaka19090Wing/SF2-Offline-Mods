"""Arrows and throwing knives as they fly through the level.

Bullets never appear as actors, but projectiles do: the client spawns an
`SFCProjectile*` blueprint in the persistent level and moves it every tick.  That
gives us the one thing a bullet never provides -- the real flight path.

We keep the sampled positions of every live projectile, and report the moment
the game hides it: a thrown weapon that strikes a body is hidden on contact,
while one that lands on the ground keeps its model until it is cleaned up many
seconds later.  The position read in the same poll as that flip is the hit
point, which is what the caller tests against the ragdoll bones.
"""
import time

from . import game

MAX_SPEED = 12000.0                 # units/s a round may travel (arrow: ~8000)
FLAGS = 0xB0                        # Engine.Actor.bHidden / bDeleteMe
HIDDEN = 0x2


class Projectiles:
    def __init__(self, bridge, pawns, samples=512, lifetime=3.0):
        self.bridge = bridge
        self.pawns = pawns
        self.limit = samples
        self.lifetime = lifetime
        self.tracked = {}               # actor address -> entry

    def poll(self):
        """[(kind, entry)], kind is 'spawn', 'move', 'hide' or 'gone'."""
        events = []
        try:
            _, actors = self.pawns.level()
        except OSError:
            return events
        now = time.monotonic()
        live = set()
        for address in actors:
            if address < 0x10000:
                continue
            name = self.pawns.kind(address)
            if not name or not name.startswith(game.PROJECTILE_PREFIXES):
                continue
            position = game.vector(self.bridge, address + game.LOCATION)
            if position is None:
                continue
            live.add(address)
            entry = self.tracked.get(address)
            if entry is None:
                entry = {'address': address, 'klass': name,
                         'path': [position], 't': now, 'stamp': now,
                         'hidden': False,
                         'velocity': game.vector(self.bridge, address + game.VELOCITY)}
                self.tracked[address] = entry
                events.append(('spawn', entry))
                continue
            elapsed = max(now - entry['stamp'], 0.0005)
            step = sum((position[i] - entry['path'][-1][i]) ** 2 for i in range(3)) ** 0.5
            # A freed object stays in the level array for a tick and reads as a
            # jump of thousands of units.  Compare against how fast such a round
            # can actually fly (8000 units/s) so a slow poll loop is not mistaken
            # for garbage.
            if step > MAX_SPEED * elapsed + 200.0:
                continue
            entry['stamp'] = now
            moved = step > 0.01
            if moved:
                entry['path'].append(position)
                if len(entry['path']) > self.limit:
                    del entry['path'][0]
            entry['velocity'] = game.vector(self.bridge, address + game.VELOCITY)
            if not entry['hidden']:
                try:
                    flags = game.u32(self.bridge, address + FLAGS)
                except OSError:
                    continue
                if flags & HIDDEN:
                    # Reported for the log only: on a live match this flips when
                    # the round leaves the playfield, ~15 m from any corpse.
                    entry['hidden'] = True
                    entry['hide_at'] = position
                    entry['hide_age'] = now - entry['t']
                    events.append(('hide', entry))
            if moved:
                events.append(('move', entry))
        for address in [a for a in self.tracked if a not in live]:
            events.append(('gone', self.tracked.pop(address)))
        return events
