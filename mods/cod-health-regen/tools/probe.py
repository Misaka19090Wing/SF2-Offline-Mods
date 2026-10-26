"""Read-only diagnostic: where is the local player, and why can it not be found?

    runtime\\python\\python.exe -X utf8 -B tools\\probe.py [--game <root>]

Dumps both discovery routes (the persistent level's actor list and GObjects) so a
failure can be attributed instead of guessed. Never writes to the game.
"""
import argparse
import collections
import json
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from sf2mod import attach                          # noqa: E402
from sf2mod.game import Fields, GWORLD, LEVEL, ACTORS, CLASS_INDEX, Player, u32  # noqa: E402

UINT32 = 0x10000


def level_actors(bridge):
    """Route A: GWorld -> PersistentLevel -> Actors (what corpse-flinch walks)."""
    info = {}
    world = u32(bridge, GWORLD)
    info['world'] = hex(world)
    if world < UINT32:
        return info, []
    level = u32(bridge, world + LEVEL)
    info['level'] = hex(level)
    if level < UINT32:
        return info, []
    try:
        pointer, count, capacity = struct.unpack('<3I', bridge.read(level + ACTORS, 12))
    except OSError as error:
        info['actors_error'] = str(error)
        return info, []
    info.update({'actors_pointer': hex(pointer), 'actors_count': count, 'actors_capacity': capacity})
    if not 0 < count <= capacity < 100000:
        return info, []
    try:
        raw = bridge.read(pointer, 4 * count)
    except OSError as error:
        info['actors_read_error'] = str(error)
        return info, []
    return info, [struct.unpack_from('<I', raw, 4 * i)[0] for i in range(count)]


def describe(bridge, fields, address):
    """Class name and the two controller links, or why they were unreadable."""
    try:
        kind = bridge.object_name(bridge.header(address)[CLASS_INDEX])
    except (OSError, RecursionError) as error:
        return {'address': hex(address), 'class_error': str(error)}
    row = {'address': hex(address), 'class': kind}
    for name, read in (('player', fields.player_of), ('pawn', fields.pawn_of)):
        try:
            row[name] = hex(read(address))
        except OSError as error:
            row[name + '_error'] = str(error)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--pid', type=int, default=0)
    parser.add_argument('--wait', type=float, default=0.0)
    parser.add_argument('--actors', type=int, default=0, help='列出前 N 个 actor 的类名')
    args = parser.parse_args()
    game = (args.game or attach.game_dir(HERE)).resolve()
    pid = args.pid or attach.find_pid(game, timeout=args.wait)
    if not pid:
        raise SystemExit('游戏没有运行')
    bridge = attach.connect(game, pid)
    out = {'pid': pid, 'read_only': True, 'time': round(time.time(), 3)}
    try:
        fields = Fields.wait(bridge, timeout=60.0)
        out['fields'] = fields.offsets()
        info, actors = level_actors(bridge)
        out['level'] = info
        out['actor_total'] = len(actors)
        prefixes = collections.Counter()
        rows = []
        for address in actors:
            if address < UINT32:
                continue
            row = describe(bridge, fields, address)
            kind = row.get('class') or '?'
            prefixes[kind.split('_')[0][:24]] += 1
            if kind.endswith('PlayerController') or (args.actors and len(rows) < args.actors):
                rows.append(row)
        out['class_prefixes'] = dict(prefixes.most_common(20))
        out['level_controllers'] = rows
        # Route B: every UObject whose class ends with PlayerController.
        found = []
        scanned = 0
        for address, name, cls in bridge.objects():
            scanned += 1
            if cls.endswith('PlayerController') and not name.startswith('Default__'):
                row = describe(bridge, fields, address)
                found.append(row)
        out['objects_scanned'] = scanned
        player = Player(bridge, fields)
        out['player_find'] = hex(player.find())
        out['player_summary'] = player.summary()
        out['objects_controllers'] = found
        out['verdict'] = ('no controller at all' if not found and not rows else
                          'level route sees a controller' if rows else
                          'only the object route sees a controller')
    except (OSError, RuntimeError, ValueError) as error:
        out['error'] = repr(error)
    finally:
        bridge.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
