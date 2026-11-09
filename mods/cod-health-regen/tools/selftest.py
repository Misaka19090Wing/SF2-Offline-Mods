"""Read-only live check: attach, resolve Health offsets, find the local player.

Writes nothing to the game. Run it before play to confirm the client version and
the property layout still match; it never installs a ProcessEvent hook, so unlike
corpse-flinch's selftest it is safe on a client whose bridge is already patched.

    python\\python.exe -X utf8 -B tools\\selftest.py --game <game root>
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from sf2mod import attach                          # noqa: E402
from sf2mod.game import Fields, Player             # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--pid', type=int, default=0)
    parser.add_argument('--wait', type=float, default=0.0, help='等游戏出现的秒数')
    args = parser.parse_args()
    game = (args.game or attach.game_dir(HERE)).resolve()
    report = {'game': str(game), 'layout': attach.layout(game),
              'client': str(attach.client_exe(game)), 'read_only': True}
    try:
        pid = args.pid or attach.find_pid(game, timeout=args.wait)
        report['pid'] = pid
        if not pid:
            raise SystemExit('游戏没有运行：先用游戏自己的启动器进游戏，再运行本检查。')
        bridge = attach.connect(game, pid)
    except (SystemExit, ValueError, OSError, RuntimeError) as error:
        report.update({'passed': False, 'error': str(error)})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    try:
        # A client that was just started answers every check except this one:
        # GObjects is only readable once boot has progressed.
        fields = Fields.wait(bridge, timeout=30.0)
        report['health_offset'] = hex(fields.health)
        report['health_max_offset'] = hex(fields.health_max)
        player = Player(bridge)
        deadline = time.monotonic() + 5.0
        pawn = player.find()
        while not pawn and time.monotonic() < deadline:
            time.sleep(0.5)                        # still in the menu
            pawn = player.find()
        report['world'] = hex(player.world)
        report['pawn'] = hex(pawn)
        if pawn:
            health, maximum = fields.read(pawn)
            report.update({'health': health, 'health_max': maximum,
                           'note': '读到的是实时数值；本检查不写入'})
        else:
            report['note'] = '已进入世界但还没有本地玩家 pawn（还在菜单/载入中）'
        report['passed'] = True
    except (ValueError, OSError, RuntimeError) as error:
        report.update({'passed': False, 'error': str(error)})
    finally:
        bridge.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
