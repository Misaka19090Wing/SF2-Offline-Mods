# -*- coding: utf-8 -*-
"""启动游戏 + 在后台挂上鞭尸 worker。

由 <game>\\启动-鞭尸模式.cmd 调用（那个 .cmd 只有两行 ASCII，中文都在这里）：
    runtime\\python\\pythonw.exe -X utf8 -B mods\\corpse-flinch\\tools\\launch.py

行为：
  * 游戏已经在跑 → 直接挂 worker
  * 游戏只剩一个崩溃提示框（上一局崩了留下的残留进程）→ 先清掉它再重开
  * 否则先 start 游戏根目录下的 运行游戏.cmd（找不到就提示，worker 仍会等）
  * 同一份补丁只允许一个 worker：已有活着的 worker 就不再起第二个

pythonw.exe 没有控制台，所有输出同时写进 logs\\launcher.log；
真正失败时弹一个 MessageBox，避免"双击没反应"这种无从排查的情况。
"""
import ctypes
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

MARKER = ('runtime', 'tools', 'local_battle_bridge.py')
LAUNCHERS = ('运行游戏.cmd', 'launch.cmd', 'run.cmd')
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
HERE = Path(__file__).resolve().parent          # ...\\corpse-flinch\\tools
PACK = HERE.parent                              # ...\\corpse-flinch


def log_dir():
    return PACK / 'logs'


def log(text, **fields):
    """Append one line to logs\\launcher.log. Never raises."""
    for directory in (log_dir(), Path.home() / '.corpse-flinch'):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / 'launcher.log').open('a', encoding='utf-8') as fh:
                extra = ''.join(f' {key}={value}' for key, value in fields.items())
                fh.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {text}{extra}\n')
            return
        except Exception:                              # noqa: BLE001
            continue


def say(text, **fields):
    log(text, **fields)
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except Exception:                                  # noqa: BLE001
        pass


def alert(text):
    """Visible under pythonw, where a traceback would otherwise vanish."""
    log('alert: ' + text)
    try:
        ctypes.windll.user32.MessageBoxW(None, text, 'corpse-flinch', 0x00000040)
    except Exception:                                  # noqa: BLE001
        pass


def game_root():
    for parent in [PACK, *PACK.parents]:
        if parent.joinpath(*MARKER).is_file():
            return parent
    return None


