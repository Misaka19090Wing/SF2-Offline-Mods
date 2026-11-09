"""lobby-resolution: set the client's front-end window to a fixed resolution.

Default 1920x1080. The mod finds the game's main window and resizes it with
`SetWindowPos`; in windowed mode the engine resizes its viewport to the new client
area, so the lobby renders at that resolution. No game file is touched and the
R10 seal is irrelevant: this is a plain Win32 window call.

    lobby_only  true  -> only while the front end is showing (SFEntryGame)
                false -> whenever the window does not already match

If the lobby cannot be detected (a future client whose offsets moved), the mod
says so once and falls back to resizing regardless (`lobby_only` still true in
the config; the log tells you detection failed).
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sf2mod import attach, lobby, window              # noqa: E402

VERSION = '1.0.0'
DEFAULTS = {
    'enabled': True,
    'width': 1920,
    'height': 1080,
    'lobby_only': True,
    'poll_seconds': 2.0,
}
STATUS_INTERVAL = 30.0


def load_config(path=None):
    config = dict(DEFAULTS)
    file = Path(path) if path else HERE / 'config.json'
    if file.is_file():
        config.update(json.loads(file.read_text('utf-8')))
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        raise SystemExit('config.json 有未知项：' + ', '.join(sorted(unknown)))
    for key in ('width', 'height'):
        if not isinstance(config[key], int) or not 200 <= config[key] <= 7680:
            raise SystemExit(f'config.json 的 {key} 必须是 200–7680 的整数')
    if not isinstance(config['poll_seconds'], (int, float)) or config['poll_seconds'] <= 0:
        raise SystemExit('config.json 的 poll_seconds 必须大于 0')
    return config


class Log:
    """One JSON object per line, the same style as the other mods' logs."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event, **fields):
        record = {'event': event, 'time': round(time.time(), 3)}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass


class Worker:
    def __init__(self, bridge, pid, config, log):
        self.bridge = bridge
        self.pid = pid
        self.config = config
        self.log = log
        self.lobby = self._lobby()
        self.window = 0
        self.applied = 0
        self.failed = 0

    def _lobby(self):
        if not self.config['lobby_only']:
            return None
        try:
            return lobby.Lobby(self.bridge)
        except Exception as error:                    # noqa: BLE001 - degrade, never die
            self.log.emit('reader-unavailable', error=str(error),
                          note='lobby detection off, resizing whenever the window differs')
            return None

    # ------------------------------------------------------------------ window
    def ensure_window(self):
        if self.window and window.user32.IsWindow(self.window):
            return self.window
        self.window = window.find_window(self.pid)
        if self.window:
            width, height = window.client_size(self.window)
            self.log.emit('window', hwnd=hex(self.window), client=[width, height])
        return self.window

    def should_apply(self):
        if self.config['lobby_only'] and self.lobby is not None:
            try:
                return self.lobby.active()
            except OSError:
                return False                          # world changing; try next tick
        return True

    def step(self):
        hwnd = self.ensure_window()
        if not hwnd:
            return 'no-window'
        try:
            width, height = window.client_size(hwnd)
        except OSError:
            return 'no-window'
        target = (int(self.config['width']), int(self.config['height']))
        if (width, height) == target:
            return 'already'
        if not self.should_apply():
            return 'not-lobby'
        try:
            actual = window.resize_client(hwnd, target[0], target[1])
        except OSError as error:
            self.failed += 1
            if self.failed <= 3 or self.failed % 30 == 0:
                self.log.emit('resize-failed', error=str(error), total=self.failed)
            return 'failed'
        self.applied += 1
        self.log.emit('resized', hwnd=hex(hwnd), before=[width, height],
                      requested=list(target), actual=list(actual))
        return 'resized'

    def run(self):
        self.log.emit('ready', version=VERSION, pid=self.pid, **{
            key: self.config[key] for key in DEFAULTS})
        if not self.ensure_window():
            self.log.emit('window-pending', note='游戏窗口还没出现，继续等')
        end = time.monotonic() + 86400.0
        status = time.monotonic() + STATUS_INTERVAL
        while time.monotonic() < end:
            started = time.monotonic()
            if self.pid and not attach.pid_alive(self.pid):
                self.log.emit('game-exit', pid=self.pid)
                return
            try:
                state = self.step()
            except OSError:
                state = 'read-error'
            except Exception as error:                # noqa: BLE001 - keep the worker alive
                self.log.emit('worker-error', error=f'{type(error).__name__}: {error}')
                state = 'error'
            if time.monotonic() >= status:
                status = time.monotonic() + STATUS_INTERVAL
                self.log.emit('status', state=state, applied=self.applied,
                              failed=self.failed, window=hex(self.window or 0),
                              loop_ms=round((time.monotonic() - started) * 1000.0, 1))
            time.sleep(max(0.0, float(self.config['poll_seconds'])
                           - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', type=Path)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--once', action='store_true', help='report the window once and exit')
    parser.add_argument('--version', action='version', version=f'lobby-resolution {VERSION}')
    args = parser.parse_args()
    config = load_config(args.config)
    if not config['enabled']:
        print('config.json 里 enabled = false，不启动。')
        return 0
    game = (args.game or attach.game_dir(HERE)).resolve()
    pid = args.pid or attach.find_pid(game, timeout=30.0)
    if not pid:
        raise SystemExit('游戏没有运行：先启动游戏，再运行本程序。')
    log = Log(HERE / 'logs' / f"lobby-resolution-{time.strftime('%Y%m%d')}.jsonl")
    bridge = None
    try:
        bridge = attach.connect(game, pid)
    except Exception as error:                     # noqa: BLE001 - resizing needs no reader
        # A newer client whose offsets moved: still resize, just without lobby gating.
        log.emit('reader-unavailable', error=f'{type(error).__name__}: {error}',
                 note='lobby detection off, resizing whenever the window differs')
    try:
        worker = Worker(bridge, pid, config, log)
        if args.once:
            hwnd = worker.ensure_window()
            if not hwnd:
                raise SystemExit('游戏窗口没有出现。')
            print(json.dumps({'pid': pid, 'hwnd': hex(hwnd),
                              'client': window.client_size(hwnd),
                              'frame': window.frame_size(hwnd),
                              'lobby': worker.lobby.active() if worker.lobby else None,
                              'game_class': worker.lobby.game_class() if worker.lobby else None},
                             ensure_ascii=False))
            return 0
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge is not None:
            bridge.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
