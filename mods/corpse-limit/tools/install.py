"""Install into a game root (or the developer workspace) and write the launcher.

    python\\python.exe -X utf8 -B tools\\install.py [--game <root>] [--no-selftest]

Copies this folder to <root>\\mods\\corpse-limit when it is not already there,
writes <root>\\启动-尸体清理.cmd, then runs the read-only selftest.  Changes no game
file and writes no registry key.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
sys.path.insert(0, str(MOD))

from sf2mod import attach                          # noqa: E402

LAUNCHER = '''@echo off
setlocal
set MOD=%~dp0mods\\corpse-limit
if not exist "%MOD%\\tools\\launch.py" exit /b 1
start "" "%MOD%\\python\\pythonw.exe" -X utf8 -B "%MOD%\\tools\\launch.py"
'''
# The manifest ships with the mod: the installed copy must stay verifiable.
IGNORE = shutil.ignore_patterns('logs', '__pycache__', '*.pyc', 'corpse-limit-*.zip')


def copy_mod(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.installing')
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(MOD, temporary, ignore=IGNORE)
    if target.exists():
        shutil.rmtree(target)
    temporary.rename(target)
    return target


def write_launcher(game):
    # The mod carries its own interpreter: R10 seals the client's python.
    python = MOD / 'python' / 'pythonw.exe'
    if not python.is_file():
        raise SystemExit('mod 自带的 Python 缺失：' + str(MOD / 'python'))
    launcher = game / '启动-尸体清理.cmd'
    launcher.write_text(LAUNCHER, encoding='ascii')
    return launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--no-selftest', action='store_true')
    args = parser.parse_args()
    game = (args.game or attach.game_dir(HERE)).resolve()
    result = {'game': str(game), 'layout': attach.layout(game)}
    target = game / 'mods' / MOD.name
    if target.resolve() == MOD.resolve():
        result['mod'] = 'already in place: ' + str(target)
    else:
        copy_mod(target)
        result['mod'] = 'installed: ' + str(target)
    result['launcher'] = str(write_launcher(game))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.no_selftest:
        return 0
    print('--- 只读自检（不写入游戏）', flush=True)
    python = MOD / 'python' / 'python.exe'
    return subprocess.run([str(python), '-I', '-X', 'utf8', '-B',
                           str(target / 'tools' / 'selftest.py'), '--game', str(game)],
                          cwd=str(game)).returncode


if __name__ == '__main__':
    raise SystemExit(main())
