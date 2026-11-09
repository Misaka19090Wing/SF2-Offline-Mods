"""Start the client when needed, then the corpse-limit worker in the background.

    python\\python.exe -X utf8 -B tools\\launch.py

The worker is started detached with pythonw, so closing this script does not stop
it; it exits by itself when the game process disappears (logs\\worker.json).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
sys.path.insert(0, str(MOD))

from sf2mod import attach                          # noqa: E402

LOGS = MOD / 'logs'
STATE = LOGS / 'worker.json'
GAME_WAIT = 180.0


def game_start_command(game):
    """How this layout starts the client (the client's own launcher, not a copy)."""
    if attach.layout(game) == 'portable':
        for name in ('运行游戏.cmd', 'SF2启动器.exe'):
            if (game / name).is_file():
                return [str(game / name)]
        raise SystemExit('找不到游戏启动器：' + str(game))
    python = game / 'runtime' / 'python' / 'python.exe'
    return [str(python), '-I', '-X', 'utf8', '-B', str(game / 'dev.py'), 'play']


def start_game(game):
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / 'launcher.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen(game_start_command(game), cwd=str(game),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return process


def worker_for(game_pid):
    """Worker pid already serving this game process, else 0."""
    try:
        state = json.loads(STATE.read_text('utf-8'))
    except (OSError, ValueError):
        return 0
    if state.get('game') == game_pid and attach.pid_alive(state.get('worker', 0)):
        return int(state['worker'])
    return 0


def start_worker(game, pid):
    LOGS.mkdir(parents=True, exist_ok=True)
    python = MOD / 'python' / 'pythonw.exe'      # bundled: R10 seals the client's python
    if not python.is_file():
        python = game / 'runtime' / 'python' / 'pythonw.exe'
    if not python.is_file():
        python = game / 'runtime' / 'python' / 'python.exe'
    with (LOGS / 'worker-console.log').open('a', encoding='utf-8') as error:
        process = subprocess.Popen(
            [str(python), '-X', 'utf8', '-B', str(MOD / 'corpse_limit.py'), '--pid', str(pid)],
            cwd=str(MOD), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    STATE.write_text(json.dumps({'worker': process.pid, 'game': int(pid)}), 'utf-8')
    return process


def main():
    game = attach.game_dir(HERE).resolve()
    report = {'game': str(game), 'layout': attach.layout(game)}
    pid = attach.find_pid(game)
    if pid:
        report['game_pid'] = pid
        report['game'] = 'already running: ' + str(game)
    else:
        report['game'] = 'starting: ' + ' '.join(game_start_command(game))
        start_game(game)
        pid = attach.find_pid(game, timeout=GAME_WAIT)
        report['game_pid'] = pid
        if not pid:
            report['passed'] = False
            report['error'] = '等到超时也没看到客户端进程，请看 logs\\launcher.log'
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
    running = worker_for(pid)
    if running:
        report['worker'] = 'already running: %d' % running
    else:
        process = start_worker(game, pid)
        time.sleep(3.0)
        report['worker'] = process.pid
        report['worker_alive_after_3s'] = attach.pid_alive(process.pid)
    report['passed'] = bool(report.get('worker_alive_after_3s', True))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
