"""Read-only check: attach, find the game window, report the lobby state.

    python\\python.exe -X utf8 -B tools\\selftest.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sf2mod import attach, lobby, window            # noqa: E402


def main():
    game = attach.game_dir(HERE)
    pid = attach.find_pid(game, timeout=10.0)
    if not pid:
        print('游戏没在运行；安装可以继续，启动游戏后再自检。')
        return 0
    bridge = attach.connect(game, pid)
    try:
        hwnd = window.find_window(pid)
        report = {'pid': pid, 'hwnd': hex(hwnd) if hwnd else None}
        if hwnd:
            report['client'] = window.client_size(hwnd)
            report['frame'] = window.frame_size(hwnd)
        try:
            detector = lobby.Lobby(bridge)
            report['game_class'] = detector.game_class()
            report['lobby'] = detector.active()
        except Exception as error:                   # noqa: BLE001
            report['lobby_error'] = str(error)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        bridge.close()


if __name__ == '__main__':
    raise SystemExit(main())
