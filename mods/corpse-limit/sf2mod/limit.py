"""Which corpses the engine should destroy now.

One rule each, unioned: too old, too far, or beyond the newest N. Nothing here
touches the game -- corpse_limit.py writes the result into `Engine.Actor.LifeSpan`
and the engine's own timer does the destroying.
"""


def decide(corpses, now, lifetime=0.0, max_distance=0.0, max_corpses=0, player=None):
    """Pick corpses to retire.

    corpses: [(address, first_seen, location)], in the order they were read.
    Returns {address: reason}; the caller turns each into a LifeSpan write.
    """
    expired = {}
    if lifetime > 0:
        for address, first_seen, _ in corpses:
            if now - first_seen >= lifetime:
                expired[address] = 'lifetime'
    if max_distance > 0 and player is not None:
        for address, _, location in corpses:
            if address not in expired and distance(location, player) > max_distance:
                expired[address] = 'distance'
    if max_corpses > 0 and len(corpses) > max_corpses:
        keep = {address for address, _, _ in sorted(corpses, key=sort_key)[-max_corpses:]}
        for address, _, _ in corpses:
            if address not in keep:
                expired.setdefault(address, 'count')
    return expired


def sort_key(corpse):
    """Oldest first; the address breaks ties so the choice is stable."""
    return (corpse[1], corpse[0])


def distance(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx * dx + dy * dy + dz * dz) ** 0.5
