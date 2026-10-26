"""Read-only check: attach, resolve fields, count corpses. Writes nothing.

    runtime\\python\\python.exe -X utf8 -B tools\\selftest.py [--game <root>]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import corpse_limit                                 # noqa: E402
from sf2mod import attach                           # noqa: E402
from sf2mod.game import Fields, Player              # noqa: E402


def main():
    game = attach.game_dir(HERE)
    pid = attach.find_pid(game, timeout=10.0)
    if not pid:
        print('游戏没在运行；安装可以继续，启动游戏后再自检。')
        return 0
    bridge = attach.connect(game, pid)
    try:
        fields = Fields.wait(bridge, timeout=60.0)
        player = Player(bridge, fields)
        actors = player.actors()
        corpses = [a for a in actors if corpse_limit.is_corpse(player, fields, a)]
        print(f'字段偏移：{fields.offsets()}')
        print(f'level actors：{len(actors)}，当前尸体：{len(corpses)}')
        for address in corpses[:10]:
            print('  ', hex(address), player.kind(address),
                  'lifespan', round(fields.life_span_of(address), 2))
        return 0
    finally:
        bridge.close()


if __name__ == '__main__':
    raise SystemExit(main())
