"""When the local player swings: two flags, both read from memory.

There is no persistent melee hit record anywhere in this build: the melee code
calls TakeDamage from native code, so a script-level hook never sees it.  What it
does expose:

* `SFPawn_Player.DoingExigencyAttack` -- the F-key knife: a bit inside 0xED8 that
  stays set while the swing animation plays.
* `SFWeapon.BurstFireCount` (0xBE0, int) -- goes to -1 while *anything* fires and
  back to 0 when it ends: guns, grenades, throws and the left-click swing of a
  throwing weapon all raise it.

The second one is what makes the tomahawk swing visible, but it fires for a rifle
shot too, so the caller has to tell them apart -- a shot consumes ammo within a
few milliseconds, a swing never does (Worker.resolve_swing).
"""
from . import game

BURST_FIRE_COUNT = 0xBE0


class Swing:
    def __init__(self, bridge):
        self.bridge = bridge
        self.active = False
        self.burst = 0
        self.burst_weapon = 0
        self.count = 0

    def poll(self, pawn, weapon=0):
        """['exigency'] / ['burst'] / both / [] on the poll a swing starts."""
        triggers = []
        now = False
        if pawn > 0x10000:
            try:
                raw = self.bridge.read(pawn + game.DOING_EXIGENCY, 1)
                now = bool(raw[0] & game.EXIGENCY_BIT)
            except OSError:
                now = self.active
        rising = now and not self.active
        self.active = now
        if rising:
            triggers.append('exigency')
        if weapon > 0x10000:
            try:
                value = game.i32(self.bridge, weapon + BURST_FIRE_COUNT)
            except OSError:
                value = 0
            if weapon != self.burst_weapon:      # a fresh weapon is not a press
                self.burst_weapon = weapon
                self.burst = value
            elif value and not self.burst:
                triggers.append('burst')
            self.burst = value
        else:
            self.burst_weapon = 0
            self.burst = 0
        if triggers:
            self.count += 1
        return triggers
