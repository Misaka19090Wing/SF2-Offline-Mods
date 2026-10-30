"""Code-like health regeneration state machine.

Pure logic: no game access, no I/O, so it is unit tested offline.  The worker feeds
it the local player's (Health, HealthMax) on every tick and writes back whatever
`update()` returns.
"""


MAX_PLAUSIBLE_HEALTH = 100000


def bot_targets(rows, player_pawn, player_team):
    """Live teammates worth healing.

    rows: (address, team, health, maximum). The player's own pawn is excluded, and
    an unknown player team means nobody: healing the other team -- or a PVE
    monster, whose team is never the player's -- is worse than not healing at all.
    """
    if player_team is None:
        return []
    return [address for address, team, health, maximum in rows
            if address != player_pawn and team == player_team
            and health > 0 and 0 < maximum <= MAX_PLAUSIBLE_HEALTH
            and health <= maximum]


class Regen:
    """Regenerate health after `delay` seconds without damage.

    Rules that must not change (they are the whole point of a CoD-style regen):

    * damage restarts the delay -- health only climbs once `delay` seconds have
      passed since the last decrease;
    * a dead pawn is never healed (`Health <= 0` returns None, always);
    * health never exceeds `HealthMax`, and never goes down;
    * the amount written is whole health points, accumulated in a float so a low
      rate still works at a low tick rate.
    """

    def __init__(self, delay=4.0, per_second=25.0):
        if delay < 0:
            raise ValueError('regen_delay_seconds must be >= 0')
        if per_second <= 0:
            raise ValueError('regen_per_second must be > 0')
        self.delay = float(delay)
        self.per_second = float(per_second)
        self.reset()

    def reset(self, now=None):
        """Forget the pawn we were tracking (map change, respawn, new pawn)."""
        self.last_health = None
        self.last_damage = now
        self.credit = 0.0
        self.last_update = now

    def update(self, now, health, max_health):
        """Health to write, or None when this tick changes nothing."""
        if health is None or max_health is None:
            return None
        # A dead or unreachable pawn: never resurrect, and do not accumulate.
        if health <= 0:
            self.reset(now)
            return None
        # Bad reflection (wrong pawn, recycled address) must never become a write.
        if max_health <= 0 or max_health > MAX_PLAUSIBLE_HEALTH or health > max_health:
            if health > max_health:
                self.reset(now)
            return None
        if self.last_update is None:
            self.last_update = now
        if self.last_damage is None:
            self.last_damage = now
        if self.last_health is None:
            self.last_health = health

        if health < self.last_health:
            self.last_damage = now
            self.credit = 0.0
        self.last_health = health

        elapsed = now - self.last_update
        self.last_update = now
        if elapsed < 0 or elapsed > 0.5:      # paused / supervisor stall: no burst
            elapsed = 0.0

        if health >= max_health:
            self.credit = 0.0
            return None
        if now - self.last_damage < self.delay:
            self.credit = 0.0
            return None

        self.credit += self.per_second * elapsed
        whole = int(self.credit)
        if whole <= 0:
            return None
        target = min(health + whole, max_health)
        gained = target - health
        if gained <= 0:
            return None
        self.credit -= gained
        self.last_health = target               # our own write is not damage
        return target
