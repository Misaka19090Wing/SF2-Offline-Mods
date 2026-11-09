"""Stop the worker and remove the launcher.

    python\\python.exe -X utf8 -B tools\\uninstall.py [--game <root>] [--purge]

Never touches the game itself: the mod only ever wrote its own folder, its logs and
the 启动-尸体清理.cmd next to the game launcher.
"""
import argparse
import ctypes
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
sys.path.insert(0, str(MOD))

from sf2mod import attach                          # noqa: E402

STATE = MOD / 'logs' / 'worker.json'


def stop(pid):
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    k.OpenProcess.restype = ctypes.c_void_p
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k.OpenProcess(0x0001, False, int(pid))   # PROCESS_TERMINATE
    if not handle:
        return False
    try:
        return bool(k.TerminateProcess(handle, 0))
    finally:
        k.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--purge', action='store_true', help='同时删除 mods\\corpse-limit')
    args = parser.parse_args()
    game = (args.game or attach.game_dir(HERE)).resolve()
    report = {'game': str(game)}
    try:
        state = json.loads(STATE.read_text('utf-8'))
    except (OSError, ValueError):
        state = {}
    worker = state.get('worker')
    if worker and attach.pid_alive(worker):
        report['worker'] = 'stopped: %d' % worker if stop(worker) else 'stop failed: %d' % worker
    else:
        report['worker'] = 'not running'
    STATE.unlink(missing_ok=True)
    launcher = game / '启动-尸体清理.cmd'
    if launcher.is_file():
        launcher.unlink()
        report['launcher'] = 'removed'
    else:
        report['launcher'] = 'not present'
    if args.purge:
        target = game / 'mods' / MOD.name
        if target.is_dir() and target.resolve() != MOD.resolve():
            shutil.rmtree(target)
            report['mod'] = 'removed: ' + str(target)
        elif target.resolve() == MOD.resolve():
            report['mod'] = 'this IS the installed copy: delete %s yourself' % MOD
        else:
            report['mod'] = 'not installed'
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