def alive(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259                                  # STILL_ACTIVE
    except Exception:                                  # noqa: BLE001
        return False


def running_worker():
    """PID from logs\\worker.pid if that process is still alive."""
    try:
        pid = int((log_dir() / 'worker.pid').read_text(encoding='ascii').strip())
    except Exception:                                  # noqa: BLE001
        return None
    return pid if alive(pid) else None


def stop(pid):
    """Kill a worker we are about to replace. True when it is gone."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, int(pid))   # PROCESS_TERMINATE
        if not handle:
            return False
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                  # noqa: BLE001
        return False
    for _ in range(20):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


DIALOG_TITLES = ('Message',)          # the caption of the engine's fatal box


def windows_of(pid):
    """Titles of the visible top-level windows owned by pid (may be empty)."""
    user32 = ctypes.windll.user32
    titles = []

    def visit(hwnd, _param):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == int(pid) and user32.IsWindowVisible(hwnd):
            size = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(size + 1)
            user32.GetWindowTextW(hwnd, buffer, size + 1)
            titles.append(buffer.value.strip())
        return True

    try:
        user32.EnumWindows(ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit), 0)
    except Exception:                                  # noqa: BLE001
        return []
    return titles


def worker_game_pid():
    """The game process the running worker attached to (last ready/process line)."""
    log = log_dir() / time.strftime('corpse-flinch-%Y%m%d.jsonl')
    try:
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()[-500:]
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        pid = record.get('pid')
        if record.get('event') in ('ready', 'process') and isinstance(pid, int):
            return pid
    return 0


def attach_module():
    sys.path.insert(0, str(PACK))
    from sf2mod import attach
    return attach


def client_pid(game):
    """PID of the isolated client, 0 when it is not running."""
    try:
        return attach_module().find_pid(game)
    except Exception:                                  # noqa: BLE001
        return 0


def wait_client(game, seconds):
    try:
        return attach_module().find_pid(game, timeout=seconds)
    except Exception:                                  # noqa: BLE001
        return 0


def start_game(game):
    for name in LAUNCHERS:
        starter = game / name
        if starter.is_file():
            say('启动游戏', script=str(starter))
            subprocess.Popen(['cmd', '/c', str(starter)], cwd=str(game),
                             creationflags=CREATE_NEW_CONSOLE,
                             stdin=subprocess.DEVNULL)
            return True
    say('找不到游戏的启动脚本（运行游戏.cmd），将等待游戏自己出现')
    return False


def python_exe(game):
    runtime = game / 'runtime' / 'python'
    for name in ('pythonw.exe', 'python.exe'):
        if (runtime / name).is_file():
            return runtime / name
    return None


def main():
    say('launcher 启动', here=str(HERE))
    game = game_root()
    if game is None:
        alert('找不到游戏目录。\n\n'
              '请把 corpse-flinch-*.zip 解压到游戏安装目录（就是 '
              'sf2.exe 所在的那一层），会得到 <游戏>\\mods\\corpse-flinch\\，'
              '再运行里面的 安装.cmd。')
        return 1
    say('游戏目录', game=str(game))

    pack = game / 'mods' / 'corpse-flinch'
    worker = pack / 'corpse_flinch.py'
    if not worker.is_file():
        alert(f'找不到 {worker}\n\n请先运行 mods\\corpse-flinch\\安装.cmd。')
        return 1

    python = python_exe(game)
    if python is None:
        alert('找不到 runtime\\python\\python.exe。\n\n'
              '这个客户端不是本项目支持的版本（缺少便携 Python）。')
        return 1

    # Start the game first, then think about the worker.  1.10.2 asked "is a
    # worker running?" before that and returned 0 when the answer was yes: a
    # worker left over from the previous session (the game was closed, the
    # worker kept idling) swallowed every double click -- no game, no worker.
    current = client_pid(game)
    if current:
        titles = windows_of(current)
        if titles and all(title in DIALOG_TITLES for title in titles):
            # A crash leaves the process alive with nothing on screen but the
            # engine's fatal-error box.  Every later double click then said
            # "game already running" and did nothing at all -- which looks like
            # a broken mod.  The dumps are already on disk, so clear it.
            say('上一局崩溃了，只剩 Fatal error 窗口，先清掉它',
                pid=current, windows=','.join(titles),
                reports='runtime\\test-client\\SFGame\\Logs')
            stop(current)
            current = 0
        elif not titles:
            say('游戏进程在跑但还没有窗口（可能还在加载）', pid=current)
        else:
            say('游戏已经在运行', pid=current)
    if not current:
        start_game(game)
        current = wait_client(game, 15.0)
        if current:
            say('游戏已启动', pid=current)
        else:
            say('还没看到游戏进程，worker 会等它出现')

    running = running_worker()
    if running:
        mine = worker_game_pid()
        if current and mine and mine != current:
            say('旧 worker 挂的是上一个游戏进程，换掉它',
                pid=running, was=mine, now=current)
            stop(running)
        else:
            # A worker whose game is gone quits by itself in about a second
            # (game-exit), so give it that second before declaring it useful.
            for _ in range(6):
                if not alive(running):
                    break
                time.sleep(0.5)
            if alive(running):
                say('worker 已经在跑，不重复启动', pid=running)
                return 0
            say('旧 worker 已经退出（它挂的游戏早关了）', pid=running)

    say('挂 worker', python=python.name, wait=300)
    # A console-less pythonw child inherits handles that look valid to Python
    # but fail on every write: the first print then kills the worker with
    # OSError 22 (exit 120).  Hand it a real stdout and a real stderr instead.
    console = pack / 'logs' / 'worker-console.log'
    try:
        console.parent.mkdir(parents=True, exist_ok=True)
        err = console.open('ab')
    except OSError:
        err = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [str(python), '-X', 'utf8', '-B', str(worker), '--wait', '300'],
            cwd=str(pack), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=err,
            creationflags=CREATE_NO_WINDOW)
    finally:
        if err is not subprocess.DEVNULL:
            err.close()
    time.sleep(2.0)
    if proc.poll() is not None:
        alert(f'worker 启动后立刻退出了（exit {proc.returncode}）。\n\n'
              f'请看日志：{console}')
        return 1
    say('worker 已启动', pid=proc.pid)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                  # noqa: BLE001
        log('launcher 崩溃:\n' + traceback.format_exc())
        alert('鞭尸启动器出错，详情见 '
              f'{PACK}\\logs\\launcher.log')
        sys.exit(1)
