# -*- coding: utf-8 -*-
"""卸载 corpse-flinch：删掉 mods\\corpse-flinch 与启动器，游戏本体不动。

    runtime\\python\\python.exe -X utf8 -B tools\\uninstall.py
    runtime\\python\\python.exe -X utf8 -B tools\\uninstall.py --game "D:\\Games\\SF2-Offline"
"""
import argparse
import ctypes
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = ('runtime', 'tools', 'local_battle_bridge.py')
LAUNCHER = '启动-鞭尸模式.cmd'

sys.path.insert(0, str(ROOT / 'tools'))
import install as installer                             # noqa: E402  (shared helpers)


def console():
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:                                  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                              # noqa: BLE001
            pass


def is_game(path):
    return Path(path).joinpath(*MARKER).is_file()


def find_game(explicit=None):
    if explicit:
        return Path(explicit).resolve()
    for parent in [ROOT, *ROOT.parents]:
        if is_game(parent):
            return parent
    return None


def remove_tree(path, tries=20):
    """Windows sometimes keeps a handle for a moment (AV, Explorer, the
    just-stopped worker): retry instead of leaving a half-deleted folder that
    poisons the next install."""
    path = Path(path)
    for _ in range(tries):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.2)
    return not path.exists()


def remove_target(target):
    """One shot at a folder or a file; True when it is gone."""
    if target.is_dir():
        return remove_tree(target)
    if target.is_file():
        try:
            target.unlink()
            return True
        except OSError:
            return False
    return True


def retry_removal(targets, rounds=3):
    """Give the user a chance to close what is holding the folder.

    The usual blocker is Obsidian or an editor with docs\\00-总目录.md open: a
    long-lived lock that no amount of 0.2 s retries can beat.
    """
    left = [t for t in targets if t.exists()]
    for _ in range(rounds):
        if not left:
            break
        try:
            answer = input('关掉打开它的程序（Obsidian、编辑器、资源管理器）后'
                           '按回车重试，输入 n 放弃：').strip().lower()
        except EOFError:
            answer = 'n'
        if answer == 'n':
            break
        left = [t for t in left if t.exists() and not remove_target(t)]
    return left


def main():
    console()
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', default=None)
    args = parser.parse_args()

    game = find_game(args.game)
    if not game:
        print('[错误] 找不到游戏目录，请用 --game 指定')
        return 1

    # A running worker holds the bridge and the log file open: stop it first,
    # otherwise the folder is only half deleted and the next install is dirty.
    pack = game / 'mods' / 'corpse-flinch'
    stopped = installer.stop_worker(pack)
    if stopped:
        print(f'已停止 {stopped} 个还在运行的 worker')

    targets = [pack, game / LAUNCHER]
    for target in targets:
        if remove_target(target) and not target.exists():
            print(f'已删除 {target}')

    left = [t for t in targets if t.exists()]
    if left:
        print(f'[警告] 没能删干净，还剩下：{", ".join(str(t) for t in left)}')
        print('       通常是某个程序正开着里面的文件（Obsidian、编辑器、'
              '资源管理器窗口、杀毒扫描）。')
        left = retry_removal(left)
        if left:
            print('[警告] 仍然删不掉：' + ', '.join(str(t) for t in left))
            print('       关掉占用它的程序后，再双击一次 卸载.cmd 即可。')
            return 1
        print('重试成功，已经删干净')

    mods = game / 'mods'
    if mods.is_dir() and not any(mods.iterdir()):
        mods.rmdir()
        print(f'已删除空目录 {mods}')
    print('卸载完成。游戏本体没有被改动过。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
