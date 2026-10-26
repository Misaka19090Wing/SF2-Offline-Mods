"""Push a ragdoll at the point the game says the bullet landed.

The call is Engine.PrimitiveComponent.AddImpulse(mesh, Impulse, BoneName,
Position, bVelChange) packed into a 36-byte frame:

    <3f  impulse    (world units per second * mass, i.e. what the engine expects)
    <3f  position   hit location from the impact list
    <Q   bone name  FName(0) = no specific bone, the impulse spreads from `position`
    <?   bVelChange False -- velocity changes are a no-op on this build
    3x   alignment

Empirical calibration on SFCPawn corpses (docs/02 has the raw numbers): a ragdoll
only starts sliding above ~600 of impulse and looks like it was hit by a car above
~1600, so the usable band is narrow, which is why strength defaults to 700 and a
`--strength` override exists.
"""
import math
import struct
import time

from . import game

ADD_IMPULSE = 'Engine.PrimitiveComponent.AddImpulse'


class Punch:
    def __init__(self, bridge, api, strength=700.0, max_impulse=1200.0,
                 dry=False, settle=0.35, emit=print, allowed=None):
        self.bridge = bridge
        self.functions = api.functions
        self.strength = strength
        self.max_impulse = max_impulse
        self.dry = dry
        self.settle_delay = settle
        self.emit = emit
        self.add_impulse = self.functions.get(ADD_IMPULSE)
        if not self.add_impulse:
            raise SystemExit('找不到 Engine.PrimitiveComponent.AddImpulse（游戏版本不匹配？）')
        self.pending = []
        self.hits = 0
        self.allowed = allowed or (lambda: True)
        self.skipped = 0

    # ------------------------------------------------------------------ impulse
    def magnitude(self, scale=1.0):
        return min(self.strength * scale, self.max_impulse)

    def apply(self, mesh, location, direction, scale=1.0):
        """AddImpulse at `location` along `direction`; returns the log record."""
        length = math.sqrt(sum(v * v for v in direction)) or 1.0
        unit = tuple(v / length for v in direction)
        amount = self.magnitude(scale)
        impulse = tuple(v * amount for v in unit)
        record = {'corpse_mesh': hex(mesh),
                  'hit': [round(v, 1) for v in location],
                  'dir': [round(v, 2) for v in unit],
                  'impulse': [round(v, 1) for v in impulse],
                  'strength': round(amount, 1)}
        if self.dry:
            record['dry'] = True
            return record
        if not self.allowed():
            # An AI match is running: the entry belongs to the AI worker, and an
            # impulse now would corrupt its batch block.
            self.skipped += 1
            record['skipped'] = 'ai-priority'
            return record
        start = game.matrix_translation(self.bridge, mesh)
        frame = struct.pack('<3f3fQ?3x', *impulse, *location, 0, False)
        self.bridge.invoke(mesh, self.add_impulse, frame, timeout=1.5)
        self.hits += 1
        self.pending.append({'mesh': mesh, 'from': start, 'amount': amount,
                             'at': time.monotonic() + self.settle_delay})
        return record

    # ------------------------------------------------------------------ settle
    def settle(self):
        """Report how far each pushed ragdoll actually travelled (cm)."""
        now = time.monotonic()
        for item in list(self.pending):
            if now < item['at']:
                continue
            self.pending.remove(item)
            here = game.matrix_translation(self.bridge, item['mesh'])
            if here is None or item['from'] is None:
                continue
            self.emit(event='settled', moved=round(math.dist(here, item['from']), 2),
                      strength=round(item['amount'], 1),
                      **{'from': [round(v, 1) for v in item['from']],
                         'to': [round(v, 1) for v in here]})
