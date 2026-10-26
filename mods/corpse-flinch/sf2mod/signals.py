"""The weapon's own hit record: which actor a bullet really struck, and where.

ASFWeap_Bullet.HitResult (@0xC90) is a `RifleHitResult` struct holding three
dynamic arrays:

    0xC90  ImpactInfos       TArray<FImpactInfo>   entries the bullet hit, in order
    0xC9C  PenetrationInfos  TArray<FImpactInfo>   where it came out again
    0xCA8  DamageScale       TArray<float>         per-entry damage falloff

Each FImpactInfo is 80 bytes:

    +0   AActor* HitActor
    +4   FVector HitLocation      exact world point of the hit
    +16  FVector HitNormal
    +28  FVector RayDir           the bullet's direction
    +40  FVector StartTrace
    +52  FImpactInfo.HitInfo      (28 bytes, the engine's inner hit info)

The game rewrites this list for every shot, so one ammo decrement gives the full,
authoritative answer for that bullet: wall and floor shots simply have no pawn in
the list, and the bone was already resolved by the game itself. That is why this
signal replaced the earlier "cast a ray and see which bone is nearby" heuristic,
which produced both false positives and misses at awkward angles.
"""
import struct

from . import game

IMPACT_SIZE = 80
LISTS = (('impact', game.IMPACT_INFOS), ('penetration', game.PENETRATION_INFOS))


def read(bridge, weapon):
    """Every FImpactInfo the weapon recorded for the most recent shot."""
    found = []
    for label, offset in LISTS:
        try:
            pointer, count, capacity = struct.unpack('<III', bridge.read(weapon + offset, 12))
        except OSError:
            continue
        if not 0 < count <= 64 or pointer < 0x10000:
            continue
        try:
            raw = bridge.read(pointer, count * IMPACT_SIZE)
        except OSError:
            continue
        for index in range(count):
            entry = raw[index * IMPACT_SIZE:(index + 1) * IMPACT_SIZE]
            found.append({
                'list': label,
                'index': index,
                'actor': struct.unpack_from('<I', entry, 0)[0],
                'loc': struct.unpack_from('<3f', entry, 4),
                'normal': struct.unpack_from('<3f', entry, 16),
                'dir': struct.unpack_from('<3f', entry, 28),
                'start': struct.unpack_from('<3f', entry, 40),
            })
    return found


def signature(found):
    """Cheap identity of a hit list, used to notice that a new shot happened."""
    return tuple((entry['list'], entry['actor'],
                  tuple(round(v, 2) for v in entry['loc'])) for entry in found)


def describe(found, name_of):
    """JSON-safe summary for the log."""
    return [{'list': e['list'], 'index': e['index'],
             'actor': name_of(e['actor']),
             'loc': [round(v, 1) for v in e['loc']],
             'dir': [round(v, 2) for v in e['dir']]} for e in found]
